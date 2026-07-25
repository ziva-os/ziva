"""One-shot cleanup helpers for reasoning tags embedded in content.

Some OpenAI-compatible providers still emit the model's chain-of-thought
wrapped in ``<think>...</think>`` or ``<mm:think>...</mm:think>`` tags
inside the normal content field when ``reasoning_split`` is not enabled. The
main streaming path now prefers native ``reasoning_content`` / ``reasoning`` /
``reasoning_details`` fields, so the streaming tag parser has been removed.

Only the one-shot ``strip_think_tags`` helper remains, used to clean sub-agent
results before returning them to the parent session.
"""

from __future__ import annotations

import re


def strip_think_tags(text: str) -> str:
    """Remove all ``<think>...</think>`` and ``<mm:think>...</mm:think>`` blocks.

    Used to clean sub-agent results before returning them to the parent
    session — the parent only needs the final answer, not the child's
    chain-of-thought. This is a one-shot pass over complete text (no
    streaming boundaries to worry about).
    """
    text = re.sub(r"<mm:think>.*?</mm:think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # If an opening tag was never closed (truncated stream), strip the
    # remainder from the opening tag onward.
    text = re.sub(r"<(?:mm:)?think>.*", "", text, flags=re.DOTALL)
    return text.strip()
