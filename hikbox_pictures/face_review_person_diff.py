from __future__ import annotations

import argparse
import html
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


def _load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _rebase_asset_relpath(relpath: str, manifest_dir: Path, output_dir: Path) -> str:
    if not relpath:
        return ""
    asset_path = (manifest_dir / relpath).resolve()
    return Path(os.path.relpath(asset_path, output_dir)).as_posix()


def _copy_member_for_output(member: dict[str, Any], manifest_dir: Path, output_dir: Path) -> dict[str, Any]:
    copied = dict(member)
    for key in ("crop_relpath", "context_relpath", "aligned_relpath"):
        copied[key] = _rebase_asset_relpath(str(member.get(key, "")), manifest_dir=manifest_dir, output_dir=output_dir)
    return copied


def _index_clusters(
    payload: dict[str, Any],
    manifest_dir: Path,
    output_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    indexed_clusters: list[dict[str, Any]] = []
    face_to_cluster: dict[str, dict[str, Any]] = {}

    for cluster in payload.get("clusters", []):
        members = [
            _copy_member_for_output(member, manifest_dir=manifest_dir, output_dir=output_dir)
            for member in list(cluster.get("members", []))
        ]
        indexed = {
            "cluster_key": str(cluster.get("cluster_key", "")),
            "cluster_label": int(cluster.get("cluster_label", -1)),
            "members": members,
        }
        indexed_clusters.append(indexed)
        for member in members:
            face_to_cluster[str(member.get("face_id", ""))] = indexed

    return indexed_clusters, face_to_cluster


def _index_persons(
    payload: dict[str, Any],
    manifest_dir: Path,
    output_dir: Path,
) -> dict[int, dict[str, Any]]:
    raw_persons = list(payload.get("persons", []))
    if not raw_persons:
        raise ValueError("manifest 缺少 persons 字段，无法按 person label 对比新增样本")

    persons_by_label: dict[int, dict[str, Any]] = {}
    for person in raw_persons:
        label = int(person.get("person_label", -1))
        person_key = str(person.get("person_key", f"person_{label}"))
        clusters: list[dict[str, Any]] = []
        members: list[dict[str, Any]] = []
        member_ids: set[str] = set()

        for cluster in list(person.get("clusters", [])):
            cluster_members = [
                _copy_member_for_output(member, manifest_dir=manifest_dir, output_dir=output_dir)
                for member in list(cluster.get("members", []))
            ]
            clusters.append(
                {
                    "cluster_key": str(cluster.get("cluster_key", "")),
                    "cluster_label": int(cluster.get("cluster_label", -1)),
                    "member_count": int(cluster.get("member_count", len(cluster_members))),
                    "members": cluster_members,
                }
            )

            for member in cluster_members:
                face_id = str(member.get("face_id", ""))
                if not face_id or face_id in member_ids:
                    continue
                member_ids.add(face_id)
                members.append(member)

        persons_by_label[label] = {
            "person_label": label,
            "person_key": person_key,
            "person_cluster_count": int(person.get("person_cluster_count", len(clusters))),
            "person_face_count": int(person.get("person_face_count", len(member_ids))),
            "clusters": clusters,
            "members": members,
            "member_ids": member_ids,
        }

    return persons_by_label


def _normalize_person_labels(person_labels: Iterable[int]) -> list[int]:
    seen: set[int] = set()
    normalized: list[int] = []
    for label in person_labels:
        value = int(label)
        if value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    if not normalized:
        raise ValueError("至少需要提供一个 person label")
    return normalized


def _annotate_member_source(
    member: dict[str, Any],
    base_face_to_cluster: dict[str, dict[str, Any]],
    candidate_face_to_cluster: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    copied = dict(member)
    face_id = str(member.get("face_id", ""))

    candidate_cluster = candidate_face_to_cluster.get(face_id)
    if candidate_cluster is None:
        copied["candidate_cluster_key"] = "missing"
        copied["candidate_cluster_label"] = -2
    else:
        copied["candidate_cluster_key"] = str(candidate_cluster.get("cluster_key", ""))
        copied["candidate_cluster_label"] = int(candidate_cluster.get("cluster_label", -1))

    base_cluster = base_face_to_cluster.get(face_id)
    if base_cluster is None:
        copied["base_cluster_key"] = "missing"
        copied["base_cluster_label"] = -2
    else:
        copied["base_cluster_key"] = str(base_cluster.get("cluster_key", ""))
        copied["base_cluster_label"] = int(base_cluster.get("cluster_label", -1))
    return copied


def _source_summary(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counter = Counter(str(member.get("base_cluster_key", "")) for member in members)
    labels = {str(member.get("base_cluster_key", "")): int(member.get("base_cluster_label", -1)) for member in members}
    summary = [
        {
            "cluster_key": cluster_key,
            "cluster_label": labels.get(cluster_key, -1),
            "face_count": face_count,
        }
        for cluster_key, face_count in counter.items()
    ]
    summary.sort(key=lambda item: (-int(item["face_count"]), str(item["cluster_key"])))
    return summary


def _sort_members_by_quality(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        members,
        key=lambda member: (
            -float(member.get("quality_score", 0.0)),
            str(member.get("face_id", "")),
        ),
    )


def build_person_added_diff_payload(
    base_manifest_path: Path,
    candidate_manifest_path: Path,
    output_html_path: Path,
    person_labels: Iterable[int],
) -> dict[str, Any]:
    normalized_labels = _normalize_person_labels(person_labels)
    output_dir = output_html_path.resolve().parent
    base_manifest_path = base_manifest_path.resolve()
    candidate_manifest_path = candidate_manifest_path.resolve()

    base_payload = _load_manifest(base_manifest_path)
    candidate_payload = _load_manifest(candidate_manifest_path)

    _, base_face_to_cluster = _index_clusters(
        base_payload,
        manifest_dir=base_manifest_path.parent,
        output_dir=output_dir,
    )
    _, candidate_face_to_cluster = _index_clusters(
        candidate_payload,
        manifest_dir=candidate_manifest_path.parent,
        output_dir=output_dir,
    )
    base_persons_by_label = _index_persons(
        base_payload,
        manifest_dir=base_manifest_path.parent,
        output_dir=output_dir,
    )
    candidate_persons_by_label = _index_persons(
        candidate_payload,
        manifest_dir=candidate_manifest_path.parent,
        output_dir=output_dir,
    )

    persons: list[dict[str, Any]] = []
    for label in normalized_labels:
        base_person = base_persons_by_label.get(label)
        candidate_person = candidate_persons_by_label.get(label)
        if candidate_person is None:
            persons.append(
                {
                    "person_label": label,
                    "candidate_person_key": "missing",
                    "candidate_member_count": 0,
                    "candidate_cluster_count": 0,
                    "base_person_key": "missing" if base_person is None else str(base_person.get("person_key", "")),
                    "base_member_count": 0 if base_person is None else int(base_person.get("person_face_count", 0)),
                    "base_cluster_count": 0 if base_person is None else int(base_person.get("person_cluster_count", 0)),
                    "added_member_count": 0,
                    "source_summary": [],
                    "added_members": [],
                }
            )
            continue

        base_member_ids = set() if base_person is None else set(base_person.get("member_ids", set()))
        candidate_members = list(candidate_person.get("members", []))

        added_members = [
            _annotate_member_source(
                member,
                base_face_to_cluster=base_face_to_cluster,
                candidate_face_to_cluster=candidate_face_to_cluster,
            )
            for member in candidate_members
            if str(member.get("face_id", "")) not in base_member_ids
        ]
        added_members = _sort_members_by_quality(added_members)
        persons.append(
            {
                "person_label": label,
                "candidate_person_key": str(candidate_person.get("person_key", "")),
                "candidate_member_count": int(candidate_person.get("person_face_count", len(candidate_members))),
                "candidate_cluster_count": int(candidate_person.get("person_cluster_count", 0)),
                "base_person_key": "missing" if base_person is None else str(base_person.get("person_key", "")),
                "base_member_count": 0 if base_person is None else int(base_person.get("person_face_count", 0)),
                "base_cluster_count": 0 if base_person is None else int(base_person.get("person_cluster_count", 0)),
                "added_member_count": len(added_members),
                "source_summary": _source_summary(added_members),
                "added_members": added_members,
            }
        )

    total_added = sum(int(person.get("added_member_count", 0)) for person in persons)
    person_with_additions = sum(1 for person in persons if int(person.get("added_member_count", 0)) > 0)

    return {
        "meta": {
            "base_manifest_path": str(base_manifest_path),
            "candidate_manifest_path": str(candidate_manifest_path),
            "target_person_labels": normalized_labels,
            "target_person_count": len(normalized_labels),
            "person_with_additions_count": person_with_additions,
            "total_added_face_count": total_added,
            "base_clusterer": str(base_payload.get("meta", {}).get("clusterer", "HDBSCAN")),
            "candidate_clusterer": str(candidate_payload.get("meta", {}).get("clusterer", "HDBSCAN")),
        },
        "persons": persons,
    }


def _render_source_chips(summary: list[dict[str, Any]]) -> str:
    if not summary:
        return '<p class="empty-text">新增样本没有基线来源信息。</p>'
    chips = "".join(
        [
            (
                f"<span class=\"source-chip\">"
                f"{html.escape(str(item.get('cluster_key', '')))}"
                f" · {int(item.get('face_count', 0))}"
                "</span>"
            )
            for item in summary
        ]
    )
    return f"<div class=\"source-chip-list\">{chips}</div>"


def _render_member_cards(members: list[dict[str, Any]]) -> str:
    if not members:
        return '<p class="empty-text">该 person 没有新增样本。</p>'

    cards: list[str] = []
    for member in members:
        face_id = html.escape(str(member.get("face_id", "")))
        crop_relpath = html.escape(str(member.get("crop_relpath", "")))
        context_relpath = html.escape(str(member.get("context_relpath", "")))
        quality_score = float(member.get("quality_score", 0.0))
        magface_quality = float(member.get("magface_quality", 0.0))
        prob = member.get("cluster_probability")
        prob_text = "-" if prob is None else f"{float(prob):.3f}"
        candidate_cluster_key = html.escape(str(member.get("candidate_cluster_key", "")))
        base_cluster_key = html.escape(str(member.get("base_cluster_key", "")))
        cards.append(
            f"""
            <article class="face-card">
              <header>
                <strong>{face_id}</strong>
                <span>Q={quality_score:.3f} · M={magface_quality:.2f} · P={prob_text}</span>
              </header>
              <div class="face-meta">候选 cluster：{candidate_cluster_key} · 基线来源：{base_cluster_key}</div>
              <div class="thumb-grid">
                <a href="{crop_relpath}" target="_blank"><img src="{crop_relpath}" alt="crop {face_id}"></a>
                <a href="{context_relpath}" target="_blank"><img src="{context_relpath}" alt="context {face_id}"></a>
              </div>
            </article>
            """
        )
    return "".join(cards)


def _render_person_sections(persons: list[dict[str, Any]]) -> str:
    sections: list[str] = []
    for person in persons:
        label = int(person.get("person_label", -1))
        candidate_person_key = html.escape(str(person.get("candidate_person_key", "")))
        base_person_key = html.escape(str(person.get("base_person_key", "")))
        candidate_member_count = int(person.get("candidate_member_count", 0))
        base_member_count = int(person.get("base_member_count", 0))
        candidate_cluster_count = int(person.get("candidate_cluster_count", 0))
        base_cluster_count = int(person.get("base_cluster_count", 0))
        added_member_count = int(person.get("added_member_count", 0))
        source_summary = list(person.get("source_summary", []))
        added_members = list(person.get("added_members", []))
        sections.append(
            f"""
            <details class="person-panel" open>
              <summary>
                <div>
                  <h2>person {label}</h2>
                  <div class="subtitle-row">候选人物组：{candidate_person_key} · 基线人物组：{base_person_key}</div>
                </div>
                <span class="count-chip">新增样本 {added_member_count}</span>
              </summary>
              <div class="person-body">
                <div class="meta-row">
                  候选总数 {candidate_member_count}（clusters={candidate_cluster_count}）
                  · 基线总数 {base_member_count}（clusters={base_cluster_count}）
                </div>
                <div class="source-block">
                  <strong>新增样本在基线中的来源</strong>
                  {_render_source_chips(source_summary)}
                </div>
                <div class="face-grid">{_render_member_cards(added_members)}</div>
              </div>
            </details>
            """
        )
    return "".join(sections)


def render_person_added_diff_html(payload: dict[str, Any]) -> str:
    meta = payload.get("meta", {})
    persons = list(payload.get("persons", []))

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>人物新增样本对比</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4f1eb;
      --panel: #fffdf9;
      --text: #1f2328;
      --muted: #5c6773;
      --line: #d9d2c7;
      --accent: #8f3b1b;
      --accent-soft: #f2d8cb;
      --chip: #efe8de;
      --shadow: 0 10px 30px rgba(84, 54, 29, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(210, 163, 126, 0.22), transparent 34%),
        linear-gradient(180deg, #fbf7f1 0%, var(--bg) 100%);
    }}
    main {{
      max-width: 1440px;
      margin: 0 auto;
      padding: 32px 24px 48px;
    }}
    h1 {{
      margin: 0 0 10px;
      font-size: 32px;
    }}
    .page-hint {{
      color: var(--muted);
      margin-bottom: 24px;
    }}
    .summary-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-bottom: 24px;
    }}
    .summary-card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: var(--shadow);
      padding: 16px 18px;
    }}
    .summary-card dt {{
      font-size: 13px;
      color: var(--muted);
      margin-bottom: 6px;
    }}
    .summary-card dd {{
      margin: 0;
      font-size: 28px;
      font-weight: 700;
    }}
    .person-panel {{
      border: 1px solid var(--line);
      border-radius: 16px;
      background: #fff;
      margin-bottom: 12px;
      overflow: hidden;
      box-shadow: var(--shadow);
    }}
    .person-panel summary {{
      list-style: none;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 16px;
    }}
    .person-panel summary::-webkit-details-marker {{
      display: none;
    }}
    .person-panel h2 {{
      margin: 0;
      font-size: 20px;
    }}
    .subtitle-row {{
      margin-top: 4px;
      color: var(--muted);
      font-size: 13px;
    }}
    .count-chip {{
      background: var(--accent-soft);
      color: var(--accent);
      border-radius: 999px;
      padding: 8px 12px;
      font-size: 13px;
      font-weight: 600;
      white-space: nowrap;
    }}
    .person-body {{
      padding: 0 16px 16px;
    }}
    .meta-row {{
      margin-bottom: 12px;
      color: var(--muted);
      font-size: 14px;
    }}
    .source-block {{
      margin-bottom: 14px;
    }}
    .source-block strong {{
      display: block;
      margin-bottom: 8px;
      font-size: 14px;
    }}
    .source-chip-list {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .source-chip {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      background: var(--chip);
      padding: 6px 10px;
      font-size: 12px;
      color: var(--text);
    }}
    .face-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
    }}
    .face-card {{
      border: 1px solid var(--line);
      border-radius: 14px;
      background: #fffdfb;
      padding: 12px;
    }}
    .face-card header {{
      display: flex;
      flex-direction: column;
      gap: 4px;
      margin-bottom: 8px;
      font-size: 13px;
    }}
    .face-meta {{
      color: var(--accent);
      font-size: 12px;
      margin-bottom: 8px;
    }}
    .thumb-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }}
    .thumb-grid a {{
      display: block;
    }}
    .thumb-grid img {{
      display: block;
      width: 100%;
      aspect-ratio: 1 / 1;
      object-fit: cover;
      border-radius: 10px;
      border: 1px solid var(--line);
      background: #f0ece6;
    }}
    .empty-text {{
      margin: 0;
      color: var(--muted);
      font-size: 14px;
    }}
  </style>
