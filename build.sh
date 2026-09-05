#!/bin/bash
# Build "Dupe Demon.app" and a drag-install DMG via PyInstaller.
# Run:  chmod +x build.sh && ./build.sh
set -euo pipefail
cd "$(dirname "$0")"

APP="Dupe Demon"
PY=".venv/bin/python"

MACOS_VER=$(sw_vers -productVersion)
echo "==> macOS $MACOS_VER build"

# Find a usable non-conda Python 3.9+. Prefer python.org framework installer,
# fall back to Homebrew if present, then PATH. Reject conda (@rpath libs
# PyInstaller can't bundle).
find_python() {
  local cand real
  for v in 3.14 3.13 3.12 3.11 3.10 3.9; do
    cand="/Library/Frameworks/Python.framework/Versions/$v/bin/python$v"
    [ -x "$cand" ] && echo "$cand" && return 0
  done
  for v in 3.14 3.13 3.12 3.11 3.10; do
    cand="/usr/local/opt/python@$v/bin/python$v"
    [ -x "$cand" ] && echo "$cand" && return 0
    cand="/opt/homebrew/opt/python@$v/bin/python$v"
    [ -x "$cand" ] && echo "$cand" && return 0
  done
  if command -v python3 >/dev/null 2>&1; then
    cand=$(command -v python3)
    real=$("$cand" -c 'import sys; print(sys.executable)' 2>/dev/null || echo "")
    if ! echo "$real" | grep -qiE 'conda|miniconda|anaconda'; then
      echo "$cand" && return 0
    fi
  fi
  return 1
}

BOOTSTRAP=$(find_python) || {
  echo "ERROR: no usable Python 3.9+ found."
  echo "       Install python.org 3.12 (https://www.python.org/downloads/macos/)"
  exit 1
}
echo "    using $BOOTSTRAP ($($BOOTSTRAP --version))"

echo "==> venv + dependencies"
if [ -x "$PY" ]; then
  VENV_REAL=$($PY -c 'import sys; print(sys.executable)' 2>/dev/null || echo none)
  if echo "$VENV_REAL" | grep -qiE 'conda|miniconda|anaconda'; then
    echo "    conda venv detected — recreating"
    rm -rf .venv
  fi
fi
[ -d .venv ] || "$BOOTSTRAP" -m venv .venv
$PY -m pip install --quiet --upgrade pip
$PY -m pip install --quiet pyinstaller pillow tkinterdnd2
$PY -c 'import tkinter' 2>/dev/null || {
  echo "ERROR: this Python has no tkinter."
  exit 1
}

echo "==> back up source"
mkdir -p versions
STAMP=$(date +%Y%m%d_%H%M%S)
cp dupe_demon.py "versions/dupe_demon_${STAMP}.py"

VERSION=$(grep -m1 '__version__' dupe_demon.py | sed 's/.*"\(.*\)"/\1/')
echo "    version $VERSION"

echo "==> self-test (import smoke)"
$PY -c "import dupe_demon; print(f'Dupe Demon {dupe_demon.__version__} — imports OK')"

echo "==> PyInstaller"
rm -rf build dist
$PY -m PyInstaller --noconfirm "Dupe Demon.spec"

echo "==> strip ad-hoc signature"
# PyInstaller ad-hoc signs by default, and that signature's hash changes on
# every rebuild (no paid Developer ID here). macOS TCC ties Automation
# grants to that hash — after enough rebuilds under active development, TCC
# can end up in a state where it won't even show the permission prompt for
# the bundle ID anymore (not denied, just never asked). Fully unsigned
# doesn't hit this; Gatekeeper's already-required "right-click > Open" for
# an unnotarized app covers the same "did the user mean to run this"
# check, so there's no security downside here.
codesign --remove-signature "dist/$APP.app" 2>/dev/null || true

echo "==> stamp version"
PLIST="dist/$APP.app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $VERSION" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$PLIST"

echo "==> verify bundle"
[ -d "dist/$APP.app" ] || { echo "ERROR: PyInstaller produced no .app"; exit 1; }
LEAKS=$(find "dist/$APP.app" \( -name '*.so' -o -name '*.dylib' \) -print0 2>/dev/null \
  | xargs -0 otool -L 2>/dev/null \
  | grep -E '^\s+(/usr/local|/opt/homebrew)' | sort -u || true)
if [ -n "$LEAKS" ]; then
  echo "WARNING: bundle references paths that a stock Mac won't have:"
  echo "$LEAKS"
else
  echo "    bundle OK — no brew/local leaks"
fi

echo "==> DMG"
mkdir -p release
DMG="release/Dupe Demon-$VERSION.dmg"
rm -f "$DMG"
STAGE=$(mktemp -d)
cp -R "dist/$APP.app" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "Dupe Demon" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"

echo "==> verify DMG"
hdiutil verify "$DMG" >/dev/null && echo "DMG verified OK"
ls -lh "$DMG"

echo "Done."
echo "  App:       dist/$APP.app"
echo "  Installer: $DMG"
