#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FONT_ROOT="${1:-${ROOT_DIR}/.cache/playwright-fonts}"
FONT_DIR="${FONT_ROOT}/fonts"
CONF_DIR="${FONT_ROOT}/fontconfig"
CONF_FILE="${CONF_DIR}/fonts.conf"
FONT_FILE="${FONT_DIR}/NotoSansCJKsc-Regular.otf"
FONT_URL="${FONT_URL:-https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf}"
FALLBACK_URL_1="https://raw.githubusercontent.com/googlefonts/noto-cjk/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf"
FALLBACK_URL_2="https://github.com/notofonts/noto-cjk/raw/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf"

mkdir -p "${FONT_DIR}" "${CONF_DIR}"

download_font() {
  local url="$1"
  if command -v curl >/dev/null 2>&1; then
    curl -L --fail --connect-timeout 8 --max-time 180 --retry 2 --retry-delay 1 -o "${FONT_FILE}" "${url}"
  elif command -v wget >/dev/null 2>&1; then
    wget --timeout=8 -O "${FONT_FILE}" "${url}"
  else
    echo "缺少 curl/wget，无法下载字体。" >&2
    exit 1
  fi
}

if [[ ! -s "${FONT_FILE}" ]] || [[ "$(wc -c < "${FONT_FILE}")" -lt 1000000 ]]; then
  rm -f "${FONT_FILE}"
  for url in "${FONT_URL}" "${FALLBACK_URL_1}" "${FALLBACK_URL_2}"; do
    echo "[playwright-fonts] 下载中文字体: ${url}"
    if download_font "${url}"; then
      if [[ -s "${FONT_FILE}" ]] && [[ "$(wc -c < "${FONT_FILE}")" -ge 1000000 ]]; then
        break
      fi
    fi
    rm -f "${FONT_FILE}"
  done
fi

if [[ ! -s "${FONT_FILE}" ]] || [[ "$(wc -c < "${FONT_FILE}")" -lt 1000000 ]]; then
  echo "下载字体失败或文件不完整: ${FONT_FILE}" >&2
  exit 1
fi

cat > "${CONF_FILE}" <<EOF
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <dir>${FONT_DIR}</dir>
  <alias>
    <family>sans-serif</family>
    <prefer>
      <family>Noto Sans CJK SC</family>
      <family>Noto Sans CJK JP</family>
      <family>Noto Sans CJK TC</family>
      <family>Noto Sans CJK KR</family>
    </prefer>
  </alias>
  <alias>
    <family>sans</family>
    <prefer>
      <family>Noto Sans CJK SC</family>
    </prefer>
  </alias>
</fontconfig>
EOF

fc-cache -f "${FONT_DIR}" >/dev/null 2>&1 || true

echo "[playwright-fonts] 已就绪"
echo "FONT_DIR=${FONT_DIR}"
echo "FONTCONFIG_FILE=${CONF_FILE}"
echo "验证命令:"
echo "  FONTCONFIG_FILE='${CONF_FILE}' fc-match 'sans:lang=zh-cn'"
