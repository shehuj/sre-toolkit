"""Run context: the single object collectors need in order to behave cheaply."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .cache import Cache
from .ledger import Ledger
from .models import Window
from .render import Console

UTC = timezone.utc


@dataclass
class Context:
    region: str | None = None
    profile: str | None = None
    console: Console = field(default_factory=Console)
    cache: Cache = field(default_factory=Cache)
    ledger: Ledger = field(default_factory=Ledger)
    dry_run: bool = False
    demo: bool = False
    deep: bool = False
    use_ai: bool = False
    model: str | None = None
    verbose: bool = False
    json_mode: bool = False

    _aws: Any = None

    @property
    def aws(self):
        from .aws.client import AwsClients

        if self._aws is None:
            self._aws = AwsClients(self)
        return self._aws

    def log(self, message: str) -> None:
        if self.verbose:
            self.console.warn(message)

    def resolve_window(
        self, start: str | None, end: str | None, minutes: int = 30
    ) -> Window:
        """Parse --start/--end, defaulting to the last `minutes` minutes."""
        end_dt = parse_time(end) if end else datetime.now(UTC)
        start_dt = parse_time(start) if start else end_dt - timedelta(minutes=minutes)
        if start_dt >= end_dt:
            raise ValueError("--start must be before --end")
        return Window(start_dt, end_dt)

    def finish(self) -> dict:
        cost = self.ledger.to_dict()
        cost["cache"] = self.cache.stats()
        return cost


def parse_time(value: str) -> datetime:
    """Accept ISO-8601, epoch seconds, or relative offsets like '-45m' / '2h ago'."""
    text = value.strip()
    rel = text.removesuffix(" ago").strip()
    if rel and (rel[0] == "-" or rel.endswith(("m", "h", "d"))) and _is_relative(rel):
        return datetime.now(UTC) - _parse_offset(rel)
    if text.isdigit() and len(text) >= 9:
        return datetime.fromtimestamp(int(text), UTC)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _is_relative(text: str) -> bool:
    body = text.lstrip("-")
    return len(body) > 1 and body[:-1].replace(".", "").isdigit() and body[-1] in "smhd"


def _parse_offset(text: str) -> timedelta:
    body = text.lstrip("-")
    amount, unit = float(body[:-1]), body[-1]
    return timedelta(
        seconds=amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    )
