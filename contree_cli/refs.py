"""Operation-reference parsing for CLI positional arguments.

Every CLI command that accepts operation UUIDs also accepts session-
history references in the same positional slot. The accepted forms
are:

- bare ``@``, ``:``, or ``HEAD``   -- the active branch tip.
- ``@N``, ``:N`` or bare ``N``     -- absolute history id.
- ``@-N``, ``:-N``, ``HEAD~N``     -- N steps back from the tip.
- ``HEAD~``                        -- shorthand for ``HEAD~1``.
- ``@+N``, ``:+N``                 -- N steps forward from the tip.

This module centralises the parsing so any UUID-list-accepting
command behaves identically. ``resolve_operation_uuids`` is the entry point
used by ``from_args`` methods; ``resolve_operation_uuid`` is the
single-token form used by ``cmd_show``.
"""

from __future__ import annotations

import re
from uuid import UUID

from contree_cli import SESSION_STORE
from contree_cli.session import SessionStore

# All accepted history references (apart from the git-style ``HEAD~``/
# ``HEAD~N`` shorthand handled separately below) fit this shape:
# an optional ``HEAD``/``@``/``:`` prefix followed by an optional signed
# integer. Examples that match: ``HEAD``, ``HEAD5``, ``HEAD+2``, ``@``,
# ``@5``, ``@-1``, ``:``, ``:7``, ``:+3``, bare ``5``. The named groups
# make the post-match logic obvious.
HISTORY_REF_RE = re.compile(r"^(?P<prefix>HEAD|@|:)?(?P<sign>[+-])?(?P<value>\d+)?$")


def history_spec_from_ref(raw: str) -> str | None:
    """Translate a user-facing history reference into a SessionStore spec.

    Returns ``None`` when ``raw`` does not look like a history reference
    and should be passed through (e.g. a UUID).
    """
    # Git-style HEAD~ / HEAD~N shorthand is normalised to a back-step
    # spec, so HEAD~ == HEAD~1 == HEAD-1 (= spec "-1").
    if raw.startswith("HEAD~"):
        suffix = raw[len("HEAD~") :]
        if suffix == "":
            return "-1"
        if suffix.isdigit():
            return f"-{suffix}"
        return None

    m = HISTORY_REF_RE.match(raw)
    if m is None:
        return None
    prefix = m.group("prefix")
    sign = m.group("sign")
    value = m.group("value")

    # Bare sign with no number ("+", "-") is not a reference; bare
    # signed numbers ("-3", "+1") aren't either because argparse would
    # treat them as options and they read like flags to humans. Bare
    # unsigned numerics ("5") are allowed as a shorthand for absolute id.
    if prefix is None:
        if sign is not None or value is None:
            return None
        return value

    # Prefix-only: HEAD, @, or :. Means "the tip".
    if value is None:
        if sign is not None:
            return None
        return ""

    # Prefix + value (with optional sign). resolve_history_spec validates
    # the numeric range (e.g. zero rejected), keeping the lexer pure.
    return f"{sign or ''}{value}" if sign else value


def looks_like_history_ref(value: str) -> bool:
    """True for session-history references; see module docstring for forms."""
    return history_spec_from_ref(value) is not None


def resolve_operation_uuid(raw: str, store: SessionStore) -> str:
    """Resolve a single token (UUID or history reference) to an operation UUID.

    Returns ``raw`` unchanged when it does not look like a history
    reference (so callers can pass real UUIDs through unaltered).
    Raises :class:`ValueError` when the active session is missing, the
    referenced history entry does not exist, or it has no operation
    UUID attached.
    """
    spec = history_spec_from_ref(raw)
    if spec is None:
        return raw
    session = store.session
    if session is None:
        raise ValueError(
            "No active session; cannot resolve history entry. Run `contree use` first.",
        )
    entry = store.resolve_history_spec(spec)
    if not entry.operation_uuid:
        raise ValueError(f"History entry {entry.id} has no operation UUID")
    return entry.operation_uuid


def resolve_token(token: str, store: SessionStore) -> str:
    """Resolve a single positional token to an operation UUID.

    Returns the input unchanged when it is already a real UUID;
    resolves history references against the active session and
    returns the underlying operation UUID. Raises :class:`ValueError`
    for malformed UUIDs and unresolvable references; the message is
    informative for references (e.g. "History entry 99 not found")
    and a generic ``UUID()`` parse error for literal tokens.
    """
    if history_spec_from_ref(token) is None:
        UUID(token)
        return token
    resolved = resolve_operation_uuid(token, store)
    UUID(resolved)
    return resolved


def resolve_operation_uuids(items: list[str]) -> list[str]:
    """Flatten and resolve positional operation references.

    Each ``item`` is split on whitespace, then each token is resolved
    via :func:`resolve_token`. Tokens that fail to resolve are
    collected and reported together via a single :class:`ValueError`,
    so the user sees every bad token in one shot instead of
    discovering them one at a time. History-reference errors keep
    their context (e.g. "@99: History entry 99 not found"); literal
    UUID parse failures are reported as just the token.

    Splitting on whitespace handles the common case where an agent or
    shell user passes multiple UUIDs as one quoted string (e.g.
    ``op wait "$UUIDS"`` where ``$UUIDS`` is a multi-line value).
    """
    tokens = [t for item in items for t in item.split() if t]
    if not tokens:
        return []
    store = SESSION_STORE.get()
    out: list[str] = []
    invalid: list[str] = []
    for token in tokens:
        try:
            out.append(resolve_token(token, store))
        except ValueError as exc:
            if history_spec_from_ref(token) is not None:
                invalid.append(f"{token}: {exc}")
            else:
                invalid.append(token)
    if invalid:
        plural = "s" if len(invalid) > 1 else ""
        raise ValueError(f"Invalid operation reference{plural}: {' '.join(invalid)}")
    return out
