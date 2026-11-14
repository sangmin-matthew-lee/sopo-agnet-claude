"""Customer greetings are written locally, never by the model.

The model doesn't know the customer's name, and the names it can see in the email are usually our own
staff ("Hi Sarah, could you help me…"), so an agent reply once opened with "Hi Sarah,". The first line
of an agent email, if it is a greeting, is rewritten to "Hi <first name>," (from the email's From
display name, kept locally) or to a plain "Hi,".
"""

from __future__ import annotations

import re

# A whole line that is only a greeting plus up to three name-like words: "Hi Sarah,", "Dear Customer,".
_GREETING_RE = re.compile(r"^(?:hi|hello|hey|dear|greetings)(?:\s+[A-Za-z.'-]+){0,3}\s*[,!]?\s*$", re.IGNORECASE)
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z'-]+$")


def first_name(display_name: str | None) -> str | None:
    """'Matthew Lee' -> 'Matthew'. None if the display name doesn't look like a person's name."""
    words = (display_name or "").strip().strip('"').split()
    if not words or "@" in words[0] or not _NAME_RE.match(words[0]):
        return None
    return words[0][0].upper() + words[0][1:]


def fix_greeting(body: str, name: str | None) -> str:
    lines = body.split("\n")
    i = next((n for n, line in enumerate(lines) if line.strip()), None)
    if i is None or not _GREETING_RE.match(lines[i].strip()):
        return body
    lines[i] = f"Hi {name}," if name else "Hi,"
    return "\n".join(lines)
