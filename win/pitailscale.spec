# PyInstaller spec — onefile build cho Windows
# Gọi: pyinstaller pitailscale.spec
# ra: dist/pitailscale.exe

block_cipher = None

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('templates', 'templates'),  # Flask template folder (bundled, extract vào _MEIPASS lúc runtime)
        ('static', 'static'),
    ],
    hiddenimports=[],  # PyInstaller tự detect — explicit psutil._psutil_windows gây lỗi khi version mismatch
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'test', 'pydoc'],
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
    name='pitailscale',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,         # windows-latest runner không có UPX pre-installed → tắt để không fail
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,      # console mode để user thấy log + install messages
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='icon.ico',  # bỏ comment khi có file icon.ico trong win/
)
