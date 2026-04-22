# HikBox Pictures 人物图库产品化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. Executors run dependency-free tasks in parallel when safe (default max concurrency: `5`). All subagents use the controller's current model; implementers use `medium` reasoning and reviewers use `xhigh` reasoning. Execution tracking, checkbox ownership, `Task completion action` timing, and task completion commit behavior follow `superpowers:subagent-driven-development`. Task worktree assignment, merge-back, and cleanup follow `superpowers:using-git-worktrees`.

**Goal:** 将当前单文件原型重构为可长期维护的人物图库产品，实现双库真相层、可恢复扫描、人物维护、导出账本、FastAPI+Jinja2 WebUI 与 CLI 全链路交付。

**Architecture:** 采用“产品内核 + Web/CLI 适配层”结构：`hikbox_pictures/product/` 承载配置、DB、扫描状态机、冻结 v5 引擎、人物与导出服务；`hikbox_pictures/web/` 与 `hikbox_pictures/cli.py` 只做编排与输入输出。扫描执行遵循单活会话与 detect 阶段子进程批处理，主进程单写者提交双库；人物与导出写操作受运行锁约束。所有行为以 `docs/db_schema.md` 与冻结参数快照为准，新增模块化测试覆盖验收清单。

**Tech Stack:** Python 3.12、SQLite（WAL）、FastAPI、Jinja2、Uvicorn、pytest、现有 InsightFace/MagFace/HDBSCAN 管线。

---

## Planned File Structure

- 产品内核：`hikbox_pictures/product/`
- DB 与 schema 启动：`hikbox_pictures/product/db/`
- 扫描与阶段执行：`hikbox_pictures/product/scan/`
- 冻结引擎封装：`hikbox_pictures/product/engine/`
- 人物服务：`hikbox_pictures/product/people/`
- 导出服务：`hikbox_pictures/product/export/`
- 审计与运行日志：`hikbox_pictures/product/audit/`、`hikbox_pictures/product/ops_event.py`
- 服务装配：`hikbox_pictures/product/service_registry.py`
- Web 层：`hikbox_pictures/web/` + `hikbox_pictures/web/templates/`
- CLI 入口：`hikbox_pictures/cli.py`
- 测试：`tests/product/`、`tests/web/`、`tests/cli/`、`tests/integration/`
- 测试数据约定：如测试需要使用真实照片样本，可直接复用 `tests/data/` 下的照片。
- 文档：`docs/db_schema.md`、`README.md`

## Parallel Execution Plan

### Wave A（串行启动）
- 可并行任务：无。
- 执行任务：Task 1。
- 原因：Task 1 建立产品目录与双库初始化能力，后续任务共享。
- 阻塞任务：Task 2-11。
- 解锁条件：Task 1 完成。

### Wave B（扫描会话）
- 可并行任务：无。
- 执行任务：Task 2。
- 原因：Task 2 先建立单活状态机与会话编排，Task 3-5 依赖其语义。
- 阻塞任务：Task 3-11。
- 解锁条件：Task 2 完成。

### Wave C（扫描输入阶段）
- 可并行任务：无。
- 执行任务：Task 3。
- 原因：metadata/fingerprint/live photo 结果是 detect 与 assignment 输入前提。
- 阻塞任务：Task 4-11。
- 解锁条件：Task 3 完成。

### Wave D（detect 阶段）
- 可并行任务：无。
- 执行任务：Task 4。
- 原因：claim/ack 与 worker 协议完成后，才能安全接入后续 embedding/assignment。
- 阻塞任务：Task 5-11。
- 解锁条件：Task 4 完成。

### Wave E（冻结引擎接入）
- 可并行任务：无。
- 执行任务：Task 5。
- 原因：人物/导出/审计均依赖 assignment_run 与冻结参数快照。
- 阻塞任务：Task 6-11。
- 解锁条件：Task 5 完成。

### Wave F（业务域并行）
- 可并行任务：
  - Task 6（人物维护、排除、合并撤销）
  - Task 7（导出模板与导出执行）
  - Task 8（审计采样与 ops 日志）
- 并行理由：三者分别落到 `people/`、`export/`、`audit/` 与对应测试目录，可并发开发。共享装配文件 `hikbox_pictures/product/service_registry.py` 仅允许 Task 8 修改，Task 6 与 Task 7 禁止触碰该文件。
- 冲突回退顺序：若执行中出现共享依赖装配冲突，按 Task 6 -> Task 7 -> Task 8 顺序串行合并。
- 阻塞任务：Task 9-11。
- 解锁条件：Task 6、Task 7、Task 8 完成。

### Wave G（Web 收口）
- 可并行任务：无。
- 执行任务：Task 9。
- 原因：页面与 API 需要绑定 Task 6-8 的服务接口，必须在其后统一集成。
- 阻塞任务：Task 10-11。
- 解锁条件：Task 9 完成。

### Wave H（CLI 收口）
- 可并行任务：无。
- 执行任务：Task 10。
- 原因：CLI `serve start` 依赖 Web app factory；其余命令依赖完整服务层。
- 阻塞任务：Task 11。
- 解锁条件：Task 10 完成。

### Wave I（验收与文档）
- 可并行任务：无。
- 执行任务：Task 11。
- 原因：需要基于全量功能做集成验收、文档同步与最终回归。
- 阻塞任务：无。

### Task 1: 产品骨架与双库初始化

**Depends on:** None

**Scope Budget:**
- Max files: 20
- Estimated files touched: 14
- Max added lines: 1000
- Estimated added lines: 680

**Files:**
- Create: `hikbox_pictures/product/__init__.py`
- Create: `hikbox_pictures/product/config.py`
- Create: `hikbox_pictures/product/db/__init__.py`
- Create: `hikbox_pictures/product/db/connection.py`
- Create: `hikbox_pictures/product/db/schema_bootstrap.py`
- Create: `hikbox_pictures/product/db/schema_meta.py`
- Create: `hikbox_pictures/product/db/sql/library_v1.sql`
- Create: `hikbox_pictures/product/db/sql/embedding_v1.sql`
- Create: `tests/product/test_workspace_init.py`
- Modify: `hikbox_pictures/__init__.py`
- Modify: `docs/db_schema.md`
- Test: `tests/product/test_workspace_init.py`

- [x] **Step 1: 先写初始化失败用例（库与配置文件不存在时应创建）**

```python
def test_init_workspace_creates_two_databases_and_config(tmp_path: Path):
    ws = tmp_path / "ws"
    external = tmp_path / "ext"
    result = initialize_workspace(ws, external)
    assert (ws / ".hikbox" / "library.db").exists()
    assert (ws / ".hikbox" / "embedding.db").exists()
    assert json.loads((ws / ".hikbox" / "config.json").read_text())["external_root"] == str(external)
```

- [x] **Step 2: 运行单测确认当前缺失实现**

Run: `source .venv/bin/activate && pytest tests/product/test_workspace_init.py::test_init_workspace_creates_two_databases_and_config -v`
Expected: FAIL，提示 `initialize_workspace` 未定义或 schema 未创建。

- [x] **Step 3: 实现工作区配置与路径校验**

```python
@dataclass(frozen=True)
class WorkspaceLayout:
    workspace_root: Path
    hikbox_root: Path
    library_db: Path
    embedding_db: Path
    config_json: Path
```

- [x] **Step 4: 实现 library/embedding schema bootstrap（含 schema_meta/embedding_meta 固定键）**

```sql
INSERT INTO schema_meta(key, value, updated_at)
VALUES ('schema_version','1',CURRENT_TIMESTAMP)
ON CONFLICT(key) DO UPDATE SET value=excluded.value;
```

- [x] **Step 5: 对齐 `docs/db_schema.md` 的实现落地说明（路径、初始化策略、版本键）**

Run: `source .venv/bin/activate && rg -n "schema_version|product_schema_name|vector_dim|vector_dtype" docs/db_schema.md hikbox_pictures/product/db/sql/*.sql`
Expected: 文档与 SQL 键名一致。

- [x] **Step 6: 复跑单测并补充已有库复用用例**

Run: `source .venv/bin/activate && pytest tests/product/test_workspace_init.py -v`
Expected: PASS。

### Task 2: 扫描会话状态机与单活冲突语义

**Depends on:** Task 1

**Scope Budget:**
- Max files: 20
- Estimated files touched: 14
- Max added lines: 1000
- Estimated added lines: 930

**Files:**
- Create: `hikbox_pictures/product/scan/__init__.py`
- Create: `hikbox_pictures/product/scan/errors.py`
- Create: `hikbox_pictures/product/scan/models.py`
- Create: `hikbox_pictures/product/scan/session_service.py`
- Create: `hikbox_pictures/product/scan/checkpoint_service.py`
- Create: `hikbox_pictures/product/source/__init__.py`
- Create: `hikbox_pictures/product/source/repository.py`
- Create: `hikbox_pictures/product/source/service.py`
- Create: `tests/product/test_scan_session_service.py`
- Create: `tests/product/test_source_service.py`
- Modify: `hikbox_pictures/product/db/sql/library_v1.sql`
- Modify: `docs/db_schema.md`
- Test: `tests/product/test_scan_session_service.py`
- Test: `tests/product/test_source_service.py`

- [x] **Step 1: 写状态机与 source 行为失败用例（含 start-or-resume 恢复 interrupted、start-new 的 interrupted -> abandoned）**

```python
def test_start_new_conflicts_when_active_session_exists(repo):
    repo.create_session(status="running")
    with pytest.raises(ScanActiveConflictError):
        start_new(repo)

def test_start_or_resume_resumes_latest_interrupted_when_no_active(repo):
    older = repo.create_session(status="interrupted", run_kind="scan_resume")
    latest = repo.create_session(status="interrupted", run_kind="scan_resume")
    before_count = repo.count_sessions()
    resumed = start_or_resume(repo)
    assert resumed.session_id == latest.id
    assert resumed.resumed is True
    assert repo.get_session(latest.id).status == "running"
    assert repo.count_sessions() == before_count

def test_start_new_abandons_interrupted_then_creates_new(repo):
    old = repo.create_session(status="interrupted", run_kind="scan_resume")
    new = start_new(repo)
    assert new.id != old.id
    assert repo.get_session(old.id).status == "abandoned"
    assert repo.get_session(new.id).status in {"pending", "running"}

def test_source_add_disable_enable_relabel_and_remove(source_service, tmp_path):
    root = tmp_path / "family"
    root.mkdir()
    source = source_service.add_source(str(root), label="family")
    source_service.disable_source(source.id)
    source_service.enable_source(source.id)
    source_service.relabel_source(source.id, "family-2026")
    source_service.remove_source(source.id)
    assert source_service.list_sources() == []
```

- [x] **Step 2: 跑失败用例确认状态机与 source 管理尚未实现**

Run: `source .venv/bin/activate && pytest tests/product/test_scan_session_service.py::test_start_new_conflicts_when_active_session_exists tests/product/test_scan_session_service.py::test_start_or_resume_resumes_latest_interrupted_when_no_active tests/product/test_scan_session_service.py::test_start_new_abandons_interrupted_then_creates_new tests/product/test_source_service.py::test_source_add_disable_enable_relabel_and_remove -v`
Expected: FAIL。

