"""One-time upload links: how a chat client sends a whole file to the server.

The MCP `upload` tool needs the document inside the tool call, which works for
notes but not for a PDF (the model would have to write out megabytes of
base64). Instead, the `upload_file` tool asks for a link: the server checks the
caller's write token, remembers the file name, title and tags under a random
ticket, and returns `POST <public url>/api/uploads/<ticket>`. The client (Claude
Code, which has a shell) then sends the file's bytes there with `curl`; the
server stores it through the normal `service.upload` path as the token's owner.

A ticket works like an S3 presigned URL: whoever holds it may upload one file,
once, within `TICKET_TTL_SECONDS`, as the token that asked for it. It carries
no other right, and the write token itself never leaves the MCP connection.
Tickets live in process memory, which is fine with exactly one uvicorn worker;
a restart drops the pending ones (the client asks for a new link).
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

TICKET_TTL_SECONDS = 15 * 60
MAX_PENDING = 50


@dataclass(frozen=True)
class Ticket:
    """What an upload link was issued for."""

    actor: str
    filename: str
    title: str | None = None
    tags: tuple[str, ...] = ()
    force: bool = False
    expires_at: float = 0.0


@dataclass
class TicketStore:
    """Single-use tickets with an expiry; thread safe."""

    ttl: float = TICKET_TTL_SECONDS
    max_pending: int = MAX_PENDING
    clock: Callable[[], float] = time.monotonic
    _tickets: dict[str, Ticket] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def issue(
        self,
        actor: str,
        filename: str,
        *,
        title: str | None = None,
        tags: list[str] | None = None,
        force: bool = False,
    ) -> tuple[str, float]:
        """Create a ticket; returns `(ticket, seconds until it expires)`.

        Expired tickets are dropped first; when `max_pending` are still open the
        oldest is dropped, so a misbehaving client cannot grow memory.
        """
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock:
            self._prune(now)
            while len(self._tickets) >= self.max_pending:
                self._tickets.pop(next(iter(self._tickets)))
            self._tickets[token] = Ticket(
                actor=actor,
                filename=filename,
                title=title,
                tags=tuple(tags or ()),
                force=force,
                expires_at=now + self.ttl,
            )
        return token, self.ttl

    def take(self, token: str) -> Ticket | None:
        """Remove and return the ticket, or None if it is unknown, used or expired."""
        now = self.clock()
        with self._lock:
            ticket = self._tickets.pop(token, None)
            self._prune(now)
        if ticket is None or ticket.expires_at <= now:
            return None
        return ticket

    def pending(self) -> int:
        with self._lock:
            self._prune(self.clock())
            return len(self._tickets)

    def clear(self) -> None:
        with self._lock:
            self._tickets.clear()

    def _prune(self, now: float) -> None:
        for key in [k for k, t in self._tickets.items() if t.expires_at <= now]:
            del self._tickets[key]


STORE = TicketStore()
"""The process-wide store (one uvicorn worker, so one store)."""
