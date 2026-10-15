#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
if ! /bin/bash "$ROOT/scripts/install_macos.sh" "$@"; then
  printf '\n安装没有完成，请查看上方提示。\n' >&2
  if [[ -t 0 ]]; then read -r -p '按回车关闭窗口…' ignored; fi
  exit 1
fi
if [[ -t 0 ]]; then read -r -p '安装完成，按回车关闭窗口…' ignored; fi
