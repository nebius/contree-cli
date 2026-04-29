"""Readline history persistence backed by the session SQLite DB."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from contree_cli.session import SessionStore

log = logging.getLogger(__name__)


def load_history(store: SessionStore) -> None:
    """Populate readline history from the session database."""
    try:
        import readline
    except ImportError:
        return

    lines = store.load_shell_history()
    for line in lines:
        readline.add_history(line)
    log.debug("Loaded %d history lines from session DB", len(lines))


def save_history(store: SessionStore) -> None:
    """Persist current readline history into the session database.

    Compares what readline holds against what the DB already has and
    appends only the new tail entries.  Then trims to the maximum.
    """
    try:
        import readline
    except ImportError:
        return

    total = readline.get_current_history_length()
    existing = store.load_shell_history()
    existing_count = len(existing)

    # Readline history is 1-indexed.
    new_count = total - existing_count
    if new_count <= 0:
        return

    start = existing_count + 1
    for i in range(start, total + 1):
        item = readline.get_history_item(i)
        if item is not None:
            store.add_shell_history(item)

    store.trim_shell_history()
    log.debug("Saved %d new history lines to session DB", new_count)
