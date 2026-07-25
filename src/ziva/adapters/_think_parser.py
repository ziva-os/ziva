"""Streaming parser that splits provider text into main/reasoning.

Many OpenAI- and Anthropic-compatible providers (MiniMax, GLM, Kimi,
DeepSeek, Qwen, ...) emit the model's chain-of-thought wrapped in
``<think>...</think>`` or ``<mm:think>...</mm:think>`` tags inside the
normal content delta instead of using the native ``reasoning_content``
field (OpenAI side) or a ``thinking`` content block (Anthropic side).

This parser extracts that text and returns it as ``reasoning_content``
so the frontend can route it to the thinking card, leaving only the
final answer in ``content``.

Shared between the OpenAI and Anthropic adapters so both code paths
produce the same reasoning/content split regardless of which provider
format the upstream happens to use.
"""

from __future__ import annotations


class ThinkTagParser:
    def __init__(self) -> None:
        self._in_think = False
        self._buffer = ""
        self._end_tag = "</think>"

    def _detect_tag(self, text: str) -> tuple[str, str, int] | None:
        """Find the earliest start tag and return (start_tag, end_tag, index)."""
        candidates = [
            ("<mm:think>", "</mm:think>"),
            ("<think>", "</think>"),
        ]
        best: tuple[str, str, int] | None = None
        for start_tag, end_tag in candidates:
            idx = text.find(start_tag)
            if idx != -1 and (best is None or idx < best[2]):
                best = (start_tag, end_tag, idx)
        return best

    def _trailing_prefix_len(self, text: str, tag: str) -> int:
        """Return how many trailing chars of `text` are a prefix of `tag`.

        Handles tags split across chunk boundaries: if text ends with ``<thi``
        and the tag is ``<think>``, returns 4 so the caller buffers those 4
        chars for the next feed() call instead of emitting them as content.
        """
        max_check = min(len(text), len(tag) - 1)
        for i in range(max_check, 0, -1):
            if text.endswith(tag[:i]):
                return i
        return 0

    def feed(self, text: str) -> tuple[str, str]:
        """Return (reasoning_content, main_content) for the incoming chunk."""
        text = self._buffer + text
        self._buffer = ""
        if not text:
            return "", ""

        reasoning_parts: list[str] = []
        main_parts: list[str] = []

        while text:
            if self._in_think:
                end = text.find(self._end_tag)
                if end == -1:
                    # Buffer trailing chars that could be the start of the
                    # end tag (e.g. ``</thi``) so a split ``</think>`` is
                    # detected on the next feed() instead of leaking into
                    # reasoning output.
                    buf_len = self._trailing_prefix_len(text, self._end_tag)
                    if buf_len > 0:
                        self._buffer = text[-buf_len:]
                        reasoning_parts.append(text[:-buf_len])
                    else:
                        reasoning_parts.append(text)
                    # main_parts may hold text that preceded the start tag in
                    # this same feed() call (e.g. feed("hi<think>reason")) —
                    # returning "" here would silently drop that content.
                    return "".join(reasoning_parts), "".join(main_parts)
                reasoning_parts.append(text[:end])
                text = text[end + len(self._end_tag):]
                self._in_think = False
            else:
                detected = self._detect_tag(text)
                if detected is None:
                    # Buffer trailing chars that could be the start of a
                    # think tag (e.g. ``<thi``) so a split ``<think>`` is
                    # detected on the next feed() instead of leaking the
                    # raw tag text into main content.
                    start_tags = ("<mm:think>", "<think>")
                    buf_len = max(
                        self._trailing_prefix_len(text, t) for t in start_tags
                    )
                    if buf_len > 0:
                        self._buffer = text[-buf_len:]
                        main_parts.append(text[:-buf_len])
                    else:
                        main_parts.append(text)
                    return "".join(reasoning_parts), "".join(main_parts)
                start_tag, end_tag, start = detected
                self._end_tag = end_tag
                main_parts.append(text[:start])
                text = text[start + len(start_tag):]
                self._in_think = True

        return "".join(reasoning_parts), "".join(main_parts)

    def flush(self) -> tuple[str, str]:
        """Return any remaining buffered text as main content."""
        text = self._buffer
        self._buffer = ""
        if self._in_think:
            return text, ""
        return "", text


def strip_think_tags(text: str) -> str:
    """Remove all ``<think>...</think>`` and ``<mm:think>...</mm:think>`` blocks.

    Used to clean sub-agent results before returning them to the parent
    session — the parent only needs the final answer, not the child's
    chain-of-thought. Unlike :class:`ThinkTagParser` this is a one-shot
    pass over complete text (no streaming boundaries to worry about).
    """
    import re
    text = re.sub(r"<mm:think>.*?</mm:think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # If an opening tag was never closed (truncated stream), strip the
    # remainder from the opening tag onward.
    text = re.sub(r"<(?:mm:)?think>.*", "", text, flags=re.DOTALL)
    return text.strip()
