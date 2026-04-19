#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
VENV_DIR="${VENV_DIR:-${ROOT_DIR}/.venv}"
LOCAL_UV_DIR="${LOCAL_UV_DIR:-${ROOT_DIR}/.tools/uv/bin}"
LOCAL_UV_BIN="${LOCAL_UV_DIR}/uv"
UV_BIN_OVERRIDE="${UV_BIN:-}"
UV_INSTALLER_URL="${UV_INSTALLER_URL:-https://astral.sh/uv/install.sh}"

export UV_CACHE_DIR="${UV_CACHE_DIR:-${ROOT_DIR}/.cache/uv}"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-${ROOT_DIR}/.tools/python}"

log() {
  echo "[hikbox-pictures] $*"
}

fail() {
  echo "$*" >&2
  exit 1
}

download_and_install_uv() {
  mkdir -p "${LOCAL_UV_DIR}"
  log "未检测到 uv，安装到 ${LOCAL_UV_DIR}"
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf "${UV_INSTALLER_URL}" | env UV_UNMANAGED_INSTALL="${LOCAL_UV_DIR}" sh
    return
  fi
  if command -v wget >/dev/null 2>&1; then
    wget -qO- "${UV_INSTALLER_URL}" | env UV_UNMANAGED_INSTALL="${LOCAL_UV_DIR}" sh
    return
  fi
  fail "缺少 curl 或 wget，无法自动安装 uv。"
}

resolve_uv_bin() {
  if [[ -n "${UV_BIN_OVERRIDE}" ]]; then
    [[ -x "${UV_BIN_OVERRIDE}" ]] || fail "指定的 UV_BIN 不可执行: ${UV_BIN_OVERRIDE}"
    echo "${UV_BIN_OVERRIDE}"
    return
  fi
  if [[ -x "${LOCAL_UV_BIN}" ]]; then
    echo "${LOCAL_UV_BIN}"
    return
  fi
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return
  fi

  download_and_install_uv
  [[ -x "${LOCAL_UV_BIN}" ]] || fail "uv 安装后仍不可执行: ${LOCAL_UV_BIN}"
  echo "${LOCAL_UV_BIN}"
}

venv_python() {
  if [[ -x "${VENV_DIR}/bin/python" ]]; then
    echo "${VENV_DIR}/bin/python"
    return
  fi
  if [[ -x "${VENV_DIR}/bin/python3" ]]; then
    echo "${VENV_DIR}/bin/python3"
    return
  fi
  echo "${VENV_DIR}/bin/python"
}

venv_matches_expected_python() {
  local python_bin
  python_bin="$(venv_python)"
  if [[ ! -x "${python_bin}" ]]; then
    return 1
  fi
  "${python_bin}" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)"
}

ACTIVE_UV_BIN="$(resolve_uv_bin)"
VENV_PYTHON="$(venv_python)"

log "项目目录: ${ROOT_DIR}"
log "uv: ${ACTIVE_UV_BIN}"
log "uv 缓存目录: ${UV_CACHE_DIR}"
log "uv Python 目录: ${UV_PYTHON_INSTALL_DIR}"
log "目标虚拟环境: ${VENV_DIR}"

mkdir -p "${UV_CACHE_DIR}" "${UV_PYTHON_INSTALL_DIR}"

log "确保使用 uv 管理的 Python ${PYTHON_VERSION}"
"${ACTIVE_UV_BIN}" python install "${PYTHON_VERSION}" --managed-python

if venv_matches_expected_python; then
  log "复用现有 .venv"
else
  if [[ -d "${VENV_DIR}" ]]; then
    log "重建 .venv，使其绑定 Python ${PYTHON_VERSION}"
    log "执行 uv venv，固定虚拟环境解释器"
    "${ACTIVE_UV_BIN}" venv "${VENV_DIR}" --python "${PYTHON_VERSION}" --managed-python --seed --clear
  else
    log "创建 .venv"
    log "执行 uv venv，固定虚拟环境解释器"
    "${ACTIVE_UV_BIN}" venv "${VENV_DIR}" --python "${PYTHON_VERSION}" --managed-python --seed
  fi
fi

VENV_PYTHON="$(venv_python)"
[[ -x "${VENV_PYTHON}" ]] || fail "虚拟环境 Python 不可用: ${VENV_PYTHON}"

log "安装项目及开发依赖（包含 deepface、tf-keras）"
"${ACTIVE_UV_BIN}" pip install --python "${VENV_PYTHON}" --editable ".[dev]"

log "安装 Playwright Chromium 浏览器"
"${VENV_PYTHON}" -m playwright install chromium

log "准备 Playwright 中文字体"
"${ROOT_DIR}/scripts/setup_playwright_zh_fonts.sh"

cat <<DONE

安装完成。

当前环境说明：
- Python 版本固定为 ${PYTHON_VERSION}
- .venv 由 uv 管理的本地 Python 创建，不依赖系统 Python
- Chromium 浏览器与 Playwright 中文字体已准备完成

常用命令：
  source "${VENV_DIR}/bin/activate"
  ./scripts/run_tests.sh
DONE