- [x] **Step 3: 实现会话服务（start_or_resume/start_new/abort + run_kind 限定）**

```python
ALLOWED_RUN_KIND = {"scan_full", "scan_incremental", "scan_resume"}
ACTIVE_STATUS = {"running", "aborting"}

def start_or_resume(repo):
    active = repo.latest_by_status(ACTIVE_STATUS)
    if active is not None:
        return ScanStartResult(session_id=active.id, resumed=True)
    latest_interrupted = repo.latest_by_status({"interrupted"})
    if latest_interrupted is not None:
        repo.update_status(latest_interrupted.id, "running")
        return ScanStartResult(session_id=latest_interrupted.id, resumed=True)
    new = repo.create_session(run_kind="scan_full", status="running")
    return ScanStartResult(session_id=new.id, resumed=False)

def start_new(repo):
    active = repo.latest_by_status(ACTIVE_STATUS)
    if active is not None:
        raise ScanActiveConflictError(active.id)
    latest_interrupted = repo.latest_by_status({"interrupted"})
    if latest_interrupted is not None:
        repo.update_status(latest_interrupted.id, "abandoned")
    return repo.create_session(run_kind="scan_full", status="pending")
```

- [x] **Step 4: 实现独立 source repository/service（绝对路径校验、root_path 唯一、软删除保护）**

```python
def add_source(root_path: str, label: str | None) -> SourceRecord:
    normalized = validate_absolute_path(root_path)
    return repo.insert_source(root_path=normalized, label=label or Path(normalized).name)
```

- [x] **Step 5: 实现 serve 启动前阻断检查函数**

```python
def assert_no_active_scan_for_serve(repo) -> None:
    if repo.has_active_session():
        raise ServeBlockedByActiveScanError()
```

- [x] **Step 6: 若 SQL 约束调整，更新 `library_v1.sql` 与 `docs/db_schema.md` 同步**

Run: `source .venv/bin/activate && rg -n "scan_session|run_kind|status|library_source|root_path|enabled|label" hikbox_pictures/product/db/sql/library_v1.sql docs/db_schema.md`
Expected: `scan_session` 与 `library_source` 的约束、索引与文档一致。

- [x] **Step 7: 跑状态机与 source 服务全量测试**

Run: `source .venv/bin/activate && pytest tests/product/test_scan_session_service.py tests/product/test_source_service.py -v`
Expected: PASS。

### Task 3: discover/metadata/fingerprint/live photo 输入阶段

**Depends on:** Task 2

**Scope Budget:**
- Max files: 20
- Estimated files touched: 12
- Max added lines: 1000
- Estimated added lines: 980

**Files:**
- Create: `hikbox_pictures/product/scan/discover_stage.py`
- Create: `hikbox_pictures/product/scan/metadata_stage.py`
- Create: `hikbox_pictures/product/scan/fingerprint.py`
- Create: `hikbox_pictures/product/scan/live_photo.py`
- Create: `tests/product/test_discover_incremental.py`
- Create: `tests/product/test_metadata_live_photo.py`
- Create: `tests/product/test_multi_source_discover_flow.py`
- Modify: `hikbox_pictures/product/scan/models.py`
- Modify: `docs/db_schema.md`
- Test: `tests/product/test_discover_incremental.py`
- Test: `tests/product/test_metadata_live_photo.py`
- Test: `tests/product/test_multi_source_discover_flow.py`

- [x] **Step 1: 写 Live Photo 匹配失败用例（仅 HEIC/HEIF，支持两种隐藏 MOV 命名）**

```python
def test_match_live_photo_hidden_mov_patterns(tmp_path: Path):
    still = tmp_path / "IMG_7379.HEIF"
    mov = tmp_path / ".IMG_7379.HEIF_1771856408349261.MOV"
    still.write_bytes(b"x"); mov.write_bytes(b"y")
    result = match_live_mov(still)
    assert result.name == mov.name
```

- [x] **Step 2: 写增量判定失败用例（file_size/mtime_ns 任一变化触发全阶段重跑）**

Run: `source .venv/bin/activate && pytest tests/product/test_discover_incremental.py::test_size_or_mtime_change_requires_full_stage_rerun -v`
Expected: FAIL。

- [x] **Step 3: 写多 source 闭环失败用例（按 source_id 维度维护 discover 与进度）**

```python
def test_discover_tracks_each_source_independently(scan_runner, source_service):
    sid1 = source_service.add_source("/abs/photos/family", "family").id
    sid2 = source_service.add_source("/abs/photos/travel", "travel").id
    summary = scan_runner.run_discover_only()
    assert summary.by_source[sid1].discovered_assets >= 0
    assert summary.by_source[sid2].discovered_assets >= 0
```

- [x] **Step 4: 实现 discover 阶段资产登记与 source 维度状态汇总**

```python
should_rerun = old.file_size != new.file_size or old.mtime_ns != new.mtime_ns
```

- [x] **Step 5: 实现 metadata 时间解析优先级与 capture_month 生成**

```python
capture_month = parsed_dt.strftime("%Y-%m")
```

- [x] **Step 6: 实现 Live Photo 入库字段写入（metadata 阶段完成，导出阶段只读）**

Run: `source .venv/bin/activate && pytest tests/product/test_metadata_live_photo.py tests/product/test_multi_source_discover_flow.py -v`
Expected: PASS。

- [x] **Step 7: 同步文档中的 live_mov_* 字段与阶段语义**

Run: `source .venv/bin/activate && rg -n "live_mov_path|metadata|HEIC|HEIF" docs/db_schema.md`
Expected: 字段与行为描述一致。

### Task 4: detect 阶段批次 claim/ack 与子进程协议

**Depends on:** Task 2, Task 3

**Scope Budget:**
- Max files: 20
- Estimated files touched: 12
- Max added lines: 1000
- Estimated added lines: 980

**Files:**
- Create: `hikbox_pictures/product/scan/detect_stage.py`
- Create: `hikbox_pictures/product/scan/detect_worker.py`
- Create: `hikbox_pictures/product/scan/artifact_writer.py`
- Create: `tests/product/test_detect_batch_claim_ack.py`
- Create: `tests/product/test_detect_worker_contract.py`
- Modify: `hikbox_pictures/product/scan/session_service.py`
- Modify: `hikbox_pictures/product/db/sql/library_v1.sql`
- Modify: `docs/db_schema.md`
- Test: `tests/product/test_detect_batch_claim_ack.py`
- Test: `tests/product/test_detect_worker_contract.py`

- [x] **Step 1: 写失败用例（默认 det_size/workers/batch_size 与批次切分规则）**

```python
defaults = build_scan_runtime_defaults(cpu_count=8)
assert defaults.det_size == 640
assert defaults.batch_size == 300
assert defaults.workers == 4
assert build_scan_runtime_defaults(cpu_count=1).workers == 1
assert split_batch(total=300, workers=3) == [100, 100, 100]
assert split_batch(total=302, workers=3) == [101, 101, 100]
```

- [x] **Step 2: 写失败用例（子进程不得直接写业务真相表）**

Run: `source .venv/bin/activate && pytest tests/product/test_detect_worker_contract.py::test_worker_never_writes_business_tables -v`
Expected: FAIL。

- [x] **Step 3: 实现主进程 claim/dispatch/ack 流程和 scan_batch/scan_batch_item 状态推进**

```python
with repo.transaction():
    batch_id = repo.claim_detect_batch(...)
```

- [x] **Step 4: 实现子进程输出协议与临时文件+rename 产物写入**

```python
tmp_path.replace(final_path)
```

- [x] **Step 5: 实现 abort 场景下未 ack 批次回退与 interrupted 迁移**

Run: `source .venv/bin/activate && pytest tests/product/test_detect_batch_claim_ack.py::test_abort_rolls_back_unacked_batches -v`
Expected: PASS。

- [x] **Step 6: 对齐文档中 detect 阶段唯一使用 claim/ack 的约束，并校验默认值断言通过**

Run: `source .venv/bin/activate && pytest tests/product/test_detect_batch_claim_ack.py::test_default_scan_runtime_values tests/product/test_detect_batch_claim_ack.py::test_split_batch_evenly -v && rg -n "scan_batch|detect|claim|ack" docs/db_schema.md hikbox_pictures/product/scan/*.py`
Expected: 默认值与切分测试 PASS，且非 detect 阶段无 claim/ack 实现。

### Task 5: embed/cluster/assignment 冻结引擎与参数快照

**Depends on:** Task 4

**Scope Budget:**
- Max files: 20
- Estimated files touched: 13
- Max added lines: 1000
- Estimated added lines: 940

**Files:**
- Create: `hikbox_pictures/product/engine/__init__.py`
- Create: `hikbox_pictures/product/engine/param_snapshot.py`
- Create: `hikbox_pictures/product/engine/frozen_v5.py`
- Create: `hikbox_pictures/product/scan/assignment_stage.py`
- Create: `tests/product/test_assignment_run_snapshot.py`
- Create: `tests/product/test_frozen_v5_contract.py`
- Modify: `hikbox_pictures/face_review_pipeline.py`
- Modify: `hikbox_pictures/product/db/sql/library_v1.sql`
- Modify: `docs/db_schema.md`
- Test: `tests/product/test_assignment_run_snapshot.py`
- Test: `tests/product/test_frozen_v5_contract.py`

- [x] **Step 1: 写失败用例（assignment_run 必须记录 algorithm_version 与 param_snapshot_json）**

```python
run = service.start_assignment_run(scan_session_id=1, run_kind="scan_full")
assert run.algorithm_version == "v5.2026-04-21"
assert run.param_snapshot_json["preview_max_side"] == 480
```

- [x] **Step 2: 写失败用例（不允许 `embedding_flip_weight` 参数存在）**

Run: `source .venv/bin/activate && pytest tests/product/test_frozen_v5_contract.py::test_param_snapshot_has_no_embedding_flip_weight -v`
Expected: 初次实现前 FAIL，当前应为 PASS（用于回归防护）。

- [x] **Step 3: 封装冻结链路调用（两遍 AHC + person_consensus + recall）并固定 late fusion=max(main,flip)**

```python
similarity = max(sim_main, sim_flip)
```

- [x] **Step 4: 将 `main/flip` embedding 统一写入 embedding.db 并去除 JSON flip 缓存路径**

Run: `source .venv/bin/activate && pytest tests/product/test_frozen_v5_contract.py::test_main_and_flip_embeddings_persisted_in_embedding_db -v`
Expected: PASS。

- [x] **Step 5: 明确 `noise/low_quality_ignored` 不落 assignment 表**

Run: `source .venv/bin/activate && pytest tests/product/test_assignment_run_snapshot.py::test_noise_and_low_quality_ignored_not_persisted_as_assignment -v`
Expected: PASS。

- [x] **Step 6: 同步 `docs/db_schema.md` 中 assignment_source 与 run 语义描述**

