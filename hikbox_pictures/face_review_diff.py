from __future__ import annotations

import argparse
import html
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any


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


def _signature_for_face(group: dict[str, Any]) -> tuple[str, ...]:
    label = int(group.get("cluster_label", -1))
    if label == -1:
        return ("noise",)
    member_ids = sorted(str(member.get("face_id", "")) for member in group.get("members", []))
    return ("cluster", *member_ids)


def _relation_summary(face_ids: list[str], face_to_group: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter()
    labels: dict[str, int] = {}
    for face_id in face_ids:
        group = face_to_group.get(face_id)
        if group is None:
            cluster_key = "missing"
            cluster_label = -2
        else:
            cluster_key = str(group.get("cluster_key", ""))
            cluster_label = int(group.get("cluster_label", -1))
        counts[cluster_key] += 1
        labels[cluster_key] = cluster_label

    summary = [
        {
            "cluster_key": cluster_key,
            "cluster_label": labels[cluster_key],
            "face_count": face_count,
        }
        for cluster_key, face_count in counts.items()
    ]
    summary.sort(key=lambda item: (-int(item["face_count"]), str(item["cluster_key"])))
    return summary


def _annotate_members(
    members: list[dict[str, Any]],
    compare_face_to_group: dict[str, dict[str, Any]],
    compare_label: str,
) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for member in members:
        face_id = str(member.get("face_id", ""))
        compare_group = compare_face_to_group.get(face_id)
        copied = dict(member)
        copied["compare_label"] = compare_label
        copied["compare_group_key"] = "missing" if compare_group is None else str(compare_group.get("cluster_key", ""))
        copied["compare_group_label"] = -2 if compare_group is None else int(compare_group.get("cluster_label", -1))
        annotated.append(copied)
    return annotated


def _build_group_index(payload: dict[str, Any], manifest_dir: Path, output_dir: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    indexed_groups: list[dict[str, Any]] = []
    face_to_group: dict[str, dict[str, Any]] = {}

    for cluster in payload.get("clusters", []):
        members = [
            _copy_member_for_output(member, manifest_dir=manifest_dir, output_dir=output_dir)
            for member in list(cluster.get("members", []))
        ]
        group = {
            "cluster_key": str(cluster.get("cluster_key", "")),
            "cluster_label": int(cluster.get("cluster_label", -1)),
            "full_member_count": len(members),
            "members": members,
        }
        indexed_groups.append(group)
        for member in members:
            face_to_group[str(member.get("face_id", ""))] = group

    return indexed_groups, face_to_group


def build_cluster_diff_payload(
    base_manifest_path: Path,
    candidate_manifest_path: Path,
    output_html_path: Path,
) -> dict[str, Any]:
    output_dir = output_html_path.resolve().parent
    base_manifest_path = base_manifest_path.resolve()
    candidate_manifest_path = candidate_manifest_path.resolve()

    base_payload = _load_manifest(base_manifest_path)
    candidate_payload = _load_manifest(candidate_manifest_path)

    base_groups, base_face_to_group = _build_group_index(base_payload, manifest_dir=base_manifest_path.parent, output_dir=output_dir)
    candidate_groups, candidate_face_to_group = _build_group_index(
        candidate_payload,
        manifest_dir=candidate_manifest_path.parent,
        output_dir=output_dir,
    )

    all_face_ids = sorted(set(base_face_to_group) | set(candidate_face_to_group))
    changed_face_ids = [
        face_id
        for face_id in all_face_ids
        if _signature_for_face(base_face_to_group.get(face_id, {"cluster_label": -2, "members": []}))
        != _signature_for_face(candidate_face_to_group.get(face_id, {"cluster_label": -2, "members": []}))
    ]
    changed_face_id_set = set(changed_face_ids)

    candidate_result_groups: list[dict[str, Any]] = []
    for group in candidate_groups:
        member_face_ids = [str(member.get("face_id", "")) for member in group.get("members", [])]
        changed_member_ids = [face_id for face_id in member_face_ids if face_id in changed_face_id_set]
        if not changed_member_ids:
            continue

        display_members = list(group["members"])
        if int(group["cluster_label"]) == -1:
            display_members = [member for member in display_members if str(member.get("face_id", "")) in changed_face_id_set]

        candidate_result_groups.append(
            {
                "cluster_key": str(group["cluster_key"]),
                "cluster_label": int(group["cluster_label"]),
                "member_count": len(display_members),
                "full_member_count": int(group["full_member_count"]),
                "changed_face_count": len(changed_member_ids),
                "source_groups": _relation_summary(member_face_ids, face_to_group=base_face_to_group),
                "members": _annotate_members(display_members, compare_face_to_group=base_face_to_group, compare_label="基线"),
            }
        )

    base_result_groups: list[dict[str, Any]] = []
    for group in base_groups:
        member_face_ids = [str(member.get("face_id", "")) for member in group.get("members", [])]
        changed_member_ids = [face_id for face_id in member_face_ids if face_id in changed_face_id_set]
        if not changed_member_ids:
            continue

        display_members = list(group["members"])
        if int(group["cluster_label"]) == -1:
            display_members = [member for member in display_members if str(member.get("face_id", "")) in changed_face_id_set]

        base_result_groups.append(
            {
                "cluster_key": str(group["cluster_key"]),
                "cluster_label": int(group["cluster_label"]),
                "member_count": len(display_members),
                "full_member_count": int(group["full_member_count"]),
                "changed_face_count": len(changed_member_ids),
                "target_groups": _relation_summary(member_face_ids, face_to_group=candidate_face_to_group),
                "members": _annotate_members(display_members, compare_face_to_group=candidate_face_to_group, compare_label="新版"),
            }
        )

    def _sort_key(group: dict[str, Any]) -> tuple[int, int, int, str]:
        return (
            1 if int(group.get("cluster_label", -1)) == -1 else 0,
            -int(group.get("changed_face_count", 0)),
            -int(group.get("member_count", 0)),
            str(group.get("cluster_key", "")),
        )

    candidate_result_groups.sort(key=_sort_key)
    base_result_groups.sort(key=_sort_key)

    return {
        "meta": {
            "base_manifest_path": str(base_manifest_path),
            "candidate_manifest_path": str(candidate_manifest_path),
            "changed_face_count": len(changed_face_ids),
            "candidate_changed_group_count": len(candidate_result_groups),
            "base_changed_group_count": len(base_result_groups),
            "base_clusterer": str(base_payload.get("meta", {}).get("clusterer", "HDBSCAN")),
            "candidate_clusterer": str(candidate_payload.get("meta", {}).get("clusterer", "HDBSCAN")),
        },
        "candidate_groups": candidate_result_groups,
        "base_groups": base_result_groups,
    }


def _render_relation_chips(relations: list[dict[str, Any]], heading: str) -> str:
    if not relations:
        return ""

    chips = "".join(
        [
            (
                f"<span class=\"relation-chip\">"
                f"{html.escape(str(item.get('cluster_key', '')))}"
                f" · {int(item.get('face_count', 0))}"
                "</span>"
            )
            for item in relations
        ]
    )
    return f"<div class=\"relation-row\"><strong>{heading}</strong><div class=\"relation-chip-list\">{chips}</div></div>"


def _render_diff_face_cards(members: list[dict[str, Any]]) -> str:
    cards: list[str] = []
    for member in members:
        face_id = html.escape(str(member.get("face_id", "")))
        crop_relpath = html.escape(str(member.get("crop_relpath", "")))
        context_relpath = html.escape(str(member.get("context_relpath", "")))
        quality_score = float(member.get("quality_score", 0.0))
        magface_quality = float(member.get("magface_quality", 0.0))
        prob = member.get("cluster_probability")
        prob_text = "-" if prob is None else f"{float(prob):.3f}"
        compare_label = html.escape(str(member.get("compare_label", "")))
        compare_group_key = html.escape(str(member.get("compare_group_key", "")))

        cards.append(
            f"""
            <article class="face-card">
              <header>
                <strong>{face_id}</strong>
                <span>Q={quality_score:.3f} · M={magface_quality:.2f} · P={prob_text}</span>
              </header>
              <div class="face-meta">{compare_label}：{compare_group_key}</div>
              <div class="thumb-grid">
                <a href="{crop_relpath}" target="_blank"><img src="{crop_relpath}" alt="crop {face_id}"></a>
                <a href="{context_relpath}" target="_blank"><img src="{context_relpath}" alt="context {face_id}"></a>
              </div>
            </article>
            """
        )
    return "".join(cards)


def _render_group_section(groups: list[dict[str, Any]], relation_key: str, relation_heading: str) -> str:
    if not groups:
        return '<p class="empty-state">没有检测到变化分组。</p>'

    blocks: list[str] = []
    for group in groups:
        cluster_key = html.escape(str(group.get("cluster_key", "")))
        cluster_label = int(group.get("cluster_label", -1))
        member_count = int(group.get("member_count", 0))
        full_member_count = int(group.get("full_member_count", member_count))
        changed_face_count = int(group.get("changed_face_count", 0))
        relation_block = _render_relation_chips(list(group.get(relation_key, [])), heading=relation_heading)
        count_text = f"变化样本 {changed_face_count} · 展示 {member_count}"
        if full_member_count != member_count:
            count_text += f" / 原始 {full_member_count}"
        else:
            count_text += f" · 原始 {full_member_count}"

        blocks.append(
            f"""
            <details class="group-panel" open>
              <summary>
                <div>
                  <h3>{cluster_key}</h3>
                  <div class="group-subtitle">label={cluster_label}</div>
                </div>
                <span class="group-count">{count_text}</span>
              </summary>
              <div class="group-body">
                {relation_block}
                <div class="face-grid">
                  {_render_diff_face_cards(list(group.get("members", [])))}
                </div>
              </div>
            </details>
            """
        )
    return "".join(blocks)


def render_cluster_diff_html(payload: dict[str, Any]) -> str:
    meta = payload.get("meta", {})
    candidate_groups = list(payload.get("candidate_groups", []))

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>微簇变化对比</title>
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
      margin: 0 0 12px;
      font-size: 32px;
    }}
    .subtitle {{
      color: var(--muted);
      margin-bottom: 24px;
    }}
    .summary-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-bottom: 24px;
    }}
    .summary-card, .section-card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: var(--shadow);
    }}
    .summary-card {{
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
    .section-card {{
      padding: 18px;
    }}
    .section-card h2 {{
      margin: 0 0 8px;
      font-size: 22px;
    }}
    .section-hint {{
      color: var(--muted);
      margin-bottom: 16px;
      font-size: 14px;
    }}
    .group-panel {{
      border: 1px solid var(--line);
      border-radius: 16px;
      background: #fff;
      margin-bottom: 12px;
      overflow: hidden;
    }}
    .group-panel summary {{
      list-style: none;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 16px;
    }}
    .group-panel summary::-webkit-details-marker {{
      display: none;
    }}
    .group-panel h3 {{
      margin: 0;
      font-size: 18px;
    }}
    .group-subtitle {{
      margin-top: 4px;
      color: var(--muted);
      font-size: 13px;
    }}
    .group-count {{
      background: var(--accent-soft);
      color: var(--accent);
      border-radius: 999px;
      padding: 8px 12px;
      font-size: 13px;
      font-weight: 600;
      white-space: nowrap;
    }}
    .group-body {{
      padding: 0 16px 16px;
    }}
    .relation-row {{
      margin-bottom: 14px;
    }}
    .relation-row strong {{
      display: block;
      margin-bottom: 8px;
      font-size: 14px;
    }}
    .relation-chip-list {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .relation-chip {{
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
    .empty-state {{
      color: var(--muted);
      margin: 0;
    }}
  </style>
</head>
<body>
  <main>
    <h1>微簇变化对比</h1>
    <div class="subtitle">
      仅集中展示相对基线发生变化的第一阶段分组，便于复核 `min_samples=1` 带来的新增合并与噪声流转。
    </div>
    <div class="summary-grid">
      <dl class="summary-card">
        <dt>变化样本</dt>
        <dd>{int(meta.get("changed_face_count", 0))}</dd>
      </dl>
      <dl class="summary-card">
        <dt>新版变化分组</dt>
        <dd>{int(meta.get("candidate_changed_group_count", 0))}</dd>
      </dl>
    </div>
    <section class="section-card">
      <h2>新版变化分组</h2>
      <div class="section-hint">仅保留候选结果主视角，展示发生变化的微簇，以及这些样本在基线中来自哪些分组。</div>
      {_render_group_section(candidate_groups, relation_key="source_groups", relation_heading="基线来源")}
    </section>
  </main>
</body>
</html>
"""


def write_cluster_diff_review(
    base_manifest_path: Path,
    candidate_manifest_path: Path,
    output_html_path: Path,
) -> dict[str, Any]:
    payload = build_cluster_diff_payload(
        base_manifest_path=base_manifest_path,
        candidate_manifest_path=candidate_manifest_path,
        output_html_path=output_html_path,
    )
    output_html_path.parent.mkdir(parents=True, exist_ok=True)
    output_html_path.write_text(render_cluster_diff_html(payload), encoding="utf-8")
    return payload


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="对比两份人脸微簇 review manifest，只输出发生变化的分组页面")
    parser.add_argument("--base-manifest", type=Path, required=True, help="基线 manifest.json")
    parser.add_argument("--candidate-manifest", type=Path, required=True, help="候选 manifest.json")
    parser.add_argument("--output-html", type=Path, required=True, help="输出 HTML 路径")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    payload = write_cluster_diff_review(
        base_manifest_path=args.base_manifest,
        candidate_manifest_path=args.candidate_manifest,
        output_html_path=args.output_html,
    )
    meta = payload.get("meta", {})
    print(
        "完成："
        f"changed_faces={meta.get('changed_face_count')} "
        f"candidate_groups={meta.get('candidate_changed_group_count')} "
        f"base_groups={meta.get('base_changed_group_count')}"
    )
    print(f"HTML: {args.output_html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
