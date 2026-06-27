# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

block_cipher = None

PROJECT_ROOT = Path(SPECPATH).resolve().parent

# PyInstaller ships runtime hooks under its install dir. We need an absolute
# path because the spec file is executed from the project root and
# `runtime_hooks` is not searched on sys.path.
import PyInstaller
_PYI_RTHOOKS = Path(PyInstaller.__file__).resolve().parent / 'hooks' / 'rthooks'

a = Analysis(
    [str(PROJECT_ROOT / 'src' / 'ziva_runtime' / '__main__.py')],
    pathex=[str(PROJECT_ROOT / 'src')],
    binaries=[],
    datas=[
        (str(PROJECT_ROOT / 'src' / 'ziva_runtime' / 'transports' / 'desktop_api' / 'static'), 'static'),
        # Bundle plugins so the packaged app has tools/hooks/memory even when
        # the workspace has no plugins/ dir. runtime.create loads these from
        # sys._MEIPASS/plugins when frozen.
        (str(PROJECT_ROOT / 'plugins'), 'plugins'),
    ],
    hiddenimports=[
        'ziva_runtime',
        'ziva_runtime.__main__',
        'ziva_runtime.app.cli',
        'ziva_runtime.app.display',
        'ziva_runtime.runtime',
        'ziva_runtime.shared_types',
        'ziva_runtime.config.loader',
        'ziva_runtime.config.instructions',
        'ziva_runtime.permissions',
        'ziva_runtime.permissions.manager',
        'ziva_runtime.permissions.wildcard',
        'ziva_runtime.session.compaction',
        'ziva_runtime.transports.desktop_api.server',
        # adapters — anthropic/retry/mcp.server are imported INSIDE functions
        # in runtime.py, so PyInstaller's static analysis misses them. Force
        # them in or the packaged backend raises ImportError (masked as
        # "could not get source code" by the traceback-inspect failure).
        'ziva_runtime.adapters.openai.provider',
        'ziva_runtime.adapters.anthropic.provider',
        'ziva_runtime.adapters.mcp.client',
        'ziva_runtime.adapters.mcp.server',
        'ziva_runtime.adapters.retry',
        'ziva_runtime.capabilities.events',
        'ziva_runtime.capabilities.registries',
        'ziva_runtime.capabilities.interfaces',
        'ziva_runtime.plugins.loader',
        'ziva_runtime.plugins.manifest',
        'ziva_runtime.storage.file_storage',
        'ziva_runtime.protocols.acp',
        'aiohttp',
        'aiohttp.web',
        'yaml',
        'rich',
        'openai',
        'anthropic',
        'mcp',
    ],
    hookspath=[],
    hooksconfig={},
    # PyInstaller's automatic rthook detection relies on the graph analyzer
    # spotting the corresponding module being imported. When the dependency
    # is transitive (e.g. inspect via stdlib traceback / SDK error formatting)
    # the hook isn't always wired up, which leaves inspect.getsource() /
    # inspect.getsourcefile() broken in the frozen binary — manifesting as
    # the cryptic OSError("could not get source code") raised by
    # inspect.py:1081. Force-include the hook so packaged backend behaves
    # the same as the dev run.
    runtime_hooks=[str(_PYI_RTHOOKS / 'pyi_rth_inspect.py')],
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
        # ML/NLP libs present in the global (miniconda) env that ziva never
        # imports. Excluding shrinks the bundle and avoids PyInstaller hook
        # failures (e.g. transformers' tokenizers version check).
        'transformers',
        'tokenizers',
        'modelscope',
        'sentencepiece',
        'datasets',
        'huggingface_hub',
        'accelerate',
        'diffusers',
        'soundfile',
        'librosa',
        'onnxruntime',
        'opencv',
    ],
    # Keep bytecode as separate .pyc files instead of compressing them into
    # the PYZ archive. The archive's zlib decompress fails ("incorrect
    # header check") in some Python/PyInstaller combos (seen with miniconda
    # 3.11), crashing on every import.
    noarchive=True,
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