Run: `source .venv/bin/activate && rg -n "assignment_source|assignment_run|param_snapshot_json" docs/db_schema.md`
Expected: 与实现一致。

### Task 6: 人物维护（重命名、排除、批量合并、撤销最近一次）

**Depends on:** Task 5

**Scope Budget:**
- Max files: 20
- Estimated files touched: 7
- Max added lines: 1000
- Estimated added lines: 760

**Files:**
- Create: `hikbox_pictures/product/people/__init__.py`
- Create: `hikbox_pictures/product/people/repository.py`
- Create: `hikbox_pictures/product/people/service.py`
- Create: `tests/product/test_people_exclusion_reassign.py`
- Create: `tests/product/test_people_merge_undo.py`
- Test: `tests/product/test_people_exclusion_reassign.py`
- Test: `tests/product/test_people_merge_undo.py`

- [x] **Step 1: 写失败用例（排除事务必须同时停用 assignment、激活 exclusion、置 pending_reassign=1）**

```python
assert row.active_assignment == 0
assert row.active_exclusion == 1
assert row.pending_reassign == 1
```

- [x] **Step 2: 写失败用例（merge 时迁移 loser exclusion，undo 回滚 delta）**

Run: `source .venv/bin/activate && pytest tests/product/test_people_merge_undo.py::test_merge_migrates_exclusions_and_undo_restores -v`
Expected: FAIL。

- [x] **Step 3: 实现 rename（允许重名）与单条/批量 exclude API 服务函数**

```python
def rename_person(person_id: int, display_name: str) -> PersonView: ...
```

- [x] **Step 4: 实现 merge winner 规则（样本数优先，平局 selected_person_ids[0]）与 delta 快照写入**

Run: `source .venv/bin/activate && pytest tests/product/test_people_merge_undo.py::test_tie_break_uses_first_selected_person_id -v`
Expected: PASS。

- [x] **Step 5: 实现 undo-last-merge 仅回滚“全局最近一次且未撤销”操作**

Run: `source .venv/bin/activate && pytest tests/product/test_people_merge_undo.py::test_only_last_merge_can_be_undone -v`
Expected: PASS。

- [x] **Step 6: 校验现有 schema 已满足人物维护约束（不在本任务改 schema）**

Run: `source .venv/bin/activate && pytest tests/product/test_people_exclusion_reassign.py tests/product/test_people_merge_undo.py -v`
Expected: PASS，且无需新增 schema 变更。

### Task 7: 导出模板与导出执行（only/group、YYYY-MM、Live Photo）

**Depends on:** Task 5

**Scope Budget:**
- Max files: 20
- Estimated files touched: 9
- Max added lines: 1000
- Estimated added lines: 930

**Files:**
- Create: `hikbox_pictures/product/export/__init__.py`
- Create: `hikbox_pictures/product/export/template_service.py`
- Create: `hikbox_pictures/product/export/bucket_rules.py`
- Create: `hikbox_pictures/product/export/run_service.py`
- Create: `tests/product/test_export_bucket_rules.py`
- Create: `tests/product/test_export_run_locking.py`
- Create: `tests/product/test_export_delivery_collision.py`
- Test: `tests/product/test_export_bucket_rules.py`
- Test: `tests/product/test_export_run_locking.py`

- [x] **Step 1: 写失败用例（模板只能选择已命名且 active 人物）**

```python
with pytest.raises(ValidationError):
    update_template_persons(template_id=1, person_ids=[anonymous_person_id])
```

- [x] **Step 2: 写失败用例（only/group 分桶按阈值 selected_min_area/4）**

Run: `source .venv/bin/activate && pytest tests/product/test_export_bucket_rules.py::test_group_bucket_threshold_rule -v`
Expected: FAIL。

- [x] **Step 3: 实现模板 create/list/update（无 delete）与 run 启动**

```python
assert "delete_template" not in ExportTemplateService.__dict__
```

- [x] **Step 4: 实现导出执行（目录 `only/group/YYYY-MM` + 同名冲突 `skipped_exists`）**

Run: `source .venv/bin/activate && pytest tests/product/test_export_delivery_collision.py -v`
Expected: PASS。

- [x] **Step 5: 实现 Live Photo 联动导出与缺失 MOV 静默跳过**

Run: `source .venv/bin/activate && pytest tests/product/test_export_run_locking.py::test_missing_live_mov_is_silently_skipped -v`
Expected: PASS。

- [x] **Step 6: 实现导出运行锁（导出进行中阻断人物归属/合并写）**

Run: `source .venv/bin/activate && pytest tests/product/test_export_run_locking.py::test_people_writes_blocked_while_export_running -v`
Expected: PASS。

### Task 8: 轻量审计采样与 ops 事件查询

**Depends on:** Task 5

**Scope Budget:**
- Max files: 20
- Estimated files touched: 7
- Max added lines: 1000
- Estimated added lines: 620

**Files:**
- Create: `hikbox_pictures/product/audit/__init__.py`
- Create: `hikbox_pictures/product/audit/service.py`
- Create: `hikbox_pictures/product/ops_event.py`
- Create: `tests/product/test_audit_sampling.py`
- Create: `tests/product/test_ops_event_query.py`
- Modify: `hikbox_pictures/product/service_registry.py`
- Test: `tests/product/test_audit_sampling.py`

- [x] **Step 1: 写失败用例（assignment_run 后必须至少产出三类 audit_type 样本）**

```python
assert {i.audit_type for i in items} >= {
    "low_margin_auto_assign", "reassign_after_exclusion", "new_anonymous_person"
}
```

- [x] **Step 2: 写失败用例（ops_event 支持 scan/export 维度过滤）**

Run: `source .venv/bin/activate && pytest tests/product/test_ops_event_query.py::test_filter_by_scan_session_and_export_run -v`
Expected: FAIL。

- [x] **Step 3: 实现审计采样服务并落库 `scan_audit_item`**

```python
def build_audit_items(run_id: int, assignments: list[Assignment]) -> list[AuditItem]: ...
```

- [x] **Step 4: 实现事件记录与分页查询接口（severity/event_type），并在 service_registry 完成审计服务装配**

Run: `source .venv/bin/activate && pytest tests/product/test_ops_event_query.py -v`
Expected: PASS。

- [x] **Step 5: 校验审计功能无需新增 schema 变更（文档统一在 Task 11 收口）**

Run: `source .venv/bin/activate && pytest tests/product/test_audit_sampling.py tests/product/test_ops_event_query.py -v`
Expected: PASS，且 schema 文件无需在本任务修改。

### Task 9: FastAPI + Jinja2 页面与 API 合同落地

**Depends on:** Task 6, Task 7, Task 8

**Scope Budget:**
- Max files: 20
- Estimated files touched: 19
- Max added lines: 1000
- Estimated added lines: 980

**Files:**
- Create: `hikbox_pictures/web/__init__.py`
- Create: `hikbox_pictures/web/app.py`
- Create: `hikbox_pictures/web/page_routes.py`
- Create: `hikbox_pictures/web/api_routes.py`
- Create: `hikbox_pictures/web/templates/base.html`
- Create: `hikbox_pictures/web/templates/people_index.html`
- Create: `hikbox_pictures/web/templates/people_detail.html`
- Create: `hikbox_pictures/web/templates/sources.html`
- Create: `hikbox_pictures/web/templates/audit.html`
- Create: `hikbox_pictures/web/templates/exports.html`
- Create: `hikbox_pictures/web/templates/logs.html`
- Create: `tests/web/test_api_contract.py`
- Create: `tests/web/test_page_render.py`
- Create: `tests/web/test_route_coverage.py`
- Modify: `pyproject.toml`
- Test: `tests/web/test_api_contract.py`
- Test: `tests/web/test_page_render.py`
- Test: `tests/web/test_route_coverage.py`

- [x] **Step 1: 写 API 合同失败用例（spec §15.3 全端点，严格断言成功 `data` 字段 + DB 副作用）**

| 端点 | 失败断言示例 | 成功断言示例（必须断言的 `data` 字段 + DB） |
| --- | --- | --- |
| `POST /api/scan/start_or_resume` | 参数非法时 `ok=false` 且 `error.code=VALIDATION_ERROR` | `data` 至少含 `{session_id,status,resumed}`；DB: 返回 `session_id` 命中 `scan_session.id`，且当恢复中断会话时状态 `interrupted -> running` |
| `POST /api/scan/start_new` | 存在 active 会话时 `ok=false` 且 `error.code=SCAN_ACTIVE_CONFLICT` | `data` 至少含 `{session_id,status}`；DB: 新 `session_id` 存在，若存在最近 interrupted 则旧会话 `-> abandoned` 后再创建新会话 |
| `POST /api/scan/abort` | `session_id` 不存在时 `ok=false` 且 `error.code=SCAN_SESSION_NOT_FOUND` | `data` 精确断言 `{session_id,status:"aborting"}`；DB: 对应会话状态为 `aborting` 且 `updated_at` 变化 |
| `POST /api/people/{id}/actions/rename` | 空名字返回 `ok=false` 且 `error.code=VALIDATION_ERROR` | `data` 精确断言 `{person_id,display_name,is_named}`；DB: `person.display_name` 更新且 `is_named=1` |
| `POST /api/people/{id}/actions/exclude-assignment` | 重复排除同一 observation 返回冲突错误 | `data` 精确断言 `{person_id,face_observation_id,pending_reassign:1}`；DB: `person_face_exclusion` 新增 active 行并触发待重分配语义 |
| `POST /api/people/{id}/actions/exclude-assignments` | 请求体空列表返回 `VALIDATION_ERROR` | `data` 精确断言 `{person_id,excluded_count}`；DB: `person_face_exclusion` 批量新增，计数与 `excluded_count` 一致 |
| `POST /api/people/actions/merge-batch` | `selected_person_ids` 少于 2 个返回 `VALIDATION_ERROR` | `data` 精确断言 `{merge_operation_id,winner_person_id,winner_person_uuid}`；DB: `merge_operation` 新增，且 `winner_person_id` 对应 `person_uuid` 与响应一致 |
| `POST /api/people/actions/undo-last-merge` | 无可撤销 merge 返回 `MERGE_OPERATION_NOT_FOUND` | `data` 精确断言 `{merge_operation_id,status:"undone"}`；DB: `merge_operation.status='undone'`，`*_delta` 回放结果可查询 |
| `GET /api/export/templates` | 非法 `limit` 返回 `VALIDATION_ERROR` | `data` 至少含 `{items:[...]}`；DB: `items` 数量与主键集合与 `export_template` 查询一致 |
| `POST /api/export/templates` | 名称重复返回 `EXPORT_TEMPLATE_DUPLICATE` | `data` 精确断言 `{template_id}`；DB: `export_template.id=template_id` 行存在 |
| `PUT /api/export/templates/{id}` | 模板不存在返回 `EXPORT_TEMPLATE_NOT_FOUND` | `data` 精确断言 `{template_id,updated:true}`；DB: 指定模板字段已更新 |
| `POST /api/export/templates/{id}/actions/run` | 模板不存在返回 `EXPORT_TEMPLATE_NOT_FOUND` | `data` 精确断言 `{export_run_id,status:"running"}`；DB: `export_run.id=export_run_id` 且 `status='running'` |
| `GET /api/scan/{session_id}/audit-items` | `session_id` 不存在返回 `SCAN_SESSION_NOT_FOUND` | `data` 至少含 `{items:[...]}`；DB: `scan_audit_item` 数量与关键字段集合一致 |

