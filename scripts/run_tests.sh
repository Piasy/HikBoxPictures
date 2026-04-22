#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKTREE_VENV_PYTHON="${ROOT_DIR}/.venv/bin/python"

GIT_COMMON_DIR_RAW="$(cd "${ROOT_DIR}" && git rev-parse --git-common-dir 2>/dev/null || true)"
REPO_ROOT_VENV_PYTHON=""
if [[ -n "${GIT_COMMON_DIR_RAW}" ]]; then
  if [[ "${GIT_COMMON_DIR_RAW}" = /* ]]; then
    GIT_COMMON_DIR="${GIT_COMMON_DIR_RAW}"
  else
    GIT_COMMON_DIR="${ROOT_DIR}/${GIT_COMMON_DIR_RAW}"
  fi
  MAIN_REPO_ROOT="$(cd "${GIT_COMMON_DIR}/.." && pwd)"
  REPO_ROOT_VENV_PYTHON="${MAIN_REPO_ROOT}/.venv/bin/python"
fi

cd "${ROOT_DIR}"

if [[ -x "${WORKTREE_VENV_PYTHON}" ]]; then
  VENV_PYTHON="${WORKTREE_VENV_PYTHON}"
elif [[ -n "${REPO_ROOT_VENV_PYTHON}" && -x "${REPO_ROOT_VENV_PYTHON}" ]]; then
  VENV_PYTHON="${REPO_ROOT_VENV_PYTHON}"
else
  echo "虚拟环境 Python 不可用" >&2
  echo "已尝试: ${WORKTREE_VENV_PYTHON}" >&2
  if [[ -n "${REPO_ROOT_VENV_PYTHON}" ]]; then
    echo "已尝试: ${REPO_ROOT_VENV_PYTHON}" >&2
  else
    echo "无法通过 git rev-parse --git-common-dir 推导主仓路径" >&2
  fi
  echo "请先执行 ./scripts/install.sh" >&2
  exit 1
fi

if [[ -n "${PYTHONPATH:-}" ]]; then
  export PYTHONPATH="${ROOT_DIR}/src:${PYTHONPATH}"
else
  export PYTHONPATH="${ROOT_DIR}/src"
fi
if [[ -z "${RUN_PLAYWRIGHT_VISUAL:-}" ]]; then
  export RUN_PLAYWRIGHT_VISUAL=1
else
  export RUN_PLAYWRIGHT_VISUAL
fi

"${VENV_PYTHON}" -m pytest -q -ra "$@"
