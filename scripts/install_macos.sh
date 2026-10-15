#!/bin/bash
# Create a self-contained source app; Python remains an external prerequisite.
set -euo pipefail
export PATH=/opt/homebrew/bin:/opt/local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME='磁盘空间管理'
BUNDLE_ID='local.disk-space-analyzer'
VERSION='0.2.0'
INSTALL_DIR=''
DESKTOP_DIR="$HOME/Desktop"
REGISTER=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --applications-dir) INSTALL_DIR="${2:?需要目录}"; shift 2 ;;
    --desktop-dir) DESKTOP_DIR="${2:?需要目录}"; shift 2 ;;
    --no-register) REGISTER=0; shift ;;
    --help) printf '%s\n' '安装磁盘空间管理.app；默认 /Applications，不可写时使用 ~/Applications。' '可选：--applications-dir DIR --desktop-dir DIR --no-register'; exit 0 ;;
    *) printf '未知参数：%s\n' "$1" >&2; exit 2 ;;
  esac
done
[[ "$(/usr/bin/uname -s)" == Darwin ]] || { echo '安装入口只支持 macOS。' >&2; exit 1; }
MAC_VERSION="$(/usr/bin/sw_vers -productVersion)"
[[ "${MAC_VERSION%%.*}" -ge 12 ]] || { echo '需要 macOS 12 或更新版本。' >&2; exit 1; }
case "$(/usr/bin/uname -m)" in arm64|x86_64) ;; *) echo '当前架构不受此源码包支持。' >&2; exit 1 ;; esac

ATLAS_RUNTIME=''
for candidate in "${ATLAS_PYTHON:-}" /opt/homebrew/bin/python3 /opt/local/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/3.*/bin/python3; do
  [[ -n "$candidate" && -x "$candidate" && "$candidate" != /usr/bin/python3 ]] || continue
  [[ "$candidate" -ef /usr/bin/python3 ]] && continue
  if "$candidate" -c 'import sys; raise SystemExit(not (sys.version_info >= (3,10)))' >/dev/null 2>&1; then ATLAS_RUNTIME="$candidate"; break; fi
done
if [[ -z "$ATLAS_RUNTIME" ]]; then
  /usr/bin/osascript -e 'display alert "需要 Python 3.10 或更新版本" message "请从 https://www.python.org/downloads/macos/ 手动安装适用于此 Mac 的 Python，然后重新运行安装入口。本程序不会自动下载 Python 或 Xcode。" as warning' 2>/dev/null || true
  echo '请先安装 Python 3.10+：https://www.python.org/downloads/macos/' >&2
  exit 1
fi

if [[ -z "$INSTALL_DIR" ]]; then
  if [[ -d /Applications && -w /Applications ]]; then INSTALL_DIR=/Applications; else INSTALL_DIR="$HOME/Applications"; fi
fi
mkdir -p "$INSTALL_DIR" "$DESKTOP_DIR"
INSTALL_DIR="$(cd "$INSTALL_DIR" && pwd)"
DESKTOP_DIR="$(cd "$DESKTOP_DIR" && pwd)"
DEST="$INSTALL_DIR/$APP_NAME.app"
DESKTOP_LINK="$DESKTOP_DIR/$APP_NAME.app"
if [[ -e "$DEST" || -L "$DEST" ]]; then
  [[ ! -L "$DEST" && -d "$DEST" ]] || { echo "已有同名入口不是本程序，未覆盖：$DEST" >&2; exit 1; }
  OLD_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$DEST/Contents/Info.plist" 2>/dev/null || true)"
  [[ "$OLD_ID" == "$BUNDLE_ID" ]] || { echo "已有同名应用不属于本程序，未覆盖：$DEST" >&2; exit 1; }
fi
if [[ -e "$DESKTOP_LINK" || -L "$DESKTOP_LINK" ]]; then
  [[ -L "$DESKTOP_LINK" ]] || { echo "桌面已有同名文件，未覆盖：$DESKTOP_LINK" >&2; exit 1; }
  OLD_TARGET="$(/usr/bin/readlink "$DESKTOP_LINK")"
  [[ "$OLD_TARGET" == "$DEST" || "$OLD_TARGET" == /Applications/Atlas.app ]] || {
    echo "桌面同名链接指向其他应用，未覆盖：$DESKTOP_LINK" >&2; exit 1;
  }
fi