Run: `source .venv/bin/activate && pytest tests/web/test_api_contract.py::test_scan_start_or_resume_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_scan_start_new_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_scan_abort_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_people_rename_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_people_exclude_assignment_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_people_exclude_assignments_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_people_merge_batch_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_people_undo_last_merge_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_export_templates_list_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_export_template_create_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_export_template_update_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_export_template_run_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_scan_audit_items_contract_data_fields_and_db_side_effect -v`
Expected: FAIL。

- [x] **Step 2: 写页面失败用例（覆盖 spec 15.1 全量页面路由 + spec §12.5/§12.6 关键交互字段）**

```python
audit_resp = client.get(f"/sources/{session_id}/audit")
audit_dom = BeautifulSoup(audit_resp.text, "html.parser")
session_node = audit_dom.select_one('[data-testid="scan-session-state"]')
assert session_node["data-session-id"] == str(session_id)
assert session_node["data-status"] == "running"
assert session_node["data-failed-count"] == "3"
progress_rows = audit_dom.select('[data-testid="source-progress-row"]')
assert len(progress_rows) == 2
assert progress_rows[0]["data-source-id"] == "1"
assert progress_rows[0]["data-processed"] == "120"
assert progress_rows[0]["data-total"] == "200"
params = audit_dom.select_one('[data-testid="scan-params"]')
assert params["data-det-size"] == "640"
assert params["data-workers"] == "4"
assert params["data-batch-size"] == "300"
assert audit_dom.select_one('[data-testid="scan-action-resume"]')["data-enabled"] == "false"
assert audit_dom.select_one('[data-testid="scan-action-abort"]')["data-enabled"] == "true"
assert audit_dom.select_one('[data-testid="scan-action-abandon-new"]')["data-enabled"] == "true"

exports_resp = client.get("/exports")
exports_dom = BeautifulSoup(exports_resp.text, "html.parser")
template_rows = exports_dom.select('[data-testid="export-template-row"]')
assert [row["data-template-id"] for row in template_rows] == ["11", "12"]
assert exports_dom.select_one('[data-testid="export-template-create"]')["data-enabled"] == "true"
assert exports_dom.select_one('[data-testid="export-template-edit-11"]')["data-enabled"] == "true"
only_stats = exports_dom.select_one('[data-testid="preview-only-stats"]')
group_stats = exports_dom.select_one('[data-testid="preview-group-stats"]')
assert only_stats["data-candidate-count"] == "38"
assert group_stats["data-candidate-count"] == "24"
samples = exports_dom.select('[data-testid="preview-sample-item"]')
assert len(samples) >= 2
history_rows = exports_dom.select('[data-testid="export-run-history-row"]')
assert history_rows[0]["data-status"] == "running"
assert exports_dom.select_one('[data-testid="people-assign-action"]')["data-enabled"] == "false"
assert exports_dom.select_one('[data-testid="people-merge-action"]')["data-enabled"] == "false"
lock_tip = exports_dom.select_one('[data-testid="people-write-lock-tip"]')
assert lock_tip["data-locked"] == "true"
assert "导出运行中" in lock_tip.text
```

Run: `source .venv/bin/activate && pytest tests/web/test_page_render.py::test_sources_audit_page_binds_session_status_source_progress_failure_stats_and_scan_params tests/web/test_page_render.py::test_sources_audit_page_binds_resume_abort_abandon_new_action_states tests/web/test_page_render.py::test_exports_page_binds_template_list_create_edit_preview_history_and_people_lock_semantics tests/web/test_route_coverage.py::test_home_page_route tests/web/test_route_coverage.py::test_people_detail_page_route tests/web/test_route_coverage.py::test_sources_page_route tests/web/test_route_coverage.py::test_sources_audit_page_route tests/web/test_route_coverage.py::test_exports_page_route tests/web/test_route_coverage.py::test_export_detail_page_route tests/web/test_route_coverage.py::test_logs_page_route -v`
Expected: FAIL（未绑定真实数据或交互状态时必须失败）。

- [x] **Step 3: 实现 FastAPI app factory 与页面路由**

```python
def create_app(services: ServiceContainer) -> FastAPI: ...
```

- [x] **Step 4: 实现核心动作 API（scan/people/export/audit）与错误码映射，逐端点保证“字段合同 + DB 副作用”**

Run: `source .venv/bin/activate && pytest tests/web/test_api_contract.py::test_scan_start_or_resume_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_scan_start_new_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_scan_abort_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_people_rename_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_people_exclude_assignment_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_people_exclude_assignments_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_people_merge_batch_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_people_undo_last_merge_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_export_templates_list_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_export_template_create_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_export_template_update_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_export_template_run_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_scan_audit_items_contract_data_fields_and_db_side_effect -v`
Expected: PASS，所有成功路径都按 spec §15.3 的字段断言：`{session_id,status,resumed}`、`{session_id,status}`、`{session_id,status:"aborting"}`、`{person_id,display_name,is_named}`、`{person_id,face_observation_id,pending_reassign:1}`、`{person_id,excluded_count}`、`{merge_operation_id,winner_person_id,winner_person_uuid}`、`{merge_operation_id,status:"undone"}`、`{items:[...]}`、`{template_id}`、`{template_id,updated:true}`、`{export_run_id,status:"running"}`、`{items:[...]}`，并逐条联动 DB 查询。

- [x] **Step 5: 实现导出中禁用人物修改入口、扫描审计摘要展示，并通过 spec 15.1 + spec §12.5/§12.6 页面断言**

Run: `source .venv/bin/activate && pytest tests/web/test_page_render.py::test_sources_audit_page_binds_session_status_source_progress_failure_stats_and_scan_params tests/web/test_page_render.py::test_sources_audit_page_binds_resume_abort_abandon_new_action_states tests/web/test_page_render.py::test_exports_page_binds_template_list_create_edit_preview_history_and_people_lock_semantics tests/web/test_route_coverage.py::test_home_page_route tests/web/test_route_coverage.py::test_people_detail_page_route tests/web/test_route_coverage.py::test_sources_page_route tests/web/test_route_coverage.py::test_sources_audit_page_route tests/web/test_route_coverage.py::test_exports_page_route tests/web/test_route_coverage.py::test_export_detail_page_route tests/web/test_route_coverage.py::test_logs_page_route -v`
Expected: PASS，页面断言必须基于注入测试数据后的 HTML 结构 / `data-*` 字段：扫描页验证会话状态、source 进度、失败统计、`det_size/workers/batch_size` 当前值与恢复/停止/放弃并新建入口状态；导出页验证模板列表、创建/编辑入口、only/group 预览统计与样例、执行历史、导出运行中禁用人物归属/合并入口与提示文案。

- [x] **Step 6: 校验 package-data 覆盖模板目录**

Run: `source .venv/bin/activate && rg -n "web/templates" pyproject.toml`
Expected: 模板路径仍可被打包。

### Task 10: CLI 命令树与退出码实现

**Depends on:** Task 2, Task 6, Task 7, Task 8, Task 9

**Scope Budget:**
- Max files: 20
- Estimated files touched: 16
- Max added lines: 1000
- Estimated added lines: 980

**Files:**
- Create: `hikbox_pictures/cli.py`
- Create: `tests/cli/test_cli_commands.py`
- Create: `tests/cli/test_cli_exit_codes.py`
- Create: `tests/cli/test_cli_init_serve_commands.py`
- Create: `tests/cli/test_cli_people_commands.py`
- Create: `tests/cli/test_cli_audit_source_list_commands.py`
- Create: `tests/cli/test_cli_export_template_commands.py`
- Create: `tests/cli/test_cli_scan_lifecycle_commands.py`
- Create: `tests/cli/test_cli_source_commands.py`
- Create: `tests/cli/test_cli_scan_export_commands.py`
- Create: `tests/cli/test_cli_output_modes.py`
- Create: `tests/cli/test_cli_db_commands.py`
- Modify: `pyproject.toml`
- Modify: `hikbox_pictures/__init__.py`
- Test: `tests/cli/test_cli_commands.py`
- Test: `tests/cli/test_cli_exit_codes.py`
- Test: `tests/cli/test_cli_init_serve_commands.py`
- Test: `tests/cli/test_cli_people_commands.py`
- Test: `tests/cli/test_cli_audit_source_list_commands.py`
- Test: `tests/cli/test_cli_export_template_commands.py`
- Test: `tests/cli/test_cli_scan_lifecycle_commands.py`
- Test: `tests/cli/test_cli_source_commands.py`
- Test: `tests/cli/test_cli_scan_export_commands.py`
- Test: `tests/cli/test_cli_output_modes.py`
- Test: `tests/cli/test_cli_db_commands.py`

- [x] **Step 1: 写 `init` 与 `serve start` 失败用例（必须执行真实命令并断言退出码+stdout/stderr）**

```python
def test_init_creates_workspace_files(cli_bin, tmp_path):
    ws = tmp_path / "ws"
    run = subprocess.run(
        [*cli_cmd, "init", "--workspace", str(ws)],
        text=True, capture_output=True, check=False
    )
    assert run.returncode == 0
    assert (ws / ".hikbox" / "library.db").exists()
    assert (ws / ".hikbox" / "embedding.db").exists()

def test_serve_start_success_path(cli_bin, prepared_workspace, wait_http_ok):
    proc = subprocess.Popen(
        [*cli_cmd, "serve", "start", "--workspace", str(prepared_workspace), "--host", "127.0.0.1", "--port", "38766"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        assert wait_http_ok("http://127.0.0.1:38766/") is True
    finally:
        proc.terminate()
        proc.wait(timeout=5)

def test_serve_start_blocked_when_scan_active(cli_bin, prepared_workspace_with_active_scan):
    run = subprocess.run(
        [*cli_cmd, "serve", "start", "--workspace", str(prepared_workspace_with_active_scan), "--port", "38765"],
        text=True, capture_output=True, check=False
    )
    assert run.returncode == 7
    assert "SERVE_BLOCKED_BY_ACTIVE_SCAN" in (run.stderr + run.stdout)
```

Run: `source .venv/bin/activate && pytest tests/cli/test_cli_init_serve_commands.py::test_init_creates_workspace_files tests/cli/test_cli_init_serve_commands.py::test_serve_start_success_path tests/cli/test_cli_init_serve_commands.py::test_serve_start_blocked_when_scan_active -v`
Expected: FAIL。

- [x] **Step 2: 写 `people` 命令失败用例（list/show/rename/exclude/exclude-batch/merge/undo-last-merge）并做 JSON 字段与 DB 真值逐项比对**

