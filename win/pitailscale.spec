# PyInstaller spec — onefile multi-mode build cho Windows
# Build: pyinstaller pitailscale.spec
# Output: dist/pitailscale.exe (~100MB với ffmpeg bundled)
#
# Pre-build (CI làm tự động): copy shared source files + download ffmpeg
#   - cp ../rec_shared.py ../vendors.py ../recorder.py ../clipapp.py ./
#   - cp -R ../templates/* templates/   (recorder.html, clip.html)
#   - cp -R ../static/*     static/     (recorder.js, clip.js, …)
#   - download ffmpeg + ffprobe → bin/

block_cipher = None

import os

datas = [
    ('templates', 'templates'),  # gateway index + recorder + clip templates
    ('static', 'static'),
]
binaries = []

# Bundle ffmpeg.exe + ffprobe.exe nếu tồn tại (CI tải sẵn vào bin/)
for exe in ('ffmpeg.exe', 'ffprobe.exe'):
    p = os.path.join('bin', exe)
    if os.path.exists(p):
        binaries.append((p, 'bin'))

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    # Flask + Jinja2 + sqlite thường bị auto-detect; liệt kê explicit
    # để chắc chắn recorder/clip modules load được từ onefile.
    hiddenimports=[
        'rec_shared', 'vendors', 'recorder', 'clipapp',
        'scanner', 'forwarder',
        'jinja2.ext',
    ],
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
    upx=False,         # windows-latest runner không có UPX pre-installed
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,      # user thấy log của recorder/clip/gateway khi chạy
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='icon.ico',
)
