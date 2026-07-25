import pytest

from ziva.adapters._think_parser import strip_think_tags


class TestStripThinkTags:
    def test_removes_think_block(self):
        text = "before <think>reasoning</think> after"
        assert strip_think_tags(text) == "before  after"

    def test_removes_mm_think_block(self):
        text = "before <mm:think>reasoning</mm:think> after"
        assert strip_think_tags(text) == "before  after"

    def test_removes_multiple_blocks(self):
        text = "a <think>x</think> b <mm:think>y</mm:think> c"
        assert strip_think_tags(text) == "a  b  c"

    def test_handles_unclosed_tag(self):
        text = "before <think> truncated"
        assert strip_think_tags(text) == "before"

    def test_returns_plain_text_unchanged(self):
        text = "no reasoning tags here"
        assert strip_think_tags(text) == "no reasoning tags here"

    def test_strips_surrounding_whitespace(self):
        text = "  \n<think>x</think>\n  result\n  "
        assert strip_think_tags(text) == "result"

    def test_handles_empty_string(self):
        assert strip_think_tags("") == ""