```python
def test_people_commands_have_real_effects(cli_bin, seeded_workspace):
    run_list = subprocess.run([*cli_cmd, "--json", "people", "list", "--workspace", str(seeded_workspace)], text=True, capture_output=True, check=False)
    list_data = json.loads(run_list.stdout)["data"]
    db_total = query_one(seeded_workspace, "SELECT COUNT(*) FROM person WHERE status='active'")[0]
    assert run_list.returncode == 0
    assert list_data["total"] == db_total
    assert len(list_data["items"]) == db_total
    item1 = next(i for i in list_data["items"] if i["person_id"] == 1)
    db_person1 = query_one(seeded_workspace, "SELECT person_uuid, display_name, is_named, status FROM person WHERE id=1")
    assert item1["person_uuid"] == db_person1[0]
    assert item1["display_name"] == db_person1[1]
    assert item1["is_named"] == bool(db_person1[2])
    assert item1["status"] == db_person1[3]

    run_named = subprocess.run([*cli_cmd, "--json", "people", "list", "--named", "--workspace", str(seeded_workspace)], text=True, capture_output=True, check=False)
    named_data = json.loads(run_named.stdout)["data"]
    db_named_total = query_one(seeded_workspace, "SELECT COUNT(*) FROM person WHERE status='active' AND is_named=1")[0]
    assert run_named.returncode == 0
    assert named_data["total"] == db_named_total
    assert all(item["is_named"] is True for item in named_data["items"])
    for item in named_data["items"]:
        assert query_one(seeded_workspace, "SELECT is_named FROM person WHERE id=?", [item["person_id"]])[0] == 1

    run_anonymous = subprocess.run([*cli_cmd, "--json", "people", "list", "--anonymous", "--workspace", str(seeded_workspace)], text=True, capture_output=True, check=False)
    anonymous_data = json.loads(run_anonymous.stdout)["data"]
    db_anonymous_total = query_one(seeded_workspace, "SELECT COUNT(*) FROM person WHERE status='active' AND is_named=0")[0]
    assert run_anonymous.returncode == 0
    assert anonymous_data["total"] == db_anonymous_total
    assert all(item["is_named"] is False for item in anonymous_data["items"])
    for item in anonymous_data["items"]:
        assert query_one(seeded_workspace, "SELECT is_named FROM person WHERE id=?", [item["person_id"]])[0] == 0

    run_show = subprocess.run([*cli_cmd, "--json", "people", "show", "1", "--workspace", str(seeded_workspace)], text=True, capture_output=True, check=False)
    show_data = json.loads(run_show.stdout)["data"]
    assert run_show.returncode == 0
    assert show_data["person_id"] == 1
    assert show_data["person_uuid"] == db_person1[0]
    assert show_data["display_name"] == db_person1[1]
    run_rename = subprocess.run([*cli_cmd, "people", "rename", "1", "family-2026", "--workspace", str(seeded_workspace)], text=True, capture_output=True, check=False)
    assert run_rename.returncode == 0
    assert query_one(seeded_workspace, "SELECT display_name FROM person WHERE id=1")[0] == "family-2026"
    run_exclude = subprocess.run([*cli_cmd, "people", "exclude", "1", "--face-observation-id", "11", "--workspace", str(seeded_workspace)], text=True, capture_output=True, check=False)
    assert run_exclude.returncode == 0
    assert query_one(seeded_workspace, "SELECT COUNT(*) FROM person_face_exclusion WHERE person_id=1 AND face_observation_id=11 AND active=1")[0] == 1
    run_batch = subprocess.run([*cli_cmd, "people", "exclude-batch", "1", "--face-observation-ids", "12,13", "--workspace", str(seeded_workspace)], text=True, capture_output=True, check=False)
    assert run_batch.returncode == 0
    assert query_one(seeded_workspace, "SELECT COUNT(*) FROM person_face_exclusion WHERE person_id=1 AND face_observation_id IN (12,13) AND active=1")[0] == 2
    run_merge = subprocess.run([*cli_cmd, "people", "merge", "--selected-person-ids", "1,2", "--workspace", str(seeded_workspace)], text=True, capture_output=True, check=False)
    assert run_merge.returncode == 0
    merge_id = query_one(seeded_workspace, "SELECT id FROM merge_operation ORDER BY id DESC LIMIT 1")[0]
    assert merge_id is not None
    assert query_one(seeded_workspace, "SELECT COUNT(*) FROM merge_operation_exclusion_delta WHERE merge_operation_id=?", [merge_id])[0] >= 1
    run_undo = subprocess.run([*cli_cmd, "people", "undo-last-merge", "--workspace", str(seeded_workspace)], text=True, capture_output=True, check=False)
    assert run_undo.returncode == 0
    assert query_one(seeded_workspace, "SELECT status FROM merge_operation WHERE id=?", [merge_id])[0] == "undone"
```

Run: `source .venv/bin/activate && pytest tests/cli/test_cli_people_commands.py::test_people_commands_have_real_effects -v`
Expected: FAIL。

- [x] **Step 3: 写 `audit list`、`source list`、`export template list/create/update`、`export run` 失败用例（结构化字段与 DB 真值比对，禁止固定输出）**

```python
def test_audit_source_export_template_and_run(cli_bin, seeded_workspace):
    scan_start = subprocess.run([*cli_cmd, "--json", "scan", "start-or-resume", "--workspace", str(seeded_workspace)], text=True, capture_output=True, check=False)
    session_id = json.loads(scan_start.stdout)["data"]["session_id"]
    audit_list = subprocess.run([*cli_cmd, "--json", "audit", "list", "--scan-session-id", str(session_id), "--workspace", str(seeded_workspace)], text=True, capture_output=True, check=False)
    audit_items = json.loads(audit_list.stdout)["data"]["items"]
    db_audit_count = query_one(seeded_workspace, "SELECT COUNT(*) FROM scan_audit_item WHERE scan_session_id=?", [session_id])[0]
    assert audit_list.returncode == 0
    assert len(audit_items) == db_audit_count
    for item in audit_items:
        assert query_one(
            seeded_workspace,
            "SELECT COUNT(*) FROM scan_audit_item WHERE scan_session_id=? AND audit_type=? AND face_observation_id=? AND person_id IS ?",
            [session_id, item["audit_type"], item["face_observation_id"], item["person_id"]],
        )[0] >= 1

    source_list = subprocess.run([*cli_cmd, "--json", "source", "list", "--workspace", str(seeded_workspace)], text=True, capture_output=True, check=False)
    source_items = json.loads(source_list.stdout)["data"]["items"]
    db_source_count = query_one(seeded_workspace, "SELECT COUNT(*) FROM library_source")[0]
    assert source_list.returncode == 0
    assert len(source_items) == db_source_count
    with sqlite3.connect(seeded_workspace / ".hikbox" / "library.db") as conn:
        db_sources = {
            row[0]: (row[1], row[2], bool(row[3]))
            for row in conn.execute("SELECT id, root_path, label, enabled FROM library_source")
        }
    assert {item["source_id"] for item in source_items} == set(db_sources.keys())
    for item in source_items:
        root_path, label, enabled = db_sources[item["source_id"]]
        assert item["root_path"] == root_path
        assert item["label"] == label
        assert item["enabled"] == enabled

    output_root = (seeded_workspace / "exports" / "named-only").resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    create_tpl = subprocess.run([*cli_cmd, "--json", "export", "template", "create", "--name", "named-only", "--output-root", str(output_root), "--workspace", str(seeded_workspace)], text=True, capture_output=True, check=False)
    assert create_tpl.returncode == 0
    template_id = json.loads(create_tpl.stdout)["data"]["template_id"]
    update_tpl = subprocess.run([*cli_cmd, "export", "template", "update", str(template_id), "--name", "named-only-v2", "--workspace", str(seeded_workspace)], text=True, capture_output=True, check=False)
    assert update_tpl.returncode == 0
    assert query_one(seeded_workspace, "SELECT name FROM export_template WHERE id=?", [template_id])[0] == "named-only-v2"
    list_tpl = subprocess.run([*cli_cmd, "export", "template", "list", "--workspace", str(seeded_workspace)], text=True, capture_output=True, check=False)
    assert list_tpl.returncode == 0 and "named-only-v2" in list_tpl.stdout
    run_export = subprocess.run([*cli_cmd, "export", "run", str(template_id), "--workspace", str(seeded_workspace)], text=True, capture_output=True, check=False)
    assert run_export.returncode == 0
    assert query_one(seeded_workspace, "SELECT COUNT(*) FROM export_run WHERE template_id=?", [template_id])[0] >= 1
```

Run: `source .venv/bin/activate && pytest tests/cli/test_cli_audit_source_list_commands.py tests/cli/test_cli_export_template_commands.py::test_audit_source_export_template_and_run -v`
Expected: FAIL。

- [x] **Step 4: 写 `config/source/scan status|list/export run-status|run-list/logs/db` 失败用例（命令签名严格对齐 spec 15.5）**

