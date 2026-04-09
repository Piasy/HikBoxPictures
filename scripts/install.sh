#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${VENV_DIR:-${ROOT_DIR}/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "[hikbox-pictures] 项目目录: ${ROOT_DIR}"
echo "[hikbox-pictures] 虚拟环境: ${VENV_DIR}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "未找到 Python 可执行文件: ${PYTHON_BIN}" >&2
  echo "请先安装 Python 3.13+，或通过 PYTHON_BIN 指定解释器路径。" >&2
  exit 1
fi

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "[hikbox-pictures] 创建虚拟环境"
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

source "${VENV_DIR}/bin/activate"

echo "[hikbox-pictures] 升级 pip 并安装兼容版本 setuptools"
python3 -m pip install --upgrade pip 'setuptools<81'

echo "[hikbox-pictures] 安装项目及开发依赖"
if ! python3 -m pip install -e '.[dev]'; then
  cat >&2 <<'EOF'
安装失败。

如果错误发生在 dlib 构建阶段，请先执行：
  xcode-select --install

然后重新运行：
  ./scripts/install.sh
EOF
  exit 1
fi

cat <<EOF

安装完成。

激活虚拟环境：
  source "${VENV_DIR}/bin/activate"

运行测试：
  PYTHONPATH=src python3 -m pytest -q
EOF
