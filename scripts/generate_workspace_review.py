#!/usr/bin/env python3
"""从 product workspace 导出当前 active assignment 的 review 页面。"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from hikbox_pictures.face_review_pipeline import (
    LARGE_PERSON_REVIEW_MIN_FACE_COUNT,
    render_review_html,
    write_person_review_pages,
)
from hikbox_pictures.product.config import WorkspaceLayout


def _workspace_root(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def _workspace_layout(workspace: Path) -> WorkspaceLayout:
    hikbox_root = workspace / ".hikbox"
    return WorkspaceLayout(
        workspace_root=workspace,
        hikbox_root=hikbox_root,
        library_db=hikbox_root / "library.db",
        embedding_db=hikbox_root / "embedding.db",
        config_json=hikbox_root / "config.json",
    )


def _require_workspace_initialized(workspace: Path) -> WorkspaceLayout:
    layout = _workspace_layout(workspace)
    missing = [path for path in (layout.library_db, layout.config_json) if not path.exists()]
    if missing:
        missing_text = ", ".join(str(path) for path in missing)
        raise ValueError(f"workspace 未初始化或文件缺失: {missing_text}")
    return layout


def _workspace_output_root(layout: WorkspaceLayout) -> Path:
    try:
        payload = json.loads(layout.config_json.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"workspace 配置读取失败: {layout.config_json}") from exc

    external_root = payload.get("external_root")
    if not isinstance(external_root, str) or not external_root.strip():
        raise ValueError(f"workspace 配置缺少 external_root: {layout.config_json}")
    return Path(external_root).expanduser().resolve()


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _load_latest_completed_assignment_run(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT
          id,
          scan_session_id,
          algorithm_version,
          param_snapshot_json,
          run_kind,
          started_at,
          finished_at,
          status
        FROM assignment_run
        WHERE status='completed'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise ValueError("workspace 中没有 completed assignment_run，无法导出 review")
    return row


def _load_source_labels(conn: sqlite3.Connection, *, scan_session_id: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT s.label
        FROM scan_session_source AS ss
        INNER JOIN library_source AS s ON s.id = ss.library_source_id
        WHERE ss.scan_session_id = ?
        ORDER BY s.id ASC
        """,
        (int(scan_session_id),),
    ).fetchall()
    labels = [str(row["label"]) for row in rows if str(row["label"]).strip()]
    if labels:
        return labels
    fallback_rows = conn.execute(
        """
        SELECT label
        FROM library_source
        WHERE enabled=1 AND removed_at IS NULL
        ORDER BY id ASC
        """
    ).fetchall()
    return [str(row["label"]) for row in fallback_rows if str(row["label"]).strip()]


def _load_scan_session_run_kind(conn: sqlite3.Connection, *, scan_session_id: int) -> str:
    row = conn.execute(
        "SELECT run_kind FROM scan_session WHERE id=?",
        (int(scan_session_id),),
    ).fetchone()
    if row is None:
        raise ValueError(f"assignment_run 关联的 scan_session 不存在: {scan_session_id}")
    return str(row["run_kind"])


def _load_previous_completed_assignment_run(
    conn: sqlite3.Connection,
    *,
    assignment_run_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, param_snapshot_json
        FROM assignment_run
        WHERE status='completed'
          AND id < ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (int(assignment_run_id),),
    ).fetchone()


