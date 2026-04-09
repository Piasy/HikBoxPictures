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

echo "[hikbox-pictures] 升级 pip"
python3 -m pip install --upgrade pip

echo "[hikbox-pictures] 安装项目及开发依赖（包含 insightface、onnxruntime）"
if ! python3 -m pip install -e '.[dev]'; then
  cat >&2 <<'ERR'
安装失败。

请确认网络可用（首次安装 insightface 模型下载需联网），然后重新运行：
  ./scripts/install.sh
ERR
  exit 1
fi

cat <<DONE

安装完成。

提示：首次运行会自动下载 insightface 预训练模型，需联网且速度会稍慢。

激活虚拟环境：
  source "${VENV_DIR}/bin/activate"

运行测试：
  PYTHONPATH=src python3 -m pytest -q
DONE