</head>
<body>
  <main>
    <h1>人物新增样本对比</h1>
    <div class="page-hint">按 person label 对比候选与基线分组，仅展示候选中新增并入的样本，方便快速人工复核。</div>
    <div class="summary-grid">
      <dl class="summary-card">
        <dt>目标 person 数</dt>
        <dd>{int(meta.get("target_person_count", 0))}</dd>
      </dl>
      <dl class="summary-card">
        <dt>有新增的 person 数</dt>
        <dd>{int(meta.get("person_with_additions_count", 0))}</dd>
      </dl>
      <dl class="summary-card">
        <dt>新增样本总数</dt>
        <dd>{int(meta.get("total_added_face_count", 0))}</dd>
      </dl>
    </div>
    {_render_person_sections(persons)}
  </main>
</body>
</html>
"""


def write_person_added_diff_review(
    base_manifest_path: Path,
    candidate_manifest_path: Path,
    output_html_path: Path,
    person_labels: Iterable[int],
) -> dict[str, Any]:
    payload = build_person_added_diff_payload(
        base_manifest_path=base_manifest_path,
        candidate_manifest_path=candidate_manifest_path,
        output_html_path=output_html_path,
        person_labels=person_labels,
    )
    output_html_path.parent.mkdir(parents=True, exist_ok=True)
    output_html_path.write_text(render_person_added_diff_html(payload), encoding="utf-8")
    return payload


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="对比两份人脸 review manifest，生成指定 person 的新增样本页面")
    parser.add_argument("--base-manifest", type=Path, required=True, help="基线 manifest.json")
    parser.add_argument("--candidate-manifest", type=Path, required=True, help="候选 manifest.json")
    parser.add_argument("--output-html", type=Path, required=True, help="输出 HTML 路径")
    parser.add_argument(
        "--person-label",
        type=int,
        action="append",
        dest="person_labels",
        required=True,
        help="目标 person label，可重复传入，例如 --person-label 0 --person-label 1",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    payload = write_person_added_diff_review(
        base_manifest_path=args.base_manifest,
        candidate_manifest_path=args.candidate_manifest,
        output_html_path=args.output_html,
        person_labels=list(args.person_labels),
    )
    meta = payload.get("meta", {})
    labels = ",".join(str(item) for item in list(meta.get("target_person_labels", [])))
    print(
        "完成："
        f"persons={labels} "
        f"person_with_additions={meta.get('person_with_additions_count')} "
        f"added_faces={meta.get('total_added_face_count')}"
    )
    print(f"HTML: {args.output_html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
