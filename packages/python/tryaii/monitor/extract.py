"""Routing text from a native ``messages[]`` payload (SPEC.md §2.1).

Pure and deterministic: the same messages always give the same text and
hash. The wrapper routes on this text; the text itself is never stored —
only its sha256 (``prompt_hash``) and, separately, its embedding.
"""

from __future__ import annotations

import hashlib
from typing import Any

from tryaii.classifiers.base import MAX_PROMPT_LENGTH

SYSTEM_ROLES = ("system", "developer")
USER_ROLE = "user"


def message_text(content: Any) -> str:
    """The text of one message's ``content``.

    A string is taken as is; a list of content parts keeps only the parts
    whose ``type`` is ``"text"`` (joined with ``\n``); anything else
    (None, images, tool payloads) is empty.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def prompt_hash(text: str) -> str:
    """sha256 hex of the UTF-8 routing text (``""`` hashes too)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def routing_text(messages: Any) -> tuple[str, str]:
    """Return ``(text, prompt_hash)`` for a chat-completions ``messages`` list.

    ``text`` = the FIRST system/developer message's text, a blank line, then
    the LAST user message's text; the user text alone when there is no
    system text; ``""`` when there is no user text at all (a call without a
    user turn is not routed). Truncated to ``MAX_PROMPT_LENGTH`` code points
    (the router's own limit). Non-dict messages are ignored.
    """
    system_text = None
    user_text = None
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role in SYSTEM_ROLES and system_text is None:
                system_text = message_text(msg.get("content"))
            elif role == USER_ROLE:
                user_text = message_text(msg.get("content"))
    if not user_text:
        text = ""
    elif system_text:
        text = system_text + "\n\n" + user_text
    else:
        text = user_text
    if len(text) > MAX_PROMPT_LENGTH:
        text = text[:MAX_PROMPT_LENGTH]
    return text, prompt_hash(text)
