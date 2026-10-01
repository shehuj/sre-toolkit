"""Test doubles.

FakeAws stands in for AwsClients with the same surface, so collectors and the
investigation pipeline can be tested end-to-end with no credentials, no network
and no spend — while still exercising the real pricing and budget code.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

UTC = timezone.utc


class FakeAws:
    def __init__(self, ctx, responses: dict[str, object] | None = None, region: str = "us-east-1"):
        self.ctx = ctx
        self.responses = responses or {}
        self._region = region
        self.calls: list[tuple[str, str]] = []

    @property
    def region(self) -> str:
        return self._region

    def account_id(self) -> str:
        return "123456789012"

    def call(self, service, operation, priced_as, quantity=1, cacheable=True, detail="", **kwargs):
        self.calls.append((service, operation))
        self.ctx.ledger.charge(priced_as, quantity, detail=detail)
        value = self.responses.get(operation, {})
        return value(**kwargs) if callable(value) else value

    def paginate(self, service, operation, priced_as, key, max_items=200, **kwargs):
        self.calls.append((service, operation))
        self.ctx.ledger.charge(priced_as, 1, detail=operation)
        return self.responses.get(operation, [])


def make_context(responses=None, **overrides):
    from sre_toolkit.cache import Cache
    from sre_toolkit.context import Context
    from sre_toolkit.ledger import Ledger
    from sre_toolkit.render import Console

    ctx = Context(
        region="us-east-1",
        console=Console(color=False),
        cache=Cache(ttl=0, enabled=False),
        ledger=Ledger(**{k: v for k, v in overrides.items() if k in ("max_spend", "dry_run")}),
        **{k: v for k, v in overrides.items() if k not in ("max_spend", "dry_run")},
    )
    ctx._aws = FakeAws(ctx, responses)
    return ctx


def window(minutes: int = 30, end: datetime | None = None):
    from sre_toolkit.models import Window

    end = end or datetime(2026, 10, 1, 7, 0, tzinfo=UTC)
    return Window(end - timedelta(minutes=minutes), end)


def metric_data(window_obj, specs_values: dict[str, tuple[float, float]], period: int = 60):
    """Build a GetMetricData-shaped response: {id: (baseline_value, during_value)}."""
    results = []
    for index, (_, (baseline, during)) in enumerate(specs_values.items()):
        timestamps, values = [], []
        cursor = window_obj.start - timedelta(seconds=window_obj.seconds)
        while cursor <= window_obj.end:
            timestamps.append(cursor)
            values.append(baseline if cursor < window_obj.start else during)
            cursor += timedelta(seconds=period)
        results.append({"Id": f"m{index}", "Timestamps": timestamps, "Values": values})
    return {"MetricDataResults": results}


def log_events(count: int, message: str, start: datetime, step_seconds: int = 10):
    return [
        {
            "timestamp": int((start + timedelta(seconds=i * step_seconds)).timestamp() * 1000),
            "message": message,
            "logStreamName": f"stream-{i % 3}",
        }
        for i in range(count)
    ]