def _build_fallback_full_meta(
    conn: sqlite3.Connection,
    *,
    assignment_run: sqlite3.Row,
    param_snapshot: dict[str, Any],
) -> dict[str, Any]:
    requested_scan_run_kind = _load_scan_session_run_kind(
        conn,
        scan_session_id=int(assignment_run["scan_session_id"]),
    )
    actual_assignment_run_kind = str(assignment_run["run_kind"])
    previous_run = _load_previous_completed_assignment_run(conn, assignment_run_id=int(assignment_run["id"]))
    previous_param_snapshot_matches = None
    previous_completed_assignment_run_id = None
    if previous_run is not None:
        previous_completed_assignment_run_id = int(previous_run["id"])
        previous_param_snapshot_matches = json.loads(str(previous_run["param_snapshot_json"])) == param_snapshot

    if requested_scan_run_kind != "scan_incremental":
        conclusion = "no"
        conclusion_text = "未发生 fallback full"
        reason_text = "当前会话请求的就是 full/resume 主链路，不存在 incremental fallback full。"
    elif actual_assignment_run_kind == "scan_full":
        conclusion = "likely_yes"
        conclusion_text = "高概率发生 fallback full"
        reason_text = "会话请求为 scan_incremental，但实际 assignment_run 记录为 scan_full。"
    else:
        conclusion = "unknown"
        conclusion_text = "无法判断 fallback full"
        reason_text = "会话请求与 assignment_run 未呈现 fallback full 特征。"

    evidence = [
        f"scan_session.run_kind = {requested_scan_run_kind}",
        f"assignment_run.run_kind = {actual_assignment_run_kind}",
    ]
    if previous_completed_assignment_run_id is not None:
        evidence.append(f"上一轮 completed assignment_run = {previous_completed_assignment_run_id}")
    if previous_param_snapshot_matches is not None:
        evidence.append(f"上一轮参数快照一致 = {'是' if previous_param_snapshot_matches else '否'}")

    return {
        "conclusion": conclusion,
        "conclusion_text": conclusion_text,
        "reason_text": reason_text,
        "requested_scan_run_kind": requested_scan_run_kind,
        "actual_assignment_run_kind": actual_assignment_run_kind,
        "previous_completed_assignment_run_id": previous_completed_assignment_run_id,
        "previous_param_snapshot_matches": previous_param_snapshot_matches,
        "evidence": evidence,
    }


def _build_member(
    row: sqlite3.Row,
    *,
    output_dir: Path,
    external_root: Path,
) -> dict[str, Any]:
    return {
        "face_id": f"fo_{int(row['face_id'])}",
        "crop_relpath": _render_relpath(
            relpath=str(row["crop_relpath"]),
            output_dir=output_dir,
            external_root=external_root,
        ),
        "context_relpath": _render_relpath(
            relpath=str(row["context_relpath"]),
            output_dir=output_dir,
            external_root=external_root,
        ),
        "quality_score": float(row["quality_score"] or 0.0),
        "magface_quality": float(row["magface_quality"] or 0.0),
        "cluster_probability": None if row["confidence"] is None else float(row["confidence"]),
    }


def _render_relpath(*, relpath: str, output_dir: Path, external_root: Path) -> str:
    normalized = relpath.replace("\\", "/").lstrip("/")
    local_artifacts = output_dir / normalized
    if local_artifacts.exists():
        return normalized
    target = external_root / normalized
    return os.path.relpath(target, start=output_dir).replace("\\", "/")


def _ensure_artifacts_link(*, output_dir: Path, external_root: Path) -> None:
    source = external_root / "artifacts"
    if not source.exists():
        raise FileNotFoundError(f"workspace artifacts 不存在: {source}")
    if output_dir.resolve() == external_root.resolve():
        return

    link_path = output_dir / "artifacts"
    if link_path.is_symlink():
        if link_path.resolve() == source.resolve():
            return
        raise ValueError(f"输出目录中的 artifacts 链接指向错误: {link_path} -> {link_path.resolve()}")
    if link_path.exists():
        return

    target_relpath = os.path.relpath(source, start=output_dir)
    link_path.symlink_to(target_relpath, target_is_directory=True)


def _load_assigned_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        WITH active_face_cluster AS (
          SELECT
            fcm.face_observation_id,
            MIN(fc.id) AS cluster_id
          FROM face_cluster_member AS fcm
          INNER JOIN face_cluster AS fc ON fc.id = fcm.face_cluster_id
          WHERE fc.status = 'active'
          GROUP BY fcm.face_observation_id
        )
        SELECT
          a.person_id,
          person.display_name,
          person.is_named,
          f.id AS face_id,
          f.crop_relpath,
          f.context_relpath,
          f.quality_score,
          f.magface_quality,
          a.confidence,
          afc.cluster_id
        FROM person_face_assignment AS a
        INNER JOIN person ON person.id = a.person_id
        INNER JOIN face_observation AS f ON f.id = a.face_observation_id
        INNER JOIN photo_asset AS p ON p.id = f.photo_asset_id
        LEFT JOIN active_face_cluster AS afc ON afc.face_observation_id = f.id
        WHERE a.active = 1
          AND person.status = 'active'
          AND f.active = 1
          AND p.asset_status = 'active'
        ORDER BY a.person_id ASC, COALESCE(afc.cluster_id, 0) ASC, f.id ASC
        """
    ).fetchall()


def _load_unassigned_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
          f.id AS face_id,
          f.crop_relpath,
          f.context_relpath,
          f.quality_score,
          f.magface_quality
        FROM face_observation AS f
        INNER JOIN photo_asset AS p ON p.id = f.photo_asset_id
        LEFT JOIN person_face_assignment AS a
          ON a.face_observation_id = f.id
         AND a.active = 1
        WHERE f.active = 1
          AND p.asset_status = 'active'
          AND a.id IS NULL
        ORDER BY f.id ASC
        """
    ).fetchall()


