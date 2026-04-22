# HikBox Pictures（人物图库产品化）

## 1. 环境安装

优先使用仓库根目录 `.venv`。

```bash
./scripts/install.sh
source .venv/bin/activate
```

## 2. 工作区初始化

说明：`--workspace`、`--json`、`--quiet` 是全局参数，推荐放在子命令前。

```bash
python -m hikbox_pictures.cli --workspace /abs/path/to/workspace init
python -m hikbox_pictures.cli --workspace /abs/path/to/workspace config set-external-root /abs/path/to/external
python -m hikbox_pictures.cli --workspace /abs/path/to/workspace config show
```

初始化后会创建：

- `workspace/.hikbox/library.db`
- `workspace/.hikbox/embedding.db`
- `workspace/.hikbox/config.json`
- `external/artifacts/{crops,aligned,context}`
- `external/logs`

## 3. 扫描与服务

```bash
python -m hikbox_pictures.cli --workspace /abs/path/to/workspace source add /abs/path/to/photos --label family
python -m hikbox_pictures.cli --workspace /abs/path/to/workspace scan start-or-resume
python -m hikbox_pictures.cli --workspace /abs/path/to/workspace scan status --latest
python -m hikbox_pictures.cli --workspace /abs/path/to/workspace scan list --limit 20
```

扫描进行中启动 Web 会直接失败（退出码 `7`）：

```bash
python -m hikbox_pictures.cli --workspace /abs/path/to/workspace serve start --host 127.0.0.1 --port 8000
```

## 4. 人物维护

```bash
python -m hikbox_pictures.cli --workspace /abs/path/to/workspace people list
python -m hikbox_pictures.cli --workspace /abs/path/to/workspace people rename 11 张三
python -m hikbox_pictures.cli --workspace /abs/path/to/workspace people exclude 11 --face-observation-id 1001
python -m hikbox_pictures.cli --workspace /abs/path/to/workspace people exclude-batch 11 --face-observation-ids 1001,1002
python -m hikbox_pictures.cli --workspace /abs/path/to/workspace people merge --selected-person-ids 11,22,33
python -m hikbox_pictures.cli --workspace /abs/path/to/workspace people undo-last-merge
```

## 5. 导出

```bash
python -m hikbox_pictures.cli --workspace /abs/path/to/workspace export template list
python -m hikbox_pictures.cli --workspace /abs/path/to/workspace export template create --name 家庭合照 --output-root /abs/path/to/exports --person-ids 11,22
python -m hikbox_pictures.cli --workspace /abs/path/to/workspace export template update 1 --name 新名称 --person-ids 11,22
python -m hikbox_pictures.cli --workspace /abs/path/to/workspace export run 1
python -m hikbox_pictures.cli --workspace /abs/path/to/workspace export run-status 1
python -m hikbox_pictures.cli --workspace /abs/path/to/workspace export run-list --template-id 1 --limit 20
```

说明：首版不提供模板删除命令。

## 6. 审计与维护命令

```bash
python -m hikbox_pictures.cli --workspace /abs/path/to/workspace audit list --scan-session-id 1
python -m hikbox_pictures.cli --workspace /abs/path/to/workspace logs list --scan-session-id 1
python -m hikbox_pictures.cli --workspace /abs/path/to/workspace db vacuum
```

## 7. 测试命令

worktree 场景建议优先直接执行：

```bash
./scripts/run_tests.sh
```

如需手动激活 Python 环境，请使用主仓 `.venv` 路径（示例占位）：

```bash
source <repo-root>/.venv/bin/activate
python -m pytest tests/integration/test_productization_acceptance.py -v
```

更多 schema 细节见 `docs/db_schema.md`。

## 8. 命令入口对照

安装后默认 console script 为 `hikbox-pictures`，与 `python -m hikbox_pictures.cli` 等价。

```bash
hikbox-pictures --workspace /abs/path/to/workspace init
hikbox-pictures --workspace /abs/path/to/workspace scan start-or-resume
hikbox-pictures --workspace /abs/path/to/workspace serve start --host 127.0.0.1 --port 8000
hikbox-pictures --workspace /abs/path/to/workspace people rename 11 张三
hikbox-pictures --workspace /abs/path/to/workspace people merge --selected-person-ids 11,22,33
hikbox-pictures --workspace /abs/path/to/workspace export template create --name 家庭合照 --output-root /abs/path/to/exports --person-ids 11,22
hikbox-pictures --workspace /abs/path/to/workspace export run 1
./scripts/run_tests.sh
```
