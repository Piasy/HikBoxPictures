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

usage() {
  echo "用法: $0 [--scope backend|frontend] [test_file...]"
  echo ""
  echo "选项:"
  echo "  --scope backend   只运行 backend 用例（排除 test_webui_*_playwright.py）"
  echo "  --scope frontend  只运行 frontend 用例（test_webui_*_playwright.py）"
  echo "  --help, -h        显示帮助"
  echo ""
  echo "参数:"
  echo "  test_file         指定一个或多个测试文件运行"
  echo ""
  echo "示例:"
  echo "  $0                                    # 运行所有用例"
  echo "  $0 --scope backend                    # 只运行 backend 用例"
  echo "  $0 --scope frontend                   # 只运行 frontend 用例"
  echo "  $0 tests/people_gallery/test_xxx.py   # 运行指定文件"
  exit 0
}

# 解析参数
SCOPE=""
TEST_FILES=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope)
      if [[ -z "${2:-}" || "$2" =~ ^-- ]]; then
        echo "错误: --scope 需要 backend 或 frontend 参数" >&2
        exit 1
      fi
      if [[ "$2" != "backend" && "$2" != "frontend" ]]; then
        echo "错误: scope 必须是 backend 或 frontend，得到: $2" >&2
        exit 1
      fi
      SCOPE="$2"
      shift 2
      ;;
    --help|-h)
      usage
      ;;
    *)
      TEST_FILES+=("$1")
      shift
      ;;
  esac
done

# 收集测试文件
FILES=()
if [[ ${#TEST_FILES[@]} -gt 0 ]]; then
  FILES=("${TEST_FILES[@]}")
else
  if [[ "$SCOPE" == "frontend" ]]; then
    while IFS= read -r f; do
      FILES+=("$f")
    done < <(find tests -name 'test_webui_*_playwright.py' | sort)
  elif [[ "$SCOPE" == "backend" ]]; then
    while IFS= read -r f; do
      [[ "$f" =~ test_webui_.*_playwright\.py ]] || FILES+=("$f")
    done < <(find tests -name 'test_*.py' | sort)
  else
    while IFS= read -r f; do
      FILES+=("$f")
    done < <(find tests -name 'test_*.py' | sort)
  fi
fi

if [[ ${#FILES[@]} -eq 0 ]]; then
  echo "没有找到匹配的测试文件" >&2
  exit 1
fi

echo "将运行 ${#FILES[@]} 个测试文件:"
printf '  %s\n' "${FILES[@]}"
echo ""

# 逐个运行测试文件
TOTAL_TESTS=0
TOTAL_FAILED=0
OVERALL_START=$(date +%s)

for file in "${FILES[@]}"; do
  FILE_START=$(date +%s)

  OUTPUT=$("${VENV_PYTHON}" -m pytest -q --tb=short "$file" 2>&1) || true

  FILE_END=$(date +%s)
  FILE_ELAPSED=$((FILE_END - FILE_START))

  echo "$OUTPUT"

  # 解析结果
  PASSED=0
  FAILED=0
  if [[ "$OUTPUT" =~ ([0-9]+)[[:space:]]+passed ]]; then
    PASSED="${BASH_REMATCH[1]}"
  fi
  if [[ "$OUTPUT" =~ ([0-9]+)[[:space:]]+failed ]]; then
    FAILED="${BASH_REMATCH[1]}"
  fi

  TOTAL=$((PASSED + FAILED))
  TOTAL_TESTS=$((TOTAL_TESTS + TOTAL))
  TOTAL_FAILED=$((TOTAL_FAILED + FAILED))

  echo "[结果] $file: 总运行用例数=$TOTAL, 失败用例数=$FAILED, 总耗时=${FILE_ELAPSED}s"
  echo ""
done

OVERALL_END=$(date +%s)
OVERALL_ELAPSED=$((OVERALL_END - OVERALL_START))

echo "===== 全部完成 ====="
echo "文件数: ${#FILES[@]}, 总运行用例数: $TOTAL_TESTS, 失败用例数: $TOTAL_FAILED, 总耗时: ${OVERALL_ELAPSED}s"
