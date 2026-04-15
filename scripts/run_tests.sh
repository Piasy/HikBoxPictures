#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="${ROOT_DIR}/.venv/bin/python"

cd "${ROOT_DIR}"

[[ -x "${VENV_PYTHON}" ]] || {
  echo "虚拟环境 Python 不可用: ${VENV_PYTHON}" >&2
  echo "请先执行 ./scripts/install.sh" >&2
  exit 1
}

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
