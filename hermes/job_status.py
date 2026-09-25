"""How each scheduled job has been doing: kept in memory by the app, read by /healthz and
the page banner. A job whose tick raises is still rescheduled, so without this only the
pod log would know that one has been failing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass
class JobStatus:
    """One scheduled job's record."""

    every: timedelta
    last_ok: datetime | None = None
    failing_since: datetime | None = None
    last_error: str | None = None

    def failing(self, now: datetime) -> bool:
        """Failing for longer than two intervals (a single bad tick is noise)."""
        return self.failing_since is not None and now - self.failing_since > 2 * self.every


def job_problems(statuses: dict[str, JobStatus]) -> list[str]:
    """One line per scheduled job that has been failing, for /healthz and the banner."""
    now = datetime.now(UTC)
    return [
        f"The {job_id} job has been failing since {st.failing_since:%Y-%m-%d %H:%M} UTC: "
        f"{st.last_error}"
        for job_id, st in statuses.items()
        if st.failing(now) and st.failing_since is not None
    ]