```python
def test_scan_export_db_and_output_modes(cli_bin, workspace, photos_dir):
    assert subprocess.run([*cli_cmd, "init", "--workspace", str(workspace)], text=True, capture_output=True, check=False).returncode == 0
    lib_db = workspace / ".hikbox" / "library.db"
    emb_db = workspace / ".hikbox" / "embedding.db"

    set_root = subprocess.run([*cli_cmd, "config", "set-external-root", str(workspace / "ext"), "--workspace", str(workspace)], text=True, capture_output=True, check=False)
    assert set_root.returncode == 0
    show = subprocess.run([*cli_cmd, "--json", "config", "show", "--workspace", str(workspace)], text=True, capture_output=True, check=False)
    show_data = json.loads(show.stdout)["data"]
    assert show.returncode == 0 and show_data["external_root"] == str(workspace / "ext")

    add = subprocess.run([*cli_cmd, "--json", "source", "add", str(photos_dir), "--label", "family", "--workspace", str(workspace)], text=True, capture_output=True, check=False)
    source_id = json.loads(add.stdout)["data"]["source_id"]
    assert subprocess.run([*cli_cmd, "--json", "source", "disable", str(source_id), "--workspace", str(workspace)], text=True, capture_output=True).returncode == 0
    assert query_one(workspace, "SELECT enabled FROM library_source WHERE id=?", [source_id])[0] == 0
    assert subprocess.run([*cli_cmd, "--json", "source", "enable", str(source_id), "--workspace", str(workspace)], text=True, capture_output=True).returncode == 0
    assert query_one(workspace, "SELECT enabled FROM library_source WHERE id=?", [source_id])[0] == 1
    assert subprocess.run([*cli_cmd, "--json", "source", "relabel", str(source_id), "family-2026", "--workspace", str(workspace)], text=True, capture_output=True).returncode == 0
    assert query_one(workspace, "SELECT label FROM library_source WHERE id=?", [source_id])[0] == "family-2026"
    assert subprocess.run([*cli_cmd, "--json", "source", "remove", str(source_id), "--workspace", str(workspace)], text=True, capture_output=True).returncode == 0
    assert query_one(workspace, "SELECT COUNT(*) FROM library_source WHERE id=? AND enabled=1", [source_id])[0] == 0

    start = subprocess.run([*cli_cmd, "--json", "scan", "start-or-resume", "--workspace", str(workspace)], text=True, capture_output=True, check=False)
    session_id = json.loads(start.stdout)["data"]["session_id"]
    with sqlite3.connect(lib_db) as conn:
        conn.execute("INSERT INTO scan_session(run_kind,status,triggered_by,created_at,updated_at) VALUES ('scan_full','completed','manual_cli',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
        conn.execute("INSERT INTO scan_session(run_kind,status,triggered_by,created_at,updated_at) VALUES ('scan_full','completed','manual_cli',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
        conn.commit()
        latest_seed_id = conn.execute("SELECT id FROM scan_session ORDER BY id DESC LIMIT 1").fetchone()[0]

    status_latest = subprocess.run([*cli_cmd, "--json", "scan", "status", "--latest", "--workspace", str(workspace)], text=True, capture_output=True, check=False)
    latest_data = json.loads(status_latest.stdout)["data"]
    assert status_latest.returncode == 0
    assert latest_data["session_id"] == latest_seed_id
    assert latest_data["status"] == query_one(workspace, "SELECT status FROM scan_session WHERE id=?", [latest_seed_id])[0]

    status = subprocess.run([*cli_cmd, "--json", "scan", "status", "--session-id", str(session_id), "--workspace", str(workspace)], text=True, capture_output=True, check=False)
    status_data = json.loads(status.stdout)["data"]
    assert status.returncode == 0 and status_data["session_id"] == session_id
    assert status_data["status"] == query_one(workspace, "SELECT status FROM scan_session WHERE id=?", [session_id])[0]

    scan_list = subprocess.run([*cli_cmd, "--json", "scan", "list", "--limit", "2", "--workspace", str(workspace)], text=True, capture_output=True, check=False)
    scan_items = json.loads(scan_list.stdout)["data"]["items"]
    assert len(scan_items) <= 2
    scan_ids = [item["session_id"] for item in scan_items]
    with sqlite3.connect(lib_db) as conn:
        expected_scan_ids = [row[0] for row in conn.execute("SELECT id FROM scan_session ORDER BY id DESC LIMIT 2").fetchall()]
    assert scan_ids == expected_scan_ids[:len(scan_ids)]

    output_root = (workspace / "exports" / "for-run-status").resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    create_tpl = subprocess.run([*cli_cmd, "--json", "export", "template", "create", "--name", "for-run-status", "--output-root", str(output_root), "--workspace", str(workspace)], text=True, capture_output=True, check=False)
    template_id = json.loads(create_tpl.stdout)["data"]["template_id"]
    run_export = subprocess.run([*cli_cmd, "--json", "export", "run", str(template_id), "--workspace", str(workspace)], text=True, capture_output=True, check=False)
    export_run_id = json.loads(run_export.stdout)["data"]["export_run_id"]
    run_export_2 = subprocess.run([*cli_cmd, "--json", "export", "run", str(template_id), "--workspace", str(workspace)], text=True, capture_output=True, check=False)
    export_run_id_2 = json.loads(run_export_2.stdout)["data"]["export_run_id"]

    output_root_other = (workspace / "exports" / "for-run-status-other").resolve()
    output_root_other.mkdir(parents=True, exist_ok=True)
    create_tpl_other = subprocess.run([*cli_cmd, "--json", "export", "template", "create", "--name", "for-run-status-other", "--output-root", str(output_root_other), "--workspace", str(workspace)], text=True, capture_output=True, check=False)
    template_id_other = json.loads(create_tpl_other.stdout)["data"]["template_id"]
    run_export_other = subprocess.run([*cli_cmd, "--json", "export", "run", str(template_id_other), "--workspace", str(workspace)], text=True, capture_output=True, check=False)
    export_run_id_other = json.loads(run_export_other.stdout)["data"]["export_run_id"]

    run_status = subprocess.run([*cli_cmd, "--json", "export", "run-status", str(export_run_id), "--workspace", str(workspace)], text=True, capture_output=True, check=False)
    run_status_data = json.loads(run_status.stdout)["data"]
    assert run_status_data["export_run_id"] == export_run_id
    assert run_status_data["status"] == query_one(workspace, "SELECT status FROM export_run WHERE id=?", [export_run_id])[0]

    run_list = subprocess.run([*cli_cmd, "--json", "export", "run-list", "--template-id", str(template_id), "--limit", "1", "--workspace", str(workspace)], text=True, capture_output=True, check=False)
    run_items = json.loads(run_list.stdout)["data"]["items"]
    db_template_run_count = query_one(workspace, "SELECT COUNT(*) FROM export_run WHERE template_id=?", [template_id])[0]
    assert len(run_items) == min(1, db_template_run_count)
    if run_items:
        expected_latest_template_run = query_one(workspace, "SELECT id FROM export_run WHERE template_id=? ORDER BY id DESC LIMIT 1", [template_id])[0]
        assert run_items[0]["template_id"] == template_id
        assert run_items[0]["export_run_id"] == expected_latest_template_run

    run_list_other = subprocess.run([*cli_cmd, "--json", "export", "run-list", "--template-id", str(template_id_other), "--limit", "5", "--workspace", str(workspace)], text=True, capture_output=True, check=False)
    run_items_other = json.loads(run_list_other.stdout)["data"]["items"]
    assert all(item["template_id"] == template_id_other for item in run_items_other)
    assert any(item["export_run_id"] == export_run_id_other for item in run_items_other)

    with sqlite3.connect(lib_db) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS vacuum_probe (id INTEGER PRIMARY KEY, payload TEXT)")
        conn.executemany("INSERT INTO vacuum_probe(payload) VALUES (?)", [("x" * 2000,) for _ in range(200)])
        conn.commit()
        conn.execute("DELETE FROM vacuum_probe")
        conn.commit()
        lib_freelist_before = conn.execute("PRAGMA freelist_count").fetchone()[0]
    assert lib_freelist_before > 0
    lib_mtime_before = lib_db.stat().st_mtime_ns
    emb_mtime_before = emb_db.stat().st_mtime_ns
    vacuum = subprocess.run([*cli_cmd, "--json", "db", "vacuum", "--library", "--embedding", "--workspace", str(workspace)], text=True, capture_output=True, check=False)
    assert vacuum.returncode == 0 and json.loads(vacuum.stdout)["ok"] is True
    with sqlite3.connect(lib_db) as conn:
        lib_freelist_after = conn.execute("PRAGMA freelist_count").fetchone()[0]
    assert lib_freelist_after == 0
    assert lib_db.exists() and emb_db.exists()
    assert lib_db.stat().st_mtime_ns > lib_mtime_before
    assert emb_db.stat().st_mtime_ns > emb_mtime_before

    with sqlite3.connect(lib_db) as conn:
        conn.execute(
            "INSERT INTO ops_event(event_type, severity, scan_session_id, export_run_id, payload_json, created_at) VALUES ('cli_probe','warning',?,?,?,CURRENT_TIMESTAMP)",
            [session_id, export_run_id_2, '{"probe":"target-1"}'],
        )
        conn.execute(
            "INSERT INTO ops_event(event_type, severity, scan_session_id, export_run_id, payload_json, created_at) VALUES ('cli_probe','warning',?,?,?,CURRENT_TIMESTAMP)",
            [session_id, export_run_id_2, '{"probe":"target-2"}'],
        )
        conn.execute(
            "INSERT INTO ops_event(event_type, severity, scan_session_id, export_run_id, payload_json, created_at) VALUES ('cli_probe','info',?,?,?,CURRENT_TIMESTAMP)",
            [session_id, export_run_id_2, '{"probe":"info"}'],
        )
        conn.execute(
            "INSERT INTO ops_event(event_type, severity, scan_session_id, export_run_id, payload_json, created_at) VALUES ('cli_probe','warning',?,?,?,CURRENT_TIMESTAMP)",
            [latest_seed_id, export_run_id_other, '{"probe":"other"}'],
        )
        conn.commit()

    logs_filtered = subprocess.run(
        [*cli_cmd, "--json", "logs", "list", "--scan-session-id", str(session_id), "--export-run-id", str(export_run_id_2), "--severity", "warning", "--limit", "1", "--workspace", str(workspace)],
        text=True,
        capture_output=True,
        check=False,
    )
    logs_items = json.loads(logs_filtered.stdout)["data"]["items"]
    db_logs_filtered_count = query_one(
        workspace,
        "SELECT COUNT(*) FROM ops_event WHERE scan_session_id=? AND export_run_id=? AND severity='warning'",
        [session_id, export_run_id_2],
    )[0]
    assert len(logs_items) == min(1, db_logs_filtered_count)
    if logs_items:
        assert logs_items[0]["scan_session_id"] == session_id
        assert logs_items[0]["export_run_id"] == export_run_id_2
        assert logs_items[0]["severity"] == "warning"

    out_json = subprocess.run([*cli_cmd, "--json", "logs", "list", "--workspace", str(workspace)], text=True, capture_output=True).stdout
    out_quiet = subprocess.run([*cli_cmd, "--quiet", "logs", "list", "--workspace", str(workspace)], text=True, capture_output=True).stdout
    assert json.loads(out_json)["ok"] is True and out_quiet.strip() == ""
```

Run: `source .venv/bin/activate && pytest tests/cli/test_cli_source_commands.py tests/cli/test_cli_scan_export_commands.py tests/cli/test_cli_db_commands.py tests/cli/test_cli_output_modes.py -v`
Expected: FAIL。

- [x] **Step 5: 写 `scan start-or-resume` / `scan start-new` / `scan abort <session_id>` 失败用例（含 interrupted 恢复与 abandoned 契约）**

