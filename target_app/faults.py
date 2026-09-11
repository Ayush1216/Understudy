"""Sessions and the fault injection armed on them.

Injection state lives on the session, never process-wide: with a frameset, the `nav` frame's
load would otherwise consume a transient fault armed for the `content` request.
"""

from __future__ import annotations

import random
import secrets
from dataclasses import dataclass, field

MODES = (
    "not_found", "validation", "permission", "maintenance",
    "session_expired", "app_error", "slow", "error_rate",
)
# `?inject=error_rate` carries no rate, so it is only meaningful when armed via /_inject.
PER_REQUEST_MODES = frozenset(MODES) - {"error_rate"}


@dataclass
class Session:
    id: str
    operator: str | None = None  # None = not signed on
    role: str = ""
    tokens: set[str] = field(default_factory=set)  # outstanding single-use form tokens
    mode: str | None = None
    ttl: int = 0  # requests left; -1 = sticky until reset
    rate: float = 0.0
    rng: random.Random = field(default_factory=random.Random)

    def new_token(self) -> str:
        t = secrets.token_hex(8)
        self.tokens.add(t)
        return t

    def take_token(self, t: str | None) -> bool:
        if t in self.tokens:
            self.tokens.discard(t)
            return True
        return False

    def arm(self, mode: str, ttl: int = 1, rate: float = 0.0) -> None:
        # error_rate is a probability per request, so a ttl would be meaningless: sticky.
        self.mode, self.ttl, self.rate = mode, (-1 if mode == "error_rate" else ttl), rate
        # Seeded on arm, not on session creation: re-arming after a reset replays the same rolls.
        self.rng.seed(self.id)

    def disarm(self) -> None:
        self.mode, self.ttl, self.rate = None, 0, 0.0

    def take(self, *, is_post: bool, override: str | None) -> str | None:
        """The fault to apply to THIS request. A query override applies to this request only
        and leaves the armed state untouched."""
        if override in PER_REQUEST_MODES:
            return override
        if self.mode is None:
            return None
        if self.mode == "error_rate":
            return "app_error" if is_post and self.rng.random() < self.rate else None
        mode = self.mode
        if self.ttl > 0:
            self.ttl -= 1
            if self.ttl == 0:
                self.disarm()
        return mode


# ponytail: sessions never expire; add an idle sweep if this outlives a demo.
SESSIONS: dict[str, Session] = {}


def new_session() -> Session:
    s = Session(secrets.token_hex(16))
    SESSIONS[s.id] = s
    return s


def reset() -> None:
    for s in SESSIONS.values():
        s.disarm()
