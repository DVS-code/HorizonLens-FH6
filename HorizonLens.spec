from pathlib import Path

block_cipher = None
ROOT = Path(SPECPATH)

datas = [
    (str(ROOT / "web"), "web"),
    (str(ROOT / "assets"), "assets"),
    (str(ROOT / "vendor" / "mediamtx" / "mediamtx.exe"), "vendor/mediamtx"),
    (str(ROOT / "vendor" / "ffmpeg" / "ffmpeg.exe"), "vendor/ffmpeg"),
    (str(ROOT / "vendor" / "ffmpeg" / "ffprobe.exe"), "vendor/ffmpeg"),
    (str(ROOT / "vendor" / "companion" / "HorizonLensCamera.dll"), "vendor/companion"),
    (str(ROOT / "vendor" / "companion" / "xbox" / "HorizonLensCamera.dll"), "vendor/companion/xbox"),
    (str(ROOT / "vendor" / "capture" / "HorizonLensCapture.exe"), "vendor/capture"),
]

a = Analysis(
    ["run.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=["aiohttp", "qrcode", "pyaudiowpatch", "cryptography"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PySide6.QtQuick", "PySide6.Qt3DCore"],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HorizonLens",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ROOT / "assets" / "horizonlens.ico"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="HorizonLens",
)
