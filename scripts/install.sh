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

if ! "${PYTHON_BIN}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)'; then
  echo "Python 版本过低：${PYTHON_BIN} 需要 >= 3.13。" >&2
  echo "请安装 Python 3.13+，或通过 PYTHON_BIN 指定符合要求的解释器。" >&2
  exit 1
fi

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "[hikbox-pictures] 创建虚拟环境"
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

source "${VENV_DIR}/bin/activate"

VENV_PYTHON="${VENV_DIR}/bin/python3"
if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo "虚拟环境 Python 不可用: ${VENV_PYTHON}" >&2
  exit 1
fi

echo "[hikbox-pictures] 升级 pip"
"${VENV_PYTHON}" -m pip install --upgrade pip

echo "[hikbox-pictures] 安装项目及开发依赖（包含 deepface、tf-keras）"
if ! "${VENV_PYTHON}" -m pip install -e '.[dev]'; then
  cat >&2 <<'ERR'
安装失败。

请确认：
1) Python 版本满足 3.13+
2) 网络可用（首次安装 deepface、tf-keras 与相关模型时需联网）
3) 系统依赖安装完整（如 TensorFlow / OpenCV 的平台依赖）

然后重新运行：
  ./scripts/install.sh
ERR
  exit 1
fi

cat <<DONE

安装完成。

提示：安装脚本会一并安装 deepface 与 tf-keras；首次运行仍可能触发模型下载，需联网，首次启动会明显慢于后续运行。

激活虚拟环境：
  source "${VENV_DIR}/bin/activate"

运行测试：
  PYTHONPATH=src python3 -m pytest -q
DONE