```python
def test_scan_start_or_resume_resumes_latest_interrupted(cli_bin, seeded_workspace):
    older_interrupted = create_scan_session(seeded_workspace, status="interrupted", run_kind="scan_resume")
    latest_interrupted = create_scan_session(seeded_workspace, status="interrupted", run_kind="scan_resume")
    total_before = query_one(seeded_workspace, "SELECT COUNT(*) FROM scan_session")[0]
    resume = subprocess.run([*cli_cmd, "--json", "scan", "start-or-resume", "--workspace", str(seeded_workspace)], text=True, capture_output=True, check=False)
    data = json.loads(resume.stdout)["data"]
    assert resume.returncode == 0
    assert data["resumed"] is True
    assert data["session_id"] == latest_interrupted
    assert query_one(seeded_workspace, "SELECT status FROM scan_session WHERE id=?", [latest_interrupted])[0] == "running"
    assert query_one(seeded_workspace, "SELECT COUNT(*) FROM scan_session")[0] == total_before

    resume_again = subprocess.run([*cli_cmd, "--json", "scan", "start-or-resume", "--workspace", str(seeded_workspace)], text=True, capture_output=True, check=False)
    data_again = json.loads(resume_again.stdout)["data"]
    assert resume_again.returncode == 0
    assert data_again["resumed"] is True
    assert data_again["session_id"] == latest_interrupted
    assert query_one(seeded_workspace, "SELECT id FROM scan_session ORDER BY id DESC LIMIT 1")[0] == latest_interrupted

def test_scan_start_new_and_abort_contract(cli_bin, seeded_workspace):
    old_interrupted = create_scan_session(seeded_workspace, status="interrupted", run_kind="scan_resume")
    start_new_from_interrupted = subprocess.run([*cli_cmd, "--json", "scan", "start-new", "--workspace", str(seeded_workspace)], text=True, capture_output=True, check=False)
    start_new_data = json.loads(start_new_from_interrupted.stdout)["data"]
    assert start_new_from_interrupted.returncode == 0
    assert start_new_data["session_id"] != old_interrupted
    assert start_new_data["resumed"] is False
    assert query_one(seeded_workspace, "SELECT status FROM scan_session WHERE id=?", [old_interrupted])[0] == "abandoned"
    assert query_one(seeded_workspace, "SELECT status FROM scan_session WHERE id=?", [start_new_data["session_id"]])[0] in {"pending", "running"}

    new_conflict = subprocess.run([*cli_cmd, "scan", "start-new", "--workspace", str(seeded_workspace)], text=True, capture_output=True, check=False)
    assert new_conflict.returncode == 4
    assert "SCAN_ACTIVE_CONFLICT" in (new_conflict.stdout + new_conflict.stderr)

    abort_run = subprocess.run([*cli_cmd, "scan", "abort", str(start_new_data["session_id"]), "--workspace", str(seeded_workspace)], text=True, capture_output=True, check=False)
    assert abort_run.returncode == 0
    assert query_one(seeded_workspace, "SELECT status FROM scan_session WHERE id=?", [start_new_data["session_id"]])[0] in {"aborting", "interrupted", "failed"}
```

Run: `source .venv/bin/activate && pytest tests/cli/test_cli_scan_lifecycle_commands.py::test_scan_start_or_resume_resumes_latest_interrupted tests/cli/test_cli_scan_lifecycle_commands.py::test_scan_start_new_and_abort_contract -v`
Expected: FAIL。

- [x] **Step 6: 实现 scan 三命令最小真实语义（第二段闭环：最小真实实现）**

实现要求：
- `scan start-or-resume`：无 active 且存在最近 `interrupted` 会话时，必须恢复该会话（`session_id` 不变），并把 DB 状态 `interrupted -> running`。
- `scan start-or-resume`：恢复 interrupted 时响应需包含 `resumed=true`（或等价字段），且 `session_id` 等于最近 interrupted 会话。
- `scan start-or-resume`：若已存在 active 会话（`running|aborting`），CLI 必须直接返回同一 `session_id` 且 `resumed=true`，并且不新增 `scan_session` 行。
- `scan start-or-resume`：仅在无 active 且无 interrupted 时创建新会话，并返回 `resumed=false`。
- `scan start-new`：当无 active 会话且存在最近 `interrupted` 会话时，必须先把该会话更新为 `abandoned`，再创建新会话（`session_id` 不同）。
- `scan start-new`：存在 `running|aborting` 会话时返回冲突错误码 `4`。
- `scan abort <session_id>`：仅对活动会话置 `aborting` 并记录 `updated_at`；不存在返回 `3`。

- [x] **Step 7: 跑 scan 三命令通过用例（第三段闭环：通过用例）**

Run: `source .venv/bin/activate && pytest tests/cli/test_cli_scan_lifecycle_commands.py -v`
Expected: PASS，包含 `start-or-resume` 的 `interrupted -> running` 迁移、`resumed=true`、返回最近 interrupted 的 `session_id`、active 场景复用同 `session_id`，以及 `start-new` 的 `interrupted -> abandoned` + 新会话断言。

- [x] **Step 8: 做 scan 三命令命令行验证（第四段闭环：退出码+输出+DB 状态）**

Run: `source .venv/bin/activate && python - <<'PY'\nimport json\nimport shutil\nimport sqlite3\nimport subprocess\nimport tomllib\nfrom pathlib import Path\n\npyproject = tomllib.loads(Path('pyproject.toml').read_text(encoding='utf-8'))\nscripts = pyproject.get('project', {}).get('scripts', {})\ncli_name = next((k for k, v in scripts.items() if v == 'hikbox_pictures.cli:cli_entry'), None)\nassert cli_name, 'pyproject 未声明 hikbox_pictures.cli:cli_entry 脚本入口'\nrepo_root = Path('.').resolve()\nvenv_bin = Path('/Users/linker/src/Piasy/HikBoxPictures-3/.venv/bin')\nif not venv_bin.exists():\n    venv_bin = repo_root / '.venv' / 'bin'\npython_bin = venv_bin / 'python'\nassert python_bin.exists(), f'Python 不存在: {python_bin}'\ncli_cmd = [str(python_bin), '-m', 'hikbox_pictures.cli']\n\nws = Path('.tmp/cli/scan-lifecycle-ws')
shutil.rmtree(ws, ignore_errors=True)
subprocess.run([*cli_cmd, 'init', '--workspace', str(ws)], check=True, text=True)\nlib_db = ws / '.hikbox' / 'library.db'\nconn = sqlite3.connect(lib_db)\nconn.execute(\"INSERT INTO scan_session(run_kind,status,triggered_by,created_at,updated_at) VALUES ('scan_resume','interrupted','manual_cli',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)\")\nconn.execute(\"INSERT INTO scan_session(run_kind,status,triggered_by,created_at,updated_at) VALUES ('scan_resume','interrupted','manual_cli',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)\")\nlatest_interrupted_id = conn.execute(\"SELECT id FROM scan_session WHERE status='interrupted' ORDER BY id DESC LIMIT 1\").fetchone()[0]\ncount_before_resume = conn.execute('SELECT COUNT(*) FROM scan_session').fetchone()[0]\nconn.commit()\nconn.close()\n\nresume = subprocess.run([*cli_cmd, '--json', 'scan', 'start-or-resume', '--workspace', str(ws)], text=True, capture_output=True, check=False)\nassert resume.returncode == 0\nresume_data = json.loads(resume.stdout)['data']\nassert resume_data['resumed'] is True\nassert resume_data['session_id'] == latest_interrupted_id\nconn = sqlite3.connect(lib_db)\nstatus_after_resume = conn.execute('SELECT status FROM scan_session WHERE id=?', [latest_interrupted_id]).fetchone()[0]\ncount_after_resume = conn.execute('SELECT COUNT(*) FROM scan_session').fetchone()[0]\nconn.close()\nassert status_after_resume == 'running'\nassert count_after_resume == count_before_resume\n\nresume_again = subprocess.run([*cli_cmd, '--json', 'scan', 'start-or-resume', '--workspace', str(ws)], text=True, capture_output=True, check=False)\nassert resume_again.returncode == 0\nresume_again_data = json.loads(resume_again.stdout)['data']\nassert resume_again_data['session_id'] == latest_interrupted_id\nassert resume_again_data['resumed'] is True\n\nnew_conflict = subprocess.run([*cli_cmd, '--json', 'scan', 'start-new', '--workspace', str(ws)], text=True, capture_output=True, check=False)\nassert new_conflict.returncode == 4\nassert 'SCAN_ACTIVE_CONFLICT' in (new_conflict.stdout + new_conflict.stderr)\n\nabort = subprocess.run([*cli_cmd, '--json', 'scan', 'abort', str(latest_interrupted_id), '--workspace', str(ws)], text=True, capture_output=True, check=False)\nassert abort.returncode == 0\nconn = sqlite3.connect(lib_db)\naborted_status = conn.execute('SELECT status FROM scan_session WHERE id=?', [latest_interrupted_id]).fetchone()[0]\nconn.execute(\"UPDATE scan_session SET status='interrupted' WHERE id=?\", [latest_interrupted_id])\nold_interrupted_for_start_new = latest_interrupted_id\nconn.commit()\nconn.close()\nassert aborted_status in {'aborting', 'interrupted', 'failed'}\n\nstart_new = subprocess.run([*cli_cmd, '--json', 'scan', 'start-new', '--workspace', str(ws)], text=True, capture_output=True, check=False)\nassert start_new.returncode == 0\nstart_new_data = json.loads(start_new.stdout)['data']\nassert start_new_data['resumed'] is False\nassert start_new_data['session_id'] != old_interrupted_for_start_new\nconn = sqlite3.connect(lib_db)\nold_interrupted_status = conn.execute('SELECT status FROM scan_session WHERE id=?', [old_interrupted_for_start_new]).fetchone()[0]\nnew_status = conn.execute('SELECT status FROM scan_session WHERE id=?', [start_new_data['session_id']]).fetchone()[0]\nconn.close()\nassert old_interrupted_status == 'abandoned'\nassert new_status in {'pending', 'running'}\nprint('OK')\nPY`
Expected: 当无 active 且存在 `interrupted` 时，`start-or-resume` 退出码 `0`，返回 `resumed=true` 且 `session_id` 命中最近 interrupted，会话状态 `interrupted -> running` 且不新增会话行；active 时再次 `start-or-resume` 返回同一 `session_id`；active 时 `start-new` 退出码 `4` 且输出 `SCAN_ACTIVE_CONFLICT`；`abort` 退出码 `0`。CLI-only 验证下，`abort` 后若需继续验证“无 active 且存在 interrupted 的 `start-new` 成功路径”，需先模拟扫描主流程收敛：`UPDATE scan_session SET status='interrupted' WHERE id=<aborting_session_id>;`，再执行 `start-new` 并断言旧 interrupted `-> abandoned` 与新会话创建成功。

- [x] **Step 9: 实现 `cli_entry` 与 spec 15.5 全命令树（禁止 no-op 命令壳，逐项核对 config/source/scan/serve/audit/db）**

```python
def cli_entry(argv: list[str] | None = None) -> int: ...

SPEC_15_5_COMMANDS = [
    "config show",
    "config set-external-root <abs_path>",
    "source list|add|remove|enable|disable|relabel",
    "scan start-or-resume|start-new|abort|status|list",
    "serve start [--host] [--port]",
    "audit list --scan-session-id <id>",
    "db vacuum [--library] [--embedding]",
]
```

Run: `source .venv/bin/activate && pytest tests/cli/test_cli_commands.py::test_cli_command_signatures_match_spec_15_5 -v`
Expected: PASS，命令签名与 spec 15.5 逐项一致。

- [x] **Step 10: 实现 `serve start` 成功路径与阻断路径、错误到退出码映射（2/3/4/5/6/7）及 `--json`/`--quiet` 输出切换**

Run: `source .venv/bin/activate && pytest tests/cli/test_cli_init_serve_commands.py tests/cli/test_cli_exit_codes.py::test_validation_not_found_scan_conflict_export_lock_illegal_state_and_serve_block_codes -v`
Expected: PASS。

- [x] **Step 11: 跑关键行为套件（init/serve/people/audit/source/export-template/export/config/scan/db）**

