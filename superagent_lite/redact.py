"""Redaction of secret-looking values before anything is written to a log.

The worker adapters never receive the parent process' full environment, but
stdout/stderr from a child (or a task description echoed back by it) can still
contain credentials.  Every log write therefore goes through
:func:`sanitize_text`.
"""

from __future__ import annotations

import os
import re
from typing import Mapping

# Environment variable names that look like credentials: ``*_KEY``,
# ``*_TOKEN``, ``*_SECRET*`` (and a couple of close cousins).  The boundaries
# keep names such as ``MONKEY`` from matching.
_SECRET_NAME_RE = re.compile(
    r"(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)(?:_|$)",
    re.IGNORECASE,
)

# API-key-shaped strings, e.g. ``sk-abcdef123456``.
_SECRET_VALUE_RE = re.compile(r"sk-[A-Za-z0-9_\-]{4,}")

REDACTED = "***"

# Values shorter than this are not treated as secrets: replacing e.g. ``0`` or
# ``on`` everywhere in a log would destroy more than it protects.
_MIN_SECRET_LENGTH = 4


def secret_env_values(env: Mapping[str, str] | None = None) -> list[str]:
    """Return values of environment variables whose names look secret.

    Longest values come first so that a value containing another value is
    replaced as a whole.
    """

    source = os.environ if env is None else env
    values = {
        value
        for name, value in source.items()
        if value
        and len(value) >= _MIN_SECRET_LENGTH
        and _SECRET_NAME_RE.search(name)
    }
    return sorted(values, key=len, reverse=True)


def sanitize_text(text: str, env: Mapping[str, str] | None = None) -> str:
    """Replace secret environment values and ``sk-`` strings with ``***``."""

    if not text:
        return text
    for value in secret_env_values(env):
        text = text.replace(value, REDACTED)
    return _SECRET_VALUE_RE.sub(REDACTED, text)
