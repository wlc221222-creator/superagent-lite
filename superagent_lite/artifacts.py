"""Artifact listing and safe path resolution for a ticket's workdir.

The HTTP API exposes files a worker produced.  The only filesystem boundary
that matters is the ticket's working directory: a request may name any file
*inside* that directory, and nothing else.  Resolution therefore happens on
the fully resolved path (symlinks included) and every candidate must remain a
child of the resolved workdir:

* ``../`` (raw or percent-encoded) climbs out and is refused;
* an absolute path replaces the base in :mod:`pathlib` and is refused;
* a symlink inside the workdir that points outside is refused;
* missing files, directories and special files are refused.

Callers pass user-supplied names, so the workdir itself is never trusted to be
clean either -- it is resolved first.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import quote

from .models import Ticket

#: Types whose text content is served with an explicit charset.
_TEXTUAL_TYPES = frozenset(
    {
        "application/json",
        "application/javascript",
        "application/xml",
        "application/xhtml+xml",
    }
)


class ArtifactError(Exception):
    """Base class for a refused or missing artifact request."""

    status = 404


class ArtifactPathError(ArtifactError):
    """The name escapes the workdir or is otherwise unusable."""

    status = 404


class ArtifactNotFound(ArtifactError):
    """The name is safe but no regular file exists at it."""

    status = 404


def resolve_artifact(workdir: str | Path, name: str) -> Path:
    """Resolve ``name`` inside ``workdir`` or raise :class:`ArtifactError`.

    The returned path exists, is a regular file, and is inside the workdir.
    """

    if not isinstance(name, str) or not name.strip():
        raise ArtifactPathError("artifact name must not be empty")
    if "\x00" in name:
        raise ArtifactPathError("artifact name contains a NUL byte")

    try:
        root = Path(workdir).resolve()
    except (OSError, RuntimeError) as exc:  # pragma: no cover - defensive
        raise ArtifactNotFound(f"ticket workdir is unavailable: {exc}") from exc
    if not root.is_dir():
        raise ArtifactNotFound("ticket workdir does not exist")

    try:
        candidate = (root / name).resolve()
    except (OSError, RuntimeError) as exc:
        raise ArtifactPathError(f"invalid artifact path: {exc}") from exc

    try:
        candidate.relative_to(root)
    except ValueError:
        raise ArtifactPathError(
            "artifact path escapes the ticket workdir"
        ) from None

    if not candidate.is_file():
        raise ArtifactNotFound("artifact not found")
    return candidate


def artifact_entries(ticket: Ticket) -> list[dict[str, object]]:
    """Describe every artifact recorded on ``ticket``.

    A recorded artifact may have been deleted (or was never created) after the
    run; it is still listed so the client can see it was promised, with
    ``exists: false`` and ``size: null``.
    """

    entries: list[dict[str, object]] = []
    for name in ticket.artifacts:
        entry: dict[str, object] = {
            "name": name,
            "size": None,
            "exists": False,
            "url": f"/tickets/{ticket.id}/artifacts/{quote(name, safe='/')}",
        }
        try:
            path = resolve_artifact(ticket.workdir, name)
            entry["size"] = path.stat().st_size
            entry["exists"] = True
        except (ArtifactError, OSError):
            pass
        entries.append(entry)
    return entries


def content_type_for(path: str | Path) -> str:
    """Guess a Content-Type, defaulting to ``application/octet-stream``."""

    guessed, _encoding = mimetypes.guess_type(str(path))
    if not guessed:
        return "application/octet-stream"
    if guessed.startswith("text/") or guessed in _TEXTUAL_TYPES:
        return f"{guessed}; charset=utf-8"
    return guessed


def content_disposition(path: str | Path, *, attachment: bool = True) -> str:
    """Build a ``Content-Disposition`` header safe for any filename.

    Only the basename is used, so a name that somehow reaches this function
    with a directory part cannot inject a path into the header.  Non-ASCII
    names are carried in the RFC 5987 ``filename*`` parameter, with a
    sanitized ASCII fallback for old clients.
    """

    basename = Path(str(path).replace("\\", "/")).name or "artifact"
    fallback = "".join(
        ch if 0x20 <= ord(ch) < 0x7F and ch not in '"\\' else "_"
        for ch in basename
    )
    disposition = "attachment" if attachment else "inline"
    encoded = quote(basename, safe="")
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"