Run: `source /Users/linker/src/Piasy/HikBoxPictures-3/.venv/bin/activate && python -m pytest tests/cli/test_cli_init_serve_commands.py tests/cli/test_cli_people_commands.py tests/cli/test_cli_audit_source_list_commands.py tests/cli/test_cli_export_template_commands.py tests/cli/test_cli_scan_lifecycle_commands.py tests/cli/test_cli_source_commands.py tests/cli/test_cli_scan_export_commands.py tests/cli/test_cli_db_commands.py tests/cli/test_cli_config_commands.py -v`
Expected: PASS，且每个命令都校验退出码与真实状态变更/查询结果。

- [x] **Step 12: 校验 `pyproject.toml` 脚本入口与实际模块一致**

Run: `source /Users/linker/src/Piasy/HikBoxPictures-3/.venv/bin/activate && python -c "import hikbox_pictures.cli as c; print(hasattr(c,'cli_entry'))"`
Expected: 输出 `True`。

- [x] **Step 13: 跑 CLI 全量测试**

Run: `source /Users/linker/src/Piasy/HikBoxPictures-3/.venv/bin/activate && python -m pytest tests/cli/test_cli_commands.py tests/cli/test_cli_exit_codes.py tests/cli/test_cli_init_serve_commands.py tests/cli/test_cli_people_commands.py tests/cli/test_cli_audit_source_list_commands.py tests/cli/test_cli_export_template_commands.py tests/cli/test_cli_scan_lifecycle_commands.py tests/cli/test_cli_source_commands.py tests/cli/test_cli_scan_export_commands.py tests/cli/test_cli_output_modes.py tests/cli/test_cli_db_commands.py tests/cli/test_cli_config_commands.py -v`
Expected: PASS。

### Task 11: 端到端验收清单与文档收口

**Depends on:** Task 9, Task 10

**Scope Budget:**
- Max files: 20
- Estimated files touched: 7
- Max added lines: 1000
- Estimated added lines: 520

**Files:**
- Create: `tests/integration/test_productization_acceptance.py`
- Modify: `README.md`
- Modify: `docs/db_schema.md`
- Modify: `scripts/run_tests.sh`
- Modify: `tests/product/test_workspace_init.py`
- Test: `tests/integration/test_productization_acceptance.py`
- Test: `scripts/run_tests.sh`

- [x] **Step 1: 把 spec 的 22 条验收项拆成 22 个独立测试（AC01-AC22），禁止 `run_check/check_id` 分发与硬编码 helper 返回**

```python
def test_ac03_detect_defaults_persisted_in_db(workspace):
    db = workspace / ".hikbox" / "library.db"
    with sqlite3.connect(db) as conn:
        det_size, batch_size, workers = conn.execute(
            "SELECT det_size, batch_size, workers FROM scan_session ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert det_size == 640
    assert batch_size == 300
    assert workers == max(1, os.cpu_count() // 2)

def test_ac13_homepage_sections_visible(app_services):
    client = TestClient(create_app(app_services))
    resp = client.get("/")
    assert resp.status_code == 200
    assert "已命名人物" in resp.text and "匿名人物" in resp.text
    assert "搜索" not in resp.text

def test_ac21_cli_lock_and_conflict_codes(cli_bin, prepared_workspace):
    run = subprocess.run([*cli_cmd, "serve", "start", "--workspace", str(prepared_workspace)], text=True, capture_output=True, check=False)
    assert run.returncode == 7
    assert "SERVE_BLOCKED_BY_ACTIVE_SCAN" in (run.stdout + run.stderr)
```

- [x] **Step 2: 维护 AC01-AC22 对照表（AC 编号 -> 测试函数 -> 断言来源 + spec 条目）**

| AC 编号 | 测试函数 | 断言来源（含 spec） |
| --- | --- | --- |
| AC01 | `test_ac01_db_schema_constraints_from_sqlite_pragma` | DB（`sqlite3` + PRAGMA/真实表结构），spec §17-01 |
| AC02 | `test_ac02_artifact_layout_on_filesystem` | 文件系统（真实目录结构），spec §17-02 |
| AC03 | `test_ac03_detect_defaults_persisted_in_db` | DB（`scan_session` 真实字段），spec §17-03 |
| AC04 | `test_ac04_stage_execution_modes` | DB + 进程模式记录（真实运行结果），spec §17-04 |
| AC05 | `test_ac05_embeddings_written_to_embedding_db` | DB（`embedding.db` 真实查询），spec §17-05 |
| AC06 | `test_ac06_person_uuid_and_merge_tie_break_rule` | DB（`person` + `merge_operation`），spec §17-06 |
| AC07 | `test_ac07_assignment_source_and_noise_rules_from_db` | DB（`person_face_assignment`），spec §17-07 |
| AC08 | `test_ac08_active_assignment_uniqueness` | DB（active 唯一约束结果），spec §17-08 |
| AC09 | `test_ac09_assignment_run_snapshot_from_db` | DB（`assignment_run`），spec §17-09 |
| AC10 | `test_ac10_param_snapshot_fields` | DB（快照 JSON 字段），spec §17-10 |
| AC11 | `test_ac11_frozen_pipeline_stage_order` | DB（快照 JSON 阶段序），spec §17-11 |
| AC12 | `test_ac12_live_photo_pairing_written_in_metadata` | DB（`photo_asset.live_mov_*`），spec §17-12 |
| AC13 | `test_ac13_homepage_sections_visible` | API（`TestClient GET /`），spec §17-13 |
| AC14 | `test_ac14_nav_items_removed` | API（`TestClient GET /`），spec §17-14 |
| AC15 | `test_ac15_exclusion_reassign_happens_in_next_scan` | CLI + DB（真实命令+真实表），spec §17-15 |
| AC16 | `test_ac16_homepage_has_merge_actions` | API（`TestClient GET /`），spec §17-16 |
| AC17 | `test_ac17_merge_and_undo_restore_exclusion_delta` | CLI + DB（`merge_operation_*_delta`），spec §17-17 |
| AC18 | `test_ac18_export_run_layout_and_collision` | CLI + 文件系统 + DB，spec §17-18 |
| AC19 | `test_ac19_api_cli_contract_routes_and_commands` | API + CLI（真实路由/命令），spec §17-19 |
| AC20 | `test_ac20_audit_items_three_types` | API + DB（`scan_audit_item`），spec §17-20 |
| AC21 | `test_ac21_cli_lock_and_conflict_codes` | CLI（真实退出码与输出），spec §17-21 |
| AC22 | `test_ac22_db_schema_doc_migration_text` | 文档文件文本（`docs/db_schema.md`），spec §17-22 |

- [x] **Step 3: 落地 DB 真实断言（`sqlite3` 查询字段、约束、索引与数据结果）**

Run: `source .venv/bin/activate && pytest tests/integration/test_productization_acceptance.py::test_ac01_db_schema_constraints_from_sqlite_pragma tests/integration/test_productization_acceptance.py::test_ac03_detect_defaults_persisted_in_db tests/integration/test_productization_acceptance.py::test_ac07_assignment_source_and_noise_rules_from_db tests/integration/test_productization_acceptance.py::test_ac09_assignment_run_snapshot_from_db -v`
Expected: PASS，断言均来自真实 SQLite 查询。

- [x] **Step 4: 落地 AC19 的 API+CLI 合同断言（spec §15.3 全核心端点 + CLI 命令面）**

Run: `source .venv/bin/activate && pytest tests/web/test_api_contract.py::test_scan_start_or_resume_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_scan_start_new_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_scan_abort_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_people_rename_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_people_exclude_assignment_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_people_exclude_assignments_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_people_merge_batch_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_people_undo_last_merge_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_export_templates_list_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_export_template_create_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_export_template_update_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_export_template_run_contract_data_fields_and_db_side_effect tests/web/test_api_contract.py::test_scan_audit_items_contract_data_fields_and_db_side_effect tests/cli/test_cli_commands.py::test_cli_command_signatures_match_spec_15_5 tests/integration/test_productization_acceptance.py::test_ac19_api_cli_contract_routes_and_commands tests/integration/test_productization_acceptance.py::test_ac19_api_data_fields_and_db_side_effect_matrix -v`
Expected: PASS，`AC19` 必须逐端点断言 spec §15.3 成功 `data` 字段：`{session_id,status,resumed}`、`{session_id,status}`、`{session_id,status:"aborting"}`、`{person_id,display_name,is_named}`、`{person_id,face_observation_id,pending_reassign:1}`、`{person_id,excluded_count}`、`{merge_operation_id,winner_person_id,winner_person_uuid}`、`{merge_operation_id,status:"undone"}`、`{items:[...]}`、`{template_id}`、`{template_id,updated:true}`、`{export_run_id,status:"running"}`、`{items:[...]}`，并对每条成功分支做 DB 联动验证。

- [x] **Step 5: 落地 CLI 真实断言（执行命令并校验退出码与 stdout/stderr）**

Run: `source .venv/bin/activate && pytest tests/integration/test_productization_acceptance.py::test_ac18_export_run_layout_and_collision tests/integration/test_productization_acceptance.py::test_ac21_cli_lock_and_conflict_codes -v`
Expected: PASS，使用 `subprocess.run` 调用真实 CLI，校验 returncode 与输出内容。

- [x] **Step 6: 加防伪造约束检查（禁止回退到 `run_check` 或硬编码 helper）**

Run: `source .venv/bin/activate && rg -n "run_check\\(|check_id|class AcceptanceContext" tests/integration/test_productization_acceptance.py`
Expected: 无匹配。

Run: `source .venv/bin/activate && rg -n "sqlite3.connect|TestClient\\(|httpx\\.|subprocess.run" tests/integration/test_productization_acceptance.py`
Expected: 命中 DB/API/CLI 真实 I/O 调用。

- [x] **Step 7: 先跑验收集成测试并记录缺口**

Run: `source .venv/bin/activate && pytest tests/integration/test_productization_acceptance.py -v`
Expected: 首次 FAIL，暴露未闭环项。

- [x] **Step 8: 补齐验收缺口并复跑到全绿**

Run: `source .venv/bin/activate && pytest tests/integration/test_productization_acceptance.py -v`
Expected: PASS。

- [x] **Step 9: 更新 `README.md`（安装、初始化、扫描、serve、人物维护、导出、测试命令）并核对 schema 文档一致性**

Run: `source .venv/bin/activate && rg -n "hikbox init|hikbox scan start-or-resume|hikbox serve start|people rename|people merge|export template create|export run|./scripts/run_tests.sh" README.md`
Expected: 命令与 CLI 一致。

Run: `source .venv/bin/activate && rg -n "scan_session|assignment_run|export_template|scan_audit_item|face_embedding" docs/db_schema.md hikbox_pictures/product/db/sql/*.sql`
Expected: 表、字段、枚举、索引描述一致。

- [x] **Step 10: 执行仓库回归测试入口**

Run: `source .venv/bin/activate && ./scripts/run_tests.sh`
Expected: 全量测试 PASS，无新增回归。
