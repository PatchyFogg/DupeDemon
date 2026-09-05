# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

_dnd_datas, _dnd_binaries, _dnd_hidden = collect_all('tkinterdnd2')

a = Analysis(
    ['dupe_demon.py'],
    pathex=[],
    binaries=_dnd_binaries,
    datas=_dnd_datas + [('assets/progress_frames', 'assets/progress_frames')],
    hiddenimports=_dnd_hidden + ['tkinterdnd2'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Dupe Demon',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['app_icon.icns'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Dupe Demon',
)
app = BUNDLE(
    coll,
    name='Dupe Demon.app',
    icon='app_icon.icns',
    bundle_identifier='com.saltz.dupedemon',
    info_plist={
        # Required for macOS to show the Automation permission prompt at
        # all when the app sends Finder an Apple Event (Move to Trash).
        # Without this key the OS silently blocks the request instead of
        # prompting — no dialog, and the app never appears in Privacy &
        # Security > Automation to grant manually either.
        'NSAppleEventsUsageDescription':
            'Dupe Demon asks Finder to move duplicate photos to the Trash, '
            'so deletions stay safe and recoverable.',
    },
)
