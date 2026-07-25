"""Unit tests for the streaming <think> tag parser."""

import random

from ziva.adapters._think_parser import ThinkTagParser, strip_think_tags


def _stream(chunks):
    p = ThinkTagParser()
    reasoning, main = "", ""
    for c in chunks:
        r, m = p.feed(c)
        reasoning += r
        main += m
    r, m = p.flush()
    return reasoning + r, main + m


def test_start_tag_and_content_in_same_feed_keeps_main():
    # Regression: when the start tag arrives mid-feed and no end tag is
    # present, the text before the tag must not be dropped.
    p = ThinkTagParser()
    r, m = p.feed("hello <think>reasoning still going")
    assert m == "hello "
    assert r == "reasoning still going"


def test_state_continues_after_mid_feed_start_tag():
    p = ThinkTagParser()
    p.feed("hello <think>reasoning still going")
    r, m = p.feed(" more</think>final answer")
    assert r == " more"
    assert m == "final answer"


def test_end_tag_split_across_chunks():
    assert _stream(["<think>abc</thi", "nk>def"]) == ("abc", "def")


def test_start_tag_split_across_chunks():
    assert _stream(["pre<thi", "nk>reason</think>post"]) == ("reason", "prepost")


def test_mm_think_variant():
    assert _stream(["<mm:think>r</mm:think>main"]) == ("r", "main")


def test_no_end_tag_never_emits_content():
    reasoning, main = _stream(["<think>当前目录是 `/Users/wang", "xinxin/code/ziva`。"])
    assert reasoning == "当前目录是 `/Users/wangxinxin/code/ziva`。"
    assert main == ""


def test_plain_text_passthrough():
    assert _stream(["no tags ", "here"]) == ("", "no tags here")


def test_random_chunking_matches_one_shot():
    stream = "<think>cot with /Users/wangxinxin/code/ziva inside</think>当前目录是回答。"
    expected = _stream([stream])
    rng = random.Random(42)
    for _ in range(500):
        chunks, i = [], 0
        while i < len(stream):
            n = rng.randint(1, 8)
            chunks.append(stream[i : i + n])
            i += n
        assert _stream(chunks) == expected


def test_strip_think_tags():
    assert strip_think_tags("<think>cot</think>answer") == "answer"
    assert strip_think_tags("<mm:think>cot</mm:think>answer") == "answer"
    assert strip_think_tags("answer<think>truncated cot") == "answer"
