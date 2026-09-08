"""Thin async HTTP clients. No business logic lives here."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class HealthResult:
    name: str
    ok: bool
    configured: bool = True
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "configured": self.configured,
            "detail": self.detail,
            **self.data,
        }


def not_configured(name: str) -> HealthResult:
    return HealthResult(name=name, ok=True, configured=False, detail="not configured")
