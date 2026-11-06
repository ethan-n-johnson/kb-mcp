"""PostgreSQL-only state shared by every instance: upload tickets and settings.

With one instance these live in process memory (`kb/uploads.py`,
`SearchState.fallback_toggle`). With several instances behind the load balancer
they must be in the database, and the expiry and single-use checks must be done
by the database clock in one statement, so two instances can never both accept
one ticket. The SQL is PostgreSQL-specific (`now()`, `RETURNING`, intervals), so
it lives here, next to the dialect module, and callers pass a `Store`.

Tickets are keyed by the SHA-256 of the ticket text; the ticket itself is never
stored. `tags` is a JSON list.
"""

from __future__ import annotations

import json
from typing import Any

from kb.store.base import Store


def issue_ticket(
    store: Store,
    token_hash: str,
    actor: str,
    filename: str,
    *,
    title: str | None,
    tags: list[str],
    force: bool,
    ttl: float,
    max_pending: int,
) -> None:
    """Insert a ticket after pruning expired rows and making room under `max_pending`.

    One transaction (write transactions are serialized across instances), so
    the count and the insert cannot interleave with another instance's issue.
    The oldest pending tickets go first: all tickets live equally long, so the
    earliest `expires_at` is the oldest.
    """
    with store.transaction():
        store.execute("DELETE FROM upload_tickets WHERE expires_at <= now()")
        pending = store.fetchone(
            "SELECT count(*) AS n FROM upload_tickets WHERE used_at IS NULL"
        )["n"]
        excess = int(pending) - max_pending + 1
        if excess > 0:
            store.execute(
                "DELETE FROM upload_tickets WHERE token_hash IN ("
                "SELECT token_hash FROM upload_tickets WHERE used_at IS NULL"
                " ORDER BY expires_at, token_hash LIMIT ?)",
                (excess,),
            )
        store.execute(
            "INSERT INTO upload_tickets"
            " (token_hash, actor, filename, title, tags, force, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, now() + ? * interval '1 second')",
            (token_hash, actor, filename, title, json.dumps(tags), force, float(ttl)),
        )


def take_ticket(store: Store, token_hash: str) -> dict[str, Any] | None:
    """Use up a ticket in one statement; None if unknown, already used or expired."""
    row = store.fetchone(
        "UPDATE upload_tickets SET used_at = now()"
        " WHERE token_hash = ? AND used_at IS NULL AND expires_at > now()"
        " RETURNING actor, filename, title, tags, force",
        (token_hash,),
    )
    if row is None:
        return None
    return {
        "actor": row["actor"],
        "filename": row["filename"],
        "title": row["title"],
        "tags": tuple(json.loads(row["tags"])),
        "force": bool(row["force"]),
    }


def pending_tickets(store: Store) -> int:
    """Tickets that are neither used nor expired."""
    row = store.fetchone(
        "SELECT count(*) AS n FROM upload_tickets WHERE used_at IS NULL AND expires_at > now()"
    )
    return int(row["n"])


def clear_tickets(store: Store) -> None:
    store.execute("DELETE FROM upload_tickets")


def get_setting(store: Store, key: str) -> str | None:
    """The value of a shared setting, or None when it was never set."""
    row = store.fetchone("SELECT value FROM settings WHERE key = ?", (key,))
    return None if row is None else row["value"]


def set_setting(store: Store, key: str, value: str) -> None:
    """Create or replace a shared setting."""
    store.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, now())"
        " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
        (key, value),
    )