def _person_key(*, person_id: int, display_name: str | None, is_named: int) -> str:
    if int(is_named) == 1 and isinstance(display_name, str) and display_name.strip():
        return display_name.strip()
    return f"person_{person_id}"


def _build_payload(
    *,
    workspace: Path,
    output_dir: Path,
    external_root: Path,
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    assignment_run = _load_latest_completed_assignment_run(conn)
    param_snapshot = json.loads(str(assignment_run["param_snapshot_json"]))
    source_labels = _load_source_labels(conn, scan_session_id=int(assignment_run["scan_session_id"]))

    assigned_rows = _load_assigned_rows(conn)
    unassigned_rows = _load_unassigned_rows(conn)

    persons_by_id: dict[int, dict[str, Any]] = {}
    clusters_by_key: dict[str, dict[str, Any]] = {}
    real_cluster_keys: set[str] = set()

    for row in assigned_rows:
        person_id = int(row["person_id"])
        cluster_id = row["cluster_id"]
        if cluster_id is None:
            cluster_key = f"cluster_missing_person_{person_id}"
            cluster_label = -2
        else:
            cluster_key = f"cluster_{int(cluster_id)}"
            cluster_label = int(cluster_id)
            real_cluster_keys.add(cluster_key)

        person = persons_by_id.setdefault(
            person_id,
            {
                "person_label": person_id,
                "person_key": _person_key(
                    person_id=person_id,
                    display_name=None if row["display_name"] is None else str(row["display_name"]),
                    is_named=int(row["is_named"] or 0),
                ),
                "clusters": [],
            },
        )
        cluster = clusters_by_key.get(cluster_key)
        if cluster is None:
            cluster = {
                "cluster_key": cluster_key,
                "cluster_label": cluster_label,
                "members": [],
            }
            clusters_by_key[cluster_key] = cluster
            person["clusters"].append(cluster)
        cluster["members"].append(_build_member(row, output_dir=output_dir, external_root=external_root))

    for person in persons_by_id.values():
        person_clusters = list(person["clusters"])
        for cluster in person_clusters:
            cluster["members"].sort(key=lambda member: (-float(member["quality_score"]), str(member["face_id"])))
            cluster["member_count"] = len(cluster["members"])
        person_clusters.sort(
            key=lambda cluster: (
                1 if int(cluster.get("cluster_label", -1)) < 0 else 0,
                -int(cluster.get("member_count", len(cluster.get("members", [])))),
                str(cluster.get("cluster_key", "")),
            )
        )
        person["person_cluster_count"] = len(person_clusters)
        person["person_face_count"] = sum(int(cluster["member_count"]) for cluster in person_clusters)

    clusters = list(clusters_by_key.values())
    noise_members = [
        {
            "face_id": f"fo_{int(row['face_id'])}",
            "crop_relpath": _render_relpath(
                relpath=str(row["crop_relpath"]),
                output_dir=output_dir,
                external_root=external_root,
            ),
            "context_relpath": _render_relpath(
                relpath=str(row["context_relpath"]),
                output_dir=output_dir,
                external_root=external_root,
            ),
            "quality_score": float(row["quality_score"] or 0.0),
            "magface_quality": float(row["magface_quality"] or 0.0),
            "cluster_probability": None,
        }
        for row in unassigned_rows
    ]
    noise_members.sort(key=lambda member: (-float(member["quality_score"]), str(member["face_id"])))
    if noise_members:
        clusters.append(
            {
                "cluster_key": "noise",
                "cluster_label": -1,
                "member_count": len(noise_members),
                "members": noise_members,
            }
        )
    clusters.sort(
        key=lambda cluster: (
            1 if int(cluster.get("cluster_label", -1)) == -1 else 0,
            -int(cluster.get("member_count", len(cluster.get("members", [])))),
            str(cluster.get("cluster_key", "")),
        )
    )

    persons = list(persons_by_id.values())
    persons.sort(
        key=lambda person: (
            -int(person.get("person_face_count", 0)),
            int(person.get("person_label", 0)),
        )
    )

    image_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM photo_asset
            WHERE asset_status='active'
            """
        ).fetchone()[0]
    )
    face_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM face_observation AS f
            INNER JOIN photo_asset AS p ON p.id = f.photo_asset_id
            WHERE f.active=1
              AND p.asset_status='active'
            """
        ).fetchone()[0]
    )

    workspace_text = os.path.relpath(workspace, start=Path.cwd()).replace("\\", "/")
    fallback_full = _build_fallback_full_meta(
        conn,
        assignment_run=assignment_run,
        param_snapshot=param_snapshot,
    )
    meta = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model": f"workspace/{str(assignment_run['algorithm_version'])}",
        "clusterer": "workspace face_cluster",
        "person_clusterer": str(assignment_run["algorithm_version"]),
        "person_linkage": str(param_snapshot.get("person_linkage", "unknown")),
        "person_enable_same_photo_cannot_link": bool(param_snapshot.get("person_enable_same_photo_cannot_link", False)),
        "source": (
            f"workspace={workspace_text} | assignment_run={int(assignment_run['id'])} | "
            f"run_kind={str(assignment_run['run_kind'])} | sources={', '.join(source_labels)}"
        ),
        "image_count": image_count,
        "face_count": face_count,
        "cluster_count": len(real_cluster_keys),
        "noise_count": len(noise_members),
        "person_count": len(persons),
        "assignment_run_id": int(assignment_run["id"]),
        "scan_session_id": int(assignment_run["scan_session_id"]),
        "run_kind": str(assignment_run["run_kind"]),
        "finished_at": None if assignment_run["finished_at"] is None else str(assignment_run["finished_at"]),
        "fallback_full": fallback_full,
    }
    return {
        "meta": meta,
        "failed_images": [],
        "failed_faces": [],
        "persons": persons,
        "clusters": clusters,
    }


