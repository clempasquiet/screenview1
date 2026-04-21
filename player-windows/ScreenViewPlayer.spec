# PyInstaller spec for the ScreenView Windows player.
#
# Build with (from an activated venv that has the deps installed):
#
#     pyinstaller ScreenViewPlayer.spec --clean --noconfirm
#
# The ``datas`` list copies the bundled ``config.json`` next to the exe so
# first-launch seeding works without depending on source files. Drop a
# ``libmpv-2.dll`` (or ``mpv-2.dll``) into the project root before building
# and it will be picked up automatically.

from pathlib import Path

project = Path(SPECPATH)

datas = [
    (str(project / 'config.json'), '.'),
]
binaries = []

for dll_name in ('libmpv-2.dll', 'mpv-2.dll'):
    dll = project / dll_name
    if dll.is_file():
        binaries.append((str(dll), '.'))

hiddenimports = [
    'PyQt6.QtWebEngineWidgets',
    'PyQt6.QtWebEngineCore',
    'websocket',
    'mpv',
]

a = Analysis(
    ['main.py'],
    pathex=[str(project)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter'],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='ScreenViewPlayer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
