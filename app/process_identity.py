from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """A process incarnation key; creation time is the PID-reuse discriminator."""

    pid: int
    created_at: float | None

    def __post_init__(self) -> None:
        pid = int(self.pid)
        if pid < 0:
            raise ValueError("PID must be non-negative")
        object.__setattr__(self, "pid", pid)
        if self.created_at is None:
            return
        try:
            created_at = float(self.created_at)
        except (TypeError, ValueError):
            object.__setattr__(self, "created_at", None)
            return
        if not math.isfinite(created_at) or created_at < 0:
            object.__setattr__(self, "created_at", None)
        else:
            object.__setattr__(self, "created_at", created_at)

    @property
    def verified(self) -> bool:
        return self.created_at is not None