def write_workspace_review(*, workspace: Path, output_dir: Path) -> dict[str, Any]:
    layout = _require_workspace_initialized(workspace)
    external_root = _workspace_output_root(layout)
    output_dir.mkdir(parents=True, exist_ok=True)
    _ensure_artifacts_link(output_dir=output_dir, external_root=external_root)

    conn = _connect(layout.library_db)
    try:
        payload = _build_payload(
            workspace=workspace,
            output_dir=output_dir,
            external_root=external_root,
            conn=conn,
        )
    finally:
        conn.close()

    person_pages = write_person_review_pages(output_dir=output_dir, payload=payload)
    payload["meta"]["person_review_page_count"] = len(person_pages)
    payload["meta"]["person_review_pages_manifest"] = "review_person_pages.json"
    payload["meta"]["large_person_review_min_face_count"] = int(LARGE_PERSON_REVIEW_MIN_FACE_COUNT)
    payload["meta"]["large_person_review_page_count"] = len(
        [page for page in person_pages if int(page.get("person_face_count", 0)) > int(LARGE_PERSON_REVIEW_MIN_FACE_COUNT)]
    )
    payload["meta"]["large_person_review_pages_manifest"] = "review_person_pages_over_100.json"
    payload["meta"]["large_person_review_index_html"] = "review_person_pages_over_100.html"

    (output_dir / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "review_payload_meta.json").write_text(
        json.dumps(payload["meta"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "review.html").write_text(render_review_html(payload), encoding="utf-8")

    return {
        "workspace": str(workspace),
        "output_dir": str(output_dir),
        "review_html": str(output_dir / "review.html"),
        "manifest_json": str(output_dir / "manifest.json"),
        "review_payload_meta_json": str(output_dir / "review_payload_meta.json"),
        **payload["meta"],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="导出当前 workspace active assignment 的 review 页面")
    parser.add_argument("--workspace", required=True, help="workspace 根目录")
    parser.add_argument("--output-dir", required=True, help="review 输出目录")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    result = write_workspace_review(
        workspace=_workspace_root(args.workspace),
        output_dir=_workspace_root(args.output_dir),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
