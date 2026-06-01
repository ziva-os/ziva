"""Wildcard matching utilities."""

import re
import platform
from typing import Any, Dict, Optional


def match(text: str, pattern: str) -> bool:
    """Match text against a wildcard pattern."""
    if text:
        text = text.replace("\\", "/")
    if pattern:
        pattern = pattern.replace("\\", "/")

    # Escape special regex chars except * and ?
    escaped = re.escape(pattern).replace("\\*", "*").replace("\\?", "?")

    # * becomes .*
    # ? becomes .
    regex_pattern = escaped.replace("*", ".*").replace("?", ".")

    # If pattern ends with " *" (space + wildcard), make the trailing part optional
    if regex_pattern.endswith(" .*"):
        regex_pattern = regex_pattern[:-3] + "( .*)?"

    flags = re.S  # Dot matches all (including newline)
    if platform.system() == "Windows":
        flags |= re.I  # Case insensitive on Windows

    return bool(re.fullmatch(regex_pattern, text, flags=flags))


def all_matches(input_str: str, patterns: Dict[str, Any]) -> Optional[Any]:
    """Match input against multiple patterns and return the value of the last match."""
    sorted_patterns = sorted(patterns.items(), key=lambda x: (len(x[0]), x[0]))

    result = None
    for pattern, value in sorted_patterns:
        if match(input_str, pattern):
            result = value
    return result
