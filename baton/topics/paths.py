"""Strict topic paths and single-segment names, independent of storage and admission.

check_topic accepts None to clear an optional topic. Callers requiring a path
keep that requirement; reference names use the single-segment predicate.
"""

from __future__ import annotations

import re
from typing import Any

from baton.errors import InvalidRequest

# A topic is one or two lowercase slug segments: `project` or `project/subtopic`. A segment is lowercase
# ASCII letters, digits and inner hyphens, so it can never carry a SQL LIKE metacharacter and a prefix
# filter is safe to match literally. It is a label for people, separate from the conversation's id.
MAX_TOPIC_SEGMENT = 40
MAX_TOPIC_SEGMENTS = 2
TOPIC_SEGMENT = re.compile(rf"[a-z0-9](?:[a-z0-9-]{{0,{MAX_TOPIC_SEGMENT - 2}}}[a-z0-9])?")


def check_topic(topic: Any, field: str = "topic") -> str | None:
    """A topic as stored: one or two lowercase slug segments, or None for no topic at all.

    None is how "no topic" arrives, and setting it clears one. Anything else must match the grammar
    whole: a refusal names the field. Because a segment admits neither of SQL's LIKE metacharacters, a
    validated topic is safe to match literally in a prefix filter.
    """
    if topic is None:
        return None
    if not isinstance(topic, str):
        raise InvalidRequest(
            f"{field} must be a path of 1-{MAX_TOPIC_SEGMENTS} lowercase slug segments, "
            "such as project or project/subtopic, or left out"
        )
    segments = topic.split("/")
    if not 1 <= len(segments) <= MAX_TOPIC_SEGMENTS or not all(TOPIC_SEGMENT.fullmatch(s) for s in segments):
        raise InvalidRequest(
            f"{field} {topic!r} must be 1-{MAX_TOPIC_SEGMENTS} segments joined by /, each "
            f"1-{MAX_TOPIC_SEGMENT} characters of a-z, 0-9 and inner hyphens"
        )
    return topic


def is_segment(value: Any) -> bool:
    """Whether a value is one lowercase slug segment, without a path separator."""
    return isinstance(value, str) and TOPIC_SEGMENT.fullmatch(value) is not None
