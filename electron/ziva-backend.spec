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
    [str(PROJECT_ROOT / 'src' / 'ziva' / '__main__.py')],
    pathex=[str(PROJECT_ROOT / 'src')],
    binaries=[],
    datas=[
        (str(PROJECT_ROOT / 'src' / 'ziva' / 'transports' / 'desktop_api' / 'static'), 'static'),
        # Bundle plugins so the packaged app has tools/hooks/memory even when
        # the workspace has no plugins/ dir. runtime.create loads these from
        # sys._MEIPASS/plugins when frozen.
        (str(PROJECT_ROOT / 'plugins'), 'plugins'),
        # mlx (Apple Silicon ML framework) loads its Metal shader library at
        # runtime via dlopen. The metallib ships in the mlx-metal wheel as
        # `mlx/lib/mlx.metallib` (~125 MB) — PyInstaller's analysis doesn't
        # pick it up because it's dlopen'd, not import'd. Without it, mlx
        # fails with "Failed to load the default metallib".
        (
            str(Path('/Users/wangxinxin/code/ziva/.build-venv/lib/python3.11/site-packages/mlx/lib/mlx.metallib')),
            'mlx/lib',
        ),
        # mlx_whisper's audio module loads mel_filters.npz at runtime via
        # `os.path.join(os.path.dirname(__file__), "assets", ...)`. The
        # assets/ dir also holds gpt2.tiktoken and multilingual.tiktoken
        # which tiktoken needs to decode. PyInstaller's analysis doesn't
        # copy these data files because they're opened via plain file I/O,
        # not import machinery. Copying the whole assets/ dir mirrors the
        # on-disk layout mlx_whisper expects.
        (
            str(Path('/Users/wangxinxin/code/ziva/.build-venv/lib/python3.11/site-packages/mlx_whisper/assets')),
            'mlx_whisper/assets',
        ),
    ],
    hiddenimports=[
        'ziva',
        'ziva.__main__',
        'ziva.app.cli',
        'ziva.app.display',
        'ziva.runtime',
        'ziva.shared_types',
        'ziva.config.loader',
        'ziva.config.instructions',
        'ziva.permissions',
        'ziva.permissions.manager',
        'ziva.permissions.wildcard',
        'ziva.session.compaction',
        'ziva.transports.desktop_api.server',
        # adapters — anthropic/retry/mcp.server are imported INSIDE functions
        # in runtime.py, so PyInstaller's static analysis misses them. Force
        # them in or the packaged backend raises ImportError (masked as
        # "could not get source code" by the traceback-inspect failure).
        'ziva.adapters.openai.provider',
        'ziva.adapters.anthropic.provider',
        'ziva.adapters.mcp.client',
        'ziva.adapters.mcp.server',
        'ziva.adapters.retry',
        # _think_parser is only referenced via lazy import inside plugin tool
        # bodies (spawn_agent/get_agent_result). PyInstaller's static analyzer
        # can't follow those references, so without this entry the frozen
        # backend raises `ModuleNotFoundError: No module named
        # 'ziva.adapters._think_parser'` the first time a foreground sub-agent
        # finishes or get_agent_result returns a result. That exception kills
        # the parent turn mid-stream — orphan tool_calls then get sanitized
        # next turn as "Tool execution cancelled by user.", which surfaces in
        # the UI as a phantom cancellation. Force-include it alongside the
        # other lazy adapters.
        'ziva.adapters._think_parser',
        'ziva.capabilities.events',
        'ziva.capabilities.registries',
        'ziva.capabilities.interfaces',
        'ziva.plugins.loader',
        'ziva.plugins.manifest',
        'ziva.storage.file_storage',
        'ziva.protocols.acp',
        'aiohttp',
        'aiohttp.web',
        'yaml',
        'rich',
        'openai',
        'anthropic',
        'mcp',
        # Lark (Feishu) SDK is imported lazily inside the adapter; force it
        # into the bundle so the packaged backend can connect to Feishu.
        'lark_oapi',
        # STT (voice input) on Apple Silicon. mlx_whisper's import graph
        # is dynamic (lazy + multiprocessing), so PyInstaller's static
        # analyzer misses it. Force-include the full transitive set so
        # the bundled backend can actually transcribe audio. The package
        # is only installed on darwin/arm64 (see pyproject.toml platform
        # marker), so non-Mac builds simply skip this block — the
        # `import mlx_whisper` inside speech_to_text only runs on Mac.
        # NOTE: torch is NOT bundled. mlx_whisper's `transcribe()` runs
        # entirely on mlx; torch_whisper.py is an unused CPU fallback
        # that nothing imports. Bundling torch would also drag in
        # torch.utils.tensorboard which SIGABRTs on this miniconda
        # build env (PyInstaller's hook-torch then crashes mid-analysis).
        'mlx_whisper',
        'mlx',
        'mlx.core',
        'mlx.nn',
        'mlx.optimizers',
        'mlx.utils',
        # mlx.core's nanobind extension does an in-binary `import mlx._reprlib_fix`
        # at module init. The module is on disk but PyInstaller's analysis
        # doesn't see the dynamic import inside the .so, so it's missing
        # from the bundle and mlx.core fails to load with the cryptic
        # "Encountered an error while initializing the extension."
        'mlx._reprlib_fix',
        'huggingface_hub',
        'huggingface_hub.utils',
        'tiktoken',
        # tiktoken's C extension is loaded dynamically from tiktoken/registry.py
        # via a dotted-name import (`tiktoken.tiktoken_ext` on PyPI naming
        # collisions matter — we need both names). Without it, whisper's
        # tokenizer init fails with `No module named 'tiktoken_ext'`.
        'tiktoken_ext',
        'tiktoken.tiktoken_ext',
        'tiktoken.load',
        # tiktoken_ext is a namespace package shipped as its own directory
        # (site-packages/tiktoken_ext/openai_public.py) that holds the BPE
        # tables tiktoken needs to encode/decode. It's discovered via
        # pkgutil.iter_modules at runtime, so PyInstaller's static analysis
        # doesn't see the import. Whisper's tokenizer init fails without it.
        'tiktoken_ext.openai_public',
        'numba',
        'numba.core',
        'more_itertools',
        # Plugins are bundled as data and loaded dynamically, so PyInstaller's
        # static analyzer misses their imports. The old web_fetch plugin
        # imported html.parser; include it defensively so dynamic plugin loads
        # don't fail in the frozen binary.
        'html.parser',
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
    #
    # Also include the multiprocessing hooks. mlx_whisper and other libraries
    # spawn subprocesses; without these hooks the frozen binary fails to start
    # the multiprocessing resource tracker and worker processes, showing errors
    # like "invalid choice: 'from multiprocessing.resource_tracker import main'".
    runtime_hooks=[
        str(_PYI_RTHOOKS / 'pyi_rth_inspect.py'),
        str(_PYI_RTHOOKS / 'pyi_rth_multiprocessing.py'),
    ],
    excludes=[
        'tkinter',
        'matplotlib',
        'PIL',
        'pytest',
        'torch',
        'torchvision',
        'torchaudio',
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
        # imports directly. NOTE: numpy / scipy / huggingface_hub / tokenizers
        # are NOT excluded — mlx_whisper (voice input on macOS) requires them,
        # and on non-Mac builds mlx_whisper is never imported so the unused
        # deps end up tree-shaken by PyInstaller's analysis.
        'transformers',
        'modelscope',
        'sentencepiece',
        'datasets',
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