# A staging app is fully assembled and validated before moving an old install.
STAGE_DIR="$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/disk-space-install.XXXXXX")"
STAGE="$STAGE_DIR/$APP_NAME.app"
cat > "$STAGE_DIR/launcher.applescript" <<'APPLESCRIPT'
on run
  set runtimeScript to (POSIX path of (path to me)) & "Contents/Resources/runtime/启动磁盘空间管理.command"
  try
    do shell script "/bin/bash " & quoted form of runtimeScript
  on error errorMessage
    display alert "磁盘空间管理未能启动" message errorMessage as warning
  end try
end run
APPLESCRIPT
/usr/bin/osacompile -o "$STAGE" "$STAGE_DIR/launcher.applescript"
"$ATLAS_RUNTIME" - "$ROOT" "$STAGE" "$VERSION" "$BUNDLE_ID" <<'PY'
import os, pathlib, plistlib, shutil, sys
root, app = map(pathlib.Path, sys.argv[1:3])
runtime = app / 'Contents/Resources/runtime'
runtime.mkdir(parents=True)
required = ('server.py', 'launcher.py', '启动磁盘空间管理.command',
            'index.html', 'app.js', 'styles.css', 'classic.html', 'classic.js', 'classic.css',
            'static/index.html', 'static/app.js', 'static/styles.css', 'trash_helper.swift')
optional = ('bin/trash-helper-arm64', 'bin/trash-helper-x86_64')
for relative in required + optional:
    source = root / relative
    if relative in optional and not source.exists():
        continue
    if not source.is_file() or source.is_symlink():
        raise SystemExit('缺少或不接受的源码文件：' + str(source))
    target = runtime / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(0o755 if relative.endswith('.command') or relative.startswith('bin/') else 0o644)
info_path = app / 'Contents/Info.plist'
with info_path.open('rb') as handle:
    info = plistlib.load(handle)
info.update(CFBundleIdentifier=sys.argv[4], CFBundleName='磁盘空间管理',
            CFBundleDisplayName='磁盘空间管理', CFBundleShortVersionString=sys.argv[3],
            CFBundleVersion=sys.argv[3], LSMinimumSystemVersion='12.0')
with info_path.open('wb') as handle:
    plistlib.dump(info, handle)
PY
/usr/bin/plutil -lint "$STAGE/Contents/Info.plist"
/usr/bin/codesign --force --deep --sign - "$STAGE"
/usr/bin/codesign --verify --deep "$STAGE"

STAMP="$(/bin/date +%Y%m%d-%H%M%S)-$$"
BACKUP_DIR="${ATLAS_STATE_DIR:-$HOME/Library/Application Support/磁盘空间管理}/install-backups"
mkdir -p "$BACKUP_DIR"
if [[ -e "$DEST" ]]; then
  mv "$DEST" "$BACKUP_DIR/$APP_NAME-$STAMP.app"
  printf '原应用已保留：%s\n' "$BACKUP_DIR/$APP_NAME-$STAMP.app"
fi
if ! mv "$STAGE" "$DEST"; then
  if [[ -d "$BACKUP_DIR/$APP_NAME-$STAMP.app" && ! -e "$DEST" ]]; then
    mv "$BACKUP_DIR/$APP_NAME-$STAMP.app" "$DEST"
  fi
  echo "安装移动失败，暂存内容保留在：$STAGE_DIR" >&2; exit 1
fi
if [[ -L "$DESKTOP_LINK" ]]; then mv "$DESKTOP_LINK" "$BACKUP_DIR/desktop-$STAMP.app"; fi
ln -s "$DEST" "$DESKTOP_LINK"
# Migrate only the exact known legacy shortcut; never follow arbitrary links.
LEGACY_LINK="$DESKTOP_DIR/Atlas.app"
if [[ -L "$LEGACY_LINK" && "$(/usr/bin/readlink "$LEGACY_LINK")" == /Applications/Atlas.app ]]; then
  mv "$LEGACY_LINK" "$BACKUP_DIR/legacy-desktop-$STAMP.app"
fi
[[ -x "$DEST/Contents/MacOS/applet" && -L "$DESKTOP_LINK" ]] || { echo '入口验证失败。' >&2; exit 1; }
if [[ "$REGISTER" == 1 ]]; then
  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$DEST" || true
  /usr/bin/mdimport "$DEST" || true
fi
printf '\n已安装：%s\n桌面入口：%s\nPython：%s\n双击桌面“磁盘空间管理”即可启动。\n' "$DEST" "$DESKTOP_LINK" "$ATLAS_RUNTIME"
printf '源码包可移走；安装后的应用从自身资源运行。安装暂存目录：%s\n' "$STAGE_DIR"
