"""Unit tests for the runtime's _resolve_image_paths helper and
the Runtime._current_model_supports_image helper that drives its
model-capability branch.

The runtime expands ``image_url`` blocks whose ``url`` field is a
local file path (the desktop UI drops user-pasted images to disk and
sends the path; the provider adapter only accepts ``data:`` or
``http(s):`` URLs). The behavior branches on whether the current
model is vision-capable:

  * Vision model   → read file → base64 dataURL
  * Non-vision     → don't read; rewrite block to a text reference
                     naming the file (the model can use read_file,
                     OCR tools, or surface back to the user)

Other invariants the tests pin down:

  * data: / http(s): URLs are passthrough regardless of model flag.
  * file:// URLs are treated as paths (scheme stripped, file read).
  * Missing file / unknown extension / relative path with no anchor
    / OS error / empty url → text reference, never a leaked path.
  * Original message list is NOT mutated (resolver must not corrupt
    the persisted history).
  * `_current_model_supports_image` reads the active model from
    config, defaults to True when the model is unknown or the field
    is missing (conservative — prefer a useful provider error over
    silently rewriting attachments to text).
"""
import base64
import os
import tempfile
from pathlib import Path

from ziva_runtime.runtime import Runtime, _resolve_image_paths
from ziva_runtime.shared_types import ChatMessage


def test_path_to_data_url():
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"\x89PNG_FAKE_BYTES")
        tmp = f.name
    try:
        msgs = [ChatMessage(
            role="user",
            content=[{"type": "image_url", "image_url": {"url": tmp}}],
        )]
        out = _resolve_image_paths(msgs)
        url = out[0].content[0]["image_url"]["url"]
        assert url.startswith("data:image/png;base64,"), url
        assert base64.b64decode(url.split(",", 1)[1]) == b"\x89PNG_FAKE_BYTES"
    finally:
        os.unlink(tmp)


def test_data_url_passthrough():
    msgs = [ChatMessage(
        role="user",
        content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
    )]
    out = _resolve_image_paths(msgs)
    assert out[0].content[0]["image_url"]["url"] == "data:image/png;base64,AAAA"


def test_http_url_passthrough():
    msgs = [ChatMessage(
        role="user",
        content=[{"type": "image_url", "image_url": {"url": "https://example.com/x.png"}}],
    )]
    out = _resolve_image_paths(msgs)
    assert out[0].content[0]["image_url"]["url"] == "https://example.com/x.png"


def test_missing_file_becomes_placeholder():
    msgs = [ChatMessage(
        role="user",
        content=[{"type": "image_url", "image_url": {"url": "/nonexistent/path.png"}}],
    )]
    out = _resolve_image_paths(msgs)
    block = out[0].content[0]
    assert block["type"] == "text"
    assert "FileNotFoundError" in block["text"] or "unavailable" in block["text"]
    assert "/nonexistent/path.png" in block["text"]


def test_unknown_extension_becomes_text_reference():
    """An extension we don't recognize must NOT leak as a path-shaped
    image_url to the provider (OpenAI would 400 with "Invalid image
    URL"). Convert to a text block describing the file instead — the
    model can still call read_file on the path if it actually needs
    the bytes."""
    with tempfile.NamedTemporaryFile(suffix=".xyz", delete=False) as f:
        f.write(b"fake")
        tmp = f.name
    try:
        msgs = [ChatMessage(
            role="user",
            content=[{"type": "image_url", "image_url": {"url": tmp}}],
        )]
        out = _resolve_image_paths(msgs)
        block = out[0].content[0]
        assert block["type"] == "text", block
        assert "not a recognized image" not in block["text"]  # we keep the info, just rewrite
        assert tmp in block["text"]
        assert ".xyz" in block["text"]
        assert "4 bytes" in block["text"]
    finally:
        os.unlink(tmp)


