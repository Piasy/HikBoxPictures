# tests/data 数据说明

## `legacy-v2-small.db`

- 用途：用于验证 v2 旧库升级到 v3 migration 的真实路径。
- 来源：从本地旧库快照抽样裁剪而来（不是空库合成）。
- 体积：约 220KB，便于放入仓库直接用于测试。

### 覆盖的关键表

- `library_source`
- `scan_session` / `scan_session_source` / `scan_checkpoint`
- `photo_asset` / `face_observation` / `face_embedding`
- `person` / `person_face_assignment` / `person_prototype`
- `review_item`
- `export_template` / `export_template_person`
- `schema_migration`（固定为 `1,2,3`）

### 重新生成

```bash
source .venv/bin/activate
python3 tools/build_legacy_v2_fixture.py \
  --source-db .hikbox/.hikbox/library.db \
  --output-db tests/data/legacy-v2-small.db
```

说明：生成脚本默认读取当前仓库下 `.hikbox/.hikbox/library.db`，并按固定抽样集合裁剪为最小可测库。
