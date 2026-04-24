# HikBox Pictures

HikBox Pictures 是一个面向本地照片库的人物图库工具，提供工作区初始化、扫描、Web 浏览、人物维护和按人物导出能力。

## 安装

推荐直接使用仓库脚本准备环境：

```bash
./scripts/install.sh
source .venv/bin/activate
```

如果是首次运行浏览器相关用例，再执行：

```bash
./scripts/setup_playwright_zh_fonts.sh
python3 -m playwright install chromium
```

## 初始化

先创建 workspace，再按需添加照片源目录：

```bash
hikbox init --workspace ./workspace --external-root ./external
hikbox source add /abs/path/to/photos --label family --workspace ./workspace
```

## 扫描

日常扫描入口使用 `start-or-resume`：

```bash
hikbox scan start-or-resume --workspace ./workspace --run-kind scan_full
hikbox scan start-or-resume --workspace ./workspace --run-kind scan_incremental
```

如需查看最近一次扫描状态：

```bash
hikbox scan status --latest --workspace ./workspace
```

## 启动服务

启动本地 Web 服务：

```bash
hikbox serve start --workspace ./workspace --host 127.0.0.1 --port 8000
```

## 人物维护

常用人物维护命令：

```bash
hikbox people rename 1 "Alice" --workspace ./workspace
hikbox people merge --selected-person-ids 1,2 --workspace ./workspace
```

## 导出

先创建导出模板，再创建导出运行：

```bash
hikbox export template create --name family --output-root /abs/path/to/exports --person-ids 1,2 --workspace ./workspace
hikbox export run 1 --workspace ./workspace
```

`export run` 只负责创建一条运行记录；真正执行导出需要显式调用：

```bash
hikbox export execute 1 --workspace ./workspace
```

如需查询单次运行状态：

```bash
hikbox export run-status 1 --workspace ./workspace
```

如需查看导出记录：

```bash
hikbox export run-list --workspace ./workspace
```

## 测试

仓库回归入口：

```bash
./scripts/run_tests.sh
```

默认会包含 `tests/integration/test_real_data_e2e_face_input.py` 这条基于 `tests/data/e2e-face-input` 的真实样本全链路用例。

如果只想跑本次产品化验收骨架：

```bash
source .venv/bin/activate
python -m pytest tests/integration/test_productization_acceptance.py -q
```

如果只想单独复现真实样本全链路集成：

```bash
source .venv/bin/activate
python -m pytest tests/integration/test_real_data_e2e_face_input.py -q
```
