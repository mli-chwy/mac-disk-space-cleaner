#!/bin/bash
set -euo pipefail
export PATH=/opt/homebrew/bin:/opt/local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
ROOT="$(cd "$(dirname "$0")" && pwd)"

find_python() {
  local candidate
  for candidate in "${ATLAS_PYTHON:-}" /opt/homebrew/bin/python3 /opt/local/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/3.*/bin/python3; do
    [[ -n "$candidate" && -x "$candidate" && "$candidate" != /usr/bin/python3 ]] || continue
    [[ "$candidate" -ef /usr/bin/python3 ]] && continue
    if "$candidate" -c 'import sys; raise SystemExit(not (sys.version_info >= (3,10)))' >/dev/null 2>&1; then
      printf '%s\n' "$candidate"; return 0
    fi
  done
  return 1
}

if ! ATLAS_RUNTIME="$(find_python)"; then
  /usr/bin/osascript -e 'display alert "需要 Python 3.10 或更新版本" message "请从 https://www.python.org/downloads/macos/ 手动安装适用于此 Mac 的 Python，然后重新打开磁盘空间管理。本程序不会自动下载依赖，也不会启动 Xcode 安装。" as warning' 2>/dev/null || true
  printf '%s\n' '需要 Python 3.10+：https://www.python.org/downloads/macos/' >&2
  exit 1
fi
exec "$ATLAS_RUNTIME" "$ROOT/launcher.py" "$@"
