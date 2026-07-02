from pathlib import Path

import pytest

from ziva.plugins.manifest import load_manifest


def test_manifest_invalid_type(tmp_path: Path):
    p = tmp_path / "manifest.yaml"
    p.write_text("id: bad.x\ntype: unknown\nversion: 0.1\nentry: impl.py:X\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported type"):
        load_manifest(p)


def test_manifest_invalid_entry_format(tmp_path: Path):
    p = tmp_path / "manifest.yaml"
    p.write_text("id: bad.x\ntype: tool\nversion: 0.1\nentry: impl.py\n", encoding="utf-8")
    with pytest.raises(ValueError, match="entry must use"):
        load_manifest(p)