def test_relative_path_becomes_text_reference():
    """Resolver refuses to guess a base directory — but a relative
    path also must not leak as a raw image_url to the provider. We
    convert to a text reference that names the path so the user
    sees the original intent and the model can call read_file."""
    msgs = [ChatMessage(
        role="user",
        content=[{"type": "image_url", "image_url": {"url": "rel/path.png"}}],
    )]
    out = _resolve_image_paths(msgs)
    block = out[0].content[0]
    assert block["type"] == "text", block
    assert "rel/path.png" in block["text"]
    assert "relative" in block["text"].lower() or "no base" in block["text"].lower()


def test_file_url_prefix_stripped_and_read():
    """file:// URL gets the scheme stripped, then treated as a path.
    Should resolve to a data URL just like a bare path would."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"\x89PNG_FAKE")
        tmp = f.name
    try:
        msgs = [ChatMessage(
            role="user",
            content=[{"type": "image_url", "image_url": {"url": f"file://{tmp}"}}],
        )]
        out = _resolve_image_paths(msgs)
        url = out[0].content[0]["image_url"]["url"]
        assert url.startswith("data:image/png;base64,"), url
    finally:
        os.unlink(tmp)


def test_empty_url_becomes_text_reference():
    """A malformed block with an empty url should not crash the
    resolver and must not leak (an empty url would be an obviously
    broken image_url to the provider)."""
    msgs = [ChatMessage(
        role="user",
        content=[{"type": "image_url", "image_url": {"url": ""}}],
    )]
    out = _resolve_image_paths(msgs)
    block = out[0].content[0]
    assert block["type"] == "text"
    assert "empty" in block["text"].lower()


def test_original_message_untouched():
    """The resolver must NOT mutate the input — the persisted history
    keeps the path form so reloads stay cheap; only the per-turn
    copy sent to the provider is expanded."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"\x89PNG_FAKE_BYTES")
        tmp = f.name
    try:
        orig_content = [{"type": "image_url", "image_url": {"url": tmp}}]
        msgs = [ChatMessage(role="user", content=orig_content)]
        out = _resolve_image_paths(msgs)
        # Original still has the path
        assert msgs[0].content is orig_content
        assert msgs[0].content[0]["image_url"]["url"] == tmp
        # Output has the data URL
        assert out[0] is not msgs[0]
        assert out[0].content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    finally:
        os.unlink(tmp)


def test_string_content_passthrough():
    msgs = [ChatMessage(role="user", content="hello")]
    out = _resolve_image_paths(msgs)
    assert out[0].content == "hello"


