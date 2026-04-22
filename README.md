# HikBox Pictures（人物图库产品化）

## 1. 环境安装

优先使用仓库根目录 `.venv`。

```bash
./scripts/install.sh
source .venv/bin/activate
```

## 2. 工作区初始化

```bash
python -m hikbox_pictures.cli init --workspace /abs/path/to/workspace
python -m hikbox_pictures.cli config set-external-root /abs/path/to/external --workspace /abs/path/to/workspace
python -m hikbox_pictures.cli config show --workspace /abs/path/to/workspace
```

初始化后会创建：

- `workspace/.hikbox/library.db`
- `workspace/.hikbox/embedding.db`
- `workspace/.hikbox/config.json`
- `external/artifacts/{crops,aligned,context}`
- `external/logs`

## 3. 扫描与服务

```bash
python -m hikbox_pictures.cli source add /abs/path/to/photos --label family --workspace /abs/path/to/workspace
python -m hikbox_pictures.cli scan start-or-resume --workspace /abs/path/to/workspace
python -m hikbox_pictures.cli scan status --latest --workspace /abs/path/to/workspace
python -m hikbox_pictures.cli scan list --limit 20 --workspace /abs/path/to/workspace
```

扫描进行中启动 Web 会直接失败（退出码 `7`）：

```bash
python -m hikbox_pictures.cli serve start --workspace /abs/path/to/workspace --host 127.0.0.1 --port 8000
```

## 4. 人物维护

```bash
python -m hikbox_pictures.cli people list --workspace /abs/path/to/workspace
python -m hikbox_pictures.cli people rename 11 张三 --workspace /abs/path/to/workspace
python -m hikbox_pictures.cli people exclude 11 --face-observation-id 1001 --workspace /abs/path/to/workspace
python -m hikbox_pictures.cli people exclude-batch 11 --face-observation-ids 1001,1002 --workspace /abs/path/to/workspace
python -m hikbox_pictures.cli people merge --selected-person-ids 11,22,33 --workspace /abs/path/to/workspace
python -m hikbox_pictures.cli people undo-last-merge --workspace /abs/path/to/workspace
```

## 5. 导出

```bash
python -m hikbox_pictures.cli export template list --workspace /abs/path/to/workspace
python -m hikbox_pictures.cli export template create --name 家庭合照 --output-root /abs/path/to/exports --person-ids 11,22 --workspace /abs/path/to/workspace
python -m hikbox_pictures.cli export template update 1 --name 新名称 --person-ids 11,22 --workspace /abs/path/to/workspace
python -m hikbox_pictures.cli export run 1 --workspace /abs/path/to/workspace
python -m hikbox_pictures.cli export run-status 1 --workspace /abs/path/to/workspace
python -m hikbox_pictures.cli export run-list --template-id 1 --limit 20 --workspace /abs/path/to/workspace
```

说明：首版不提供模板删除命令。

## 6. 审计与维护命令

```bash
python -m hikbox_pictures.cli audit list --scan-session-id 1 --workspace /abs/path/to/workspace
python -m hikbox_pictures.cli logs list --scan-session-id 1 --workspace /abs/path/to/workspace
python -m hikbox_pictures.cli db vacuum --workspace /abs/path/to/workspace
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
hikbox-pictures init --workspace /abs/path/to/workspace
hikbox-pictures scan start-or-resume --workspace /abs/path/to/workspace
hikbox-pictures serve start --workspace /abs/path/to/workspace --host 127.0.0.1 --port 8000
hikbox-pictures people rename 11 张三 --workspace /abs/path/to/workspace
hikbox-pictures people merge --selected-person-ids 11,22,33 --workspace /abs/path/to/workspace
hikbox-pictures export template create --name 家庭合照 --output-root /abs/path/to/exports --person-ids 11,22 --workspace /abs/path/to/workspace
hikbox-pictures export run 1 --workspace /abs/path/to/workspace
./scripts/run_tests.sh
```
