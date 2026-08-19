# -*- mode: python ; coding: utf-8 -*-
# Build:  venv\Scripts\pyinstaller.exe build_exe.spec

block_cipher = None

a = Analysis(
    ['controller.py'],
    pathex=[],
    binaries=[],
    datas=[
        # uiautomation optional helper DLLs
        ('venv/Lib/site-packages/uiautomation/bin', 'uiautomation/bin'),
    ],
    hiddenimports=[
        'pystray._win32',
        'PIL._tkinter_finder',
        'tkinter',
        '_tkinter',
        'tkinter.ttk',
        'uiautomation',
        'comtypes',
        'comtypes.stream',
        'pythoncom',
        'pywintypes',
        'win32gui',
        'win32con',
        'win32api',
        'win32process',
        'pycaw',
        'pycaw.pycaw',
        'pycaw.utils',
        'pycaw.api',
        'pycaw.api.audioclient',
        'pycaw.api.mmdeviceapi',
        'comtypes.gen',
        'comtypes.gen.MMDeviceAPILib',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='TuneBladeController',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # no black console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