def test_mixed_text_and_image():
    """A typical user turn is text + image. Text blocks must round-trip
    untouched, image blocks get expanded."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"\x89PNG_FAKE_BYTES")
        tmp = f.name
    try:
        msgs = [ChatMessage(role="user", content=[
            {"type": "text", "text": "look at this"},
            {"type": "image_url", "image_url": {"url": tmp}},
        ])]
        out = _resolve_image_paths(msgs)
        assert out[0].content[0] == {"type": "text", "text": "look at this"}
        assert out[0].content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    finally:
        os.unlink(tmp)


def test_assistant_messages_passed_through():
    """Only user (or tool-output) messages carry image blocks. We just
    want to confirm the resolver doesn't accidentally try to expand
    paths in an assistant text message."""
    msgs = [ChatMessage(role="assistant", content="no image here")]
    out = _resolve_image_paths(msgs)
    assert out[0].content == "no image here"


def test_non_dict_block_passes_through():
    """Defensive: a malformed block (string in the list, None, etc.)
    should not crash the resolver."""
    msgs = [ChatMessage(role="user", content=["not a dict", None, 42])]
    out = _resolve_image_paths(msgs)
    assert out[0].content == ["not a dict", None, 42]


def test_no_path_ever_leaks_to_provider_format():
    """Invariant: after _resolve_image_paths, NO surviving block may
    be an image_url whose url is a bare local path (i.e. not data:,
    not http(s):, not /attachments?path=..., and not an explicit
    file:// that we successfully expanded to a data URL).

    This is what protects us from OpenAI's "Invalid image URL" 400.
    If a path is unresolvable for any reason — missing file, unknown
    extension, relative-without-anchor, OS error — it must have been
    rewritten to a text block. The model can still use read_file on
    the path string, but the provider never sees a path-shaped URL.
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fp:
        fp.write(b"good")
        good_path = fp.name
    with tempfile.NamedTemporaryFile(suffix=".xyz", delete=False) as fx:
        fx.write(b"unknown-ext")
        unknown_path = fx.name
    try:
        msgs = [ChatMessage(role="user", content=[
            {"type": "text", "text": "看看这些"},
            {"type": "image_url", "image_url": {"url": good_path}},          # resolves
            {"type": "image_url", "image_url": {"url": "/no/such/file.png"}},  # missing
            {"type": "image_url", "image_url": {"url": unknown_path}},        # unknown ext
            {"type": "image_url", "image_url": {"url": "rel/x.png"}},         # relative
            {"type": "image_url", "image_url": {"url": ""}},                   # empty
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},  # passthrough
            {"type": "image_url", "image_url": {"url": "https://x.com/y.png"}},          # passthrough
        ])]
        out = _resolve_image_paths(msgs)
        for block in out[0].content:
            if block.get("type") != "image_url":
                continue  # text blocks are fine
            url = block["image_url"]["url"]
            # A surviving image_url must be either:
            #   * a data: URL (we successfully expanded a path)
            #   * an http(s) URL (passthrough, no path involved)
            assert url.startswith("data:") or url.startswith("http://") or url.startswith("https://"), \
                f"path-shaped URL leaked to provider: {url!r}"
    finally:
        os.unlink(good_path)
        os.unlink(unknown_path)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f1:
        f1.write(b"jpg1")
        tmp1 = f1.name
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f2:
        f2.write(b"png2")
        tmp2 = f2.name
    try:
        msgs = [ChatMessage(role="user", content=[
            {"type": "image_url", "image_url": {"url": tmp1}},
            {"type": "image_url", "image_url": {"url": tmp2}},
        ])]
        out = _resolve_image_paths(msgs)
        assert out[0].content[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        assert out[0].content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    finally:
        os.unlink(tmp1)
        os.unlink(tmp2)


def test_non_vision_model_gets_path_text_no_file_read():
    """When the current model is not vision-capable, path-shaped
    image_url blocks should be rewritten to a text reference
    *without* reading the file. This matters because:

      * Reading a 4K screenshot just to embed as base64 is wasted
        work — the model can't see it anyway.
      * The wire payload becomes tiny: path is ~100 bytes vs
        megabytes of base64.
      * The model still gets useful info: the path is in the text
        so it can use read_file (if applicable), an OCR tool, or
        just tell the user it can't view images.

    Crucially, we must NOT read the file at all — there should be
    zero I/O for a non-vision model. Use a nonexistent path to
    prove it: if the resolver tried to read, the test would fail
    with FileNotFoundError.
    """
    nonexistent = "/this/path/does/not/exist/anywhere.png"
    msgs = [ChatMessage(
        role="user",
        content=[{"type": "image_url", "image_url": {"url": nonexistent}}],
    )]
    out = _resolve_image_paths(msgs, model_supports_image=False)
    block = out[0].content[0]
    assert block["type"] == "text"
    assert nonexistent in block["text"]
    # No "FileNotFoundError" suffix because we never tried to read
    assert "FileNotFoundError" not in block["text"]
    # No data: URL leaked
    assert "data:" not in block["text"]


def test_non_vision_model_passes_through_http_urls():
    """http(s) URLs are passed through even for non-vision models.
    The user wrote them deliberately — they might be a link to a
    page the model can read with WebFetch, or an image the user
    knows the model can analyze via some downstream pipeline.

    We never try to fetch the URL ourselves; we just don't rewrite
    it. The provider will deal with it (or error)."""
    msgs = [ChatMessage(role="user", content=[
        {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ])]
    out = _resolve_image_paths(msgs, model_supports_image=False)
    assert out[0].content[0]["image_url"]["url"] == "https://example.com/cat.png"
    assert out[0].content[1]["image_url"]["url"] == "data:image/png;base64,AAAA"


def test_vision_model_explicit_true_matches_default():
    """Default behavior is model_supports_image=True. Passing it
    explicitly should produce the same result as omitting it.
    This pins down the default so a future refactor can't quietly
    flip it."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"\x89PNG_FAKE")
        tmp = f.name
    try:
        msgs = [ChatMessage(
            role="user",
            content=[{"type": "image_url", "image_url": {"url": tmp}}],
        )]
        out_default = _resolve_image_paths(msgs)
        out_explicit = _resolve_image_paths(msgs, model_supports_image=True)
        assert out_default[0].content[0]["image_url"]["url"] == out_explicit[0].content[0]["image_url"]["url"]
    finally:
        os.unlink(tmp)


def test_non_vision_model_mixed_text_and_image():
    """Text blocks in the same content list are untouched, even
    when there's a non-vision image block alongside."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"\x89PNG")
        tmp = f.name
    try:
        msgs = [ChatMessage(role="user", content=[
            {"type": "text", "text": "看看这个"},
            {"type": "image_url", "image_url": {"url": tmp}},
        ])]
        out = _resolve_image_paths(msgs, model_supports_image=False)
        assert out[0].content[0] == {"type": "text", "text": "看看这个"}
        assert out[0].content[1]["type"] == "text"
        assert tmp in out[0].content[1]["text"]
    finally:
        os.unlink(tmp)


def test_multiple_images_in_one_message():
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f1:
        f1.write(b"jpg1")
        tmp1 = f1.name
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f2:
        f2.write(b"png2")
        tmp2 = f2.name
    try:
        msgs = [ChatMessage(role="user", content=[
            {"type": "image_url", "image_url": {"url": tmp1}},
            {"type": "image_url", "image_url": {"url": tmp2}},
        ])]
        out = _resolve_image_paths(msgs)
        assert out[0].content[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        assert out[0].content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    finally:
        os.unlink(tmp1)
        os.unlink(tmp2)


# ----------------------------------------------------------------------------
# Runtime._current_model_supports_image
# ----------------------------------------------------------------------------
# This helper is what makes _resolve_image_paths branch on model
# capability. The branching tests above pin down the resolver's
# contract; these tests pin down the helper's contract so a config
# refactor can't quietly flip the default.

class _FakeAdapter:
    def spec(self): return []
    async def chat(self, m, **k): pass
    async def chat_stream(self, m, **k):
        for _ in []: yield _


def _make_runtime() -> "Runtime":
    """Build a Runtime with a no-op model adapter. The config is
    whatever the workspace picks up by default — tests that need
    specific model config just mutate ``rt.config`` afterward."""
    return Runtime.create(
        workspace_root=Path(tempfile.mkdtemp()),
        model_adapter=_FakeAdapter(),
    )


def _set_model(rt, *, name, providers):
    rt.config["model"] = {"name": name}
    rt.config["providers"] = providers


def test_runtime_helper_vision_model_returns_true():
    rt = _make_runtime()
    _set_model(rt, name="gpt-4o", providers=[
        {"models": [
            {"name": "gpt-4o", "capabilities": {"vision": True}},
            {"name": "gpt-3.5-turbo", "capabilities": {"vision": False}},
        ]}
    ])
    assert rt._current_model_supports_image() is True


def test_runtime_helper_non_vision_model_returns_false():
    rt = _make_runtime()
    _set_model(rt, name="gpt-3.5-turbo", providers=[
        {"models": [
            {"name": "gpt-4o", "capabilities": {"vision": True}},
            {"name": "gpt-3.5-turbo", "capabilities": {"vision": False}},
        ]}
    ])
    assert rt._current_model_supports_image() is False


def test_runtime_helper_unknown_model_defaults_to_true():
    """If we can't find the model in any provider, assume it's
    vision-capable. Better to send a base64 blob the provider can
    reject with a clear error than to silently rewrite to text."""
    rt = _make_runtime()
    _set_model(rt, name="unknown-model", providers=[])
    assert rt._current_model_supports_image() is True


def test_runtime_helper_missing_field_defaults_to_true():
    """A model entry without capabilities.vision (e.g. a minimal
    config) defaults to True — same reasoning as the unknown-model
    case."""
    rt = _make_runtime()
    _set_model(rt, name="old-model", providers=[
        {"models": [{"name": "old-model"}]}  # no capabilities block
    ])
    assert rt._current_model_supports_image() is True


def test_runtime_helper_provider_capability_falls_through():
    """A model with no capabilities block inherits the provider's
    capabilities.vision (default True if provider has none either)."""
    rt = _make_runtime()
    _set_model(rt, name="inherits-vision", providers=[
        {"capabilities": {"vision": True}, "models": [{"name": "inherits-vision"}]},
    ])
    assert rt._current_model_supports_image() is True

    rt = _make_runtime()
    _set_model(rt, name="inherits-no-vision", providers=[
        {"capabilities": {"vision": False}, "models": [{"name": "inherits-no-vision"}]},
    ])
    assert rt._current_model_supports_image() is False


def test_runtime_helper_empty_model_name_returns_true():
    """Defensive: a config with no model.name should not crash and
    should not silently flip to False. Default True is the
    safer failure mode."""
    rt = _make_runtime()
    _set_model(rt, name="", providers=[])
    assert rt._current_model_supports_image() is True


def test_runtime_helper_picks_correct_model_in_multi_provider_config():
    """With multiple providers, each with their own model list, the
    helper must look up the model by *name* across all providers and
    not just trust the first entry it finds."""
    rt = _make_runtime()
    _set_model(rt, name="claude-opus-4-7", providers=[
        {"models": [{"name": "gpt-4o", "capabilities": {"vision": True}}]},
        {"models": [{"name": "claude-opus-4-7", "capabilities": {"vision": True}}]},
        {"models": [{"name": "gpt-3.5-turbo", "capabilities": {"vision": False}}]},
    ])
    assert rt._current_model_supports_image() is True

    rt.config["model"]["name"] = "gpt-3.5-turbo"
    assert rt._current_model_supports_image() is False


# Smoke test: a vision model end-to-end still produces a dataURL,
# a non-vision model end-to-end produces a text reference. The
# individual branch tests already cover the resolver, but this
# ties the runtime helper to the resolver to make sure the wiring
# is right (i.e. the value the helper returns actually gets
# threaded through).
def test_runtime_end_to_end_vision_vs_non_vision():
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"\x89PNG_FAKE")
        tmp = f.name
    try:
        msgs = [ChatMessage(
            role="user",
            content=[{"type": "image_url", "image_url": {"url": tmp}}],
        )]

        # Vision model
        rt_v = _make_runtime()
        _set_model(rt_v, name="vision", providers=[
            {"models": [{"name": "vision", "capabilities": {"vision": True}}]},
        ])
        out = _resolve_image_paths(msgs, model_supports_image=rt_v._current_model_supports_image())
        assert out[0].content[0]["image_url"]["url"].startswith("data:image/png;base64,")

        # Non-vision model
        rt_nv = _make_runtime()
        _set_model(rt_nv, name="text-only", providers=[
            {"models": [{"name": "text-only", "capabilities": {"vision": False}}]},
        ])
        out = _resolve_image_paths(msgs, model_supports_image=rt_nv._current_model_supports_image())
        assert out[0].content[0]["type"] == "text"
        assert tmp in out[0].content[0]["text"]
    finally:
        os.unlink(tmp)