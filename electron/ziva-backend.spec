# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

block_cipher = None

PROJECT_ROOT = Path(SPECPATH).resolve().parent

a = Analysis(
    [str(PROJECT_ROOT / 'src' / 'ziva_runtime' / '__main__.py')],
    pathex=[str(PROJECT_ROOT / 'src')],
    binaries=[],
    datas=[
        (str(PROJECT_ROOT / 'src' / 'ziva_runtime' / 'transports' / 'desktop_api' / 'static'), 'static'),
    ],
    hiddenimports=[
        'ziva_runtime',
        'ziva_runtime.__main__',
        'ziva_runtime.app.cli',
        'ziva_runtime.runtime',
        'ziva_runtime.shared_types',
        'ziva_runtime.config.loader',
        'ziva_runtime.permissions',
        'ziva_runtime.session.compaction',
        'ziva_runtime.transports.desktop_api.server',
        'ziva_runtime.adapters.openai_agents.provider',
        'ziva_runtime.adapters.mcp.client',
        'ziva_runtime.plugins.loader',
        'ziva_runtime.plugins.manifest',
        'ziva_runtime.storage.file_storage',
        'aiohttp',
        'aiohttp.web',
        'yaml',
        'rich',
        'openai',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'numpy',
        'scipy',
        'PIL',
        'pytest',
        'torch',
        'tensorflow',
        'jax',
        'cv2',
        'pyarrow',
        'pandas',
        'sklearn',
        'IPython',
        'notebook',
        'sphinx',
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='ziva-backend',
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
)
