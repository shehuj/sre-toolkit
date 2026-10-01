"""Core data shapes shared by collectors, the correlator and the renderers.

Everything is a plain dataclass that serialises to JSON, because the incident
artefact (`sre incident snapshot -o incident.json`) is the contract between the
free collection step and the paid analysis step. Collect once, analyse offline
as many times as you like.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

UTC = timezone.utc


class Severity(str, Enum):
    OK = "ok"
    INFO = "info"
    WARN = "warn"
    CRIT = "crit"

    @property
    def rank(self) -> int:
        return {"ok": 0, "info": 1, "warn": 2, "crit": 3}[self.value]


class SignalKind(str, Enum):
    METRIC = "metric"
    LOG = "log"
    CHANGE = "change"
    HEALTH = "health"
    RESOURCE = "resource"


@dataclass
class Window:
    start: datetime
    end: datetime

    @property
    def seconds(self) -> int:
        return max(1, int((self.end - self.start).total_seconds()))

    def __str__(self) -> str:
        return f"{self.start.strftime('%Y-%m-%d %H:%M')} → {self.end.strftime('%H:%M')} UTC"


@dataclass
class Signal:
    """One observation about the service during the incident window."""

    name: str
    kind: SignalKind
    source: str
    severity: Severity = Severity.INFO
    summary: str = ""
    baseline: float | None = None
    peak: float | None = None
    unit: str = ""
    delta_pct: float | None = None
    first_seen: datetime | None = None
    series: list[tuple[datetime, float]] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)

    def change_ratio(self) -> float | None:
        if self.baseline is None or self.peak is None:
            return None
        if self.baseline == 0:
            return None if self.peak == 0 else float("inf")
        return self.peak / self.baseline


@dataclass
class TimelineEvent:
    at: datetime
    source: str
    title: str
    detail: str = ""
    severity: Severity = Severity.INFO


@dataclass
class Finding:
    """A correlated hypothesis. Never a remediation — this toolkit never acts."""

    title: str
    confidence: float
    rationale: str
    evidence: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    source: str = "correlator"

    @property
    def confidence_label(self) -> str:
        if self.confidence >= 0.75:
            return "high"
        if self.confidence >= 0.45:
            return "medium"
        return "low"


@dataclass
class Resource:
    kind: str
    identifier: str
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class Snapshot:
    """The portable incident artefact."""

    service: str
    window: Window
    region: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    collectors_run: list[str] = field(default_factory=list)
    collector_errors: dict[str, str] = field(default_factory=dict)
    resources: list[Resource] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    events: list[TimelineEvent] = field(default_factory=list)
    log_patterns: list[dict[str, Any]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    narrative: str = ""
    cost: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def add(self, item: Signal | TimelineEvent | Finding | Resource) -> None:
        if isinstance(item, Signal):
            self.signals.append(item)
        elif isinstance(item, TimelineEvent):
            self.events.append(item)
        elif isinstance(item, Finding):
            self.findings.append(item)
        else:
            self.resources.append(item)

    def worst_severity(self) -> Severity:
        return max((s.severity for s in self.signals), key=lambda s: s.rank, default=Severity.OK)

    def sorted_events(self) -> list[TimelineEvent]:
        return sorted(self.events, key=lambda e: e.at)

    def signal(self, name: str) -> Signal | None:
        return next((s for s in self.signals if s.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        return _encode(self)

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=False)

    # -- deserialisation -------------------------------------------------
    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Snapshot:
        win = raw.get("window") or {}
        snap = cls(
            service=raw.get("service", "unknown"),
            window=Window(_dt(win.get("start")), _dt(win.get("end"))),
            region=raw.get("region", ""),
            created_at=_dt(raw.get("created_at")) or datetime.now(UTC),
            collectors_run=list(raw.get("collectors_run", [])),
            collector_errors=dict(raw.get("collector_errors", {})),
            log_patterns=list(raw.get("log_patterns", [])),
            narrative=raw.get("narrative", ""),
            cost=dict(raw.get("cost", {})),
            meta=dict(raw.get("meta", {})),
        )
        for r in raw.get("resources", []):
            snap.resources.append(
                Resource(r.get("kind", "?"), r.get("identifier", "?"), r.get("attributes", {}))
            )
        for s in raw.get("signals", []):
            snap.signals.append(
                Signal(
                    name=s["name"],
                    kind=SignalKind(s.get("kind", "metric")),
                    source=s.get("source", ""),
                    severity=Severity(s.get("severity", "info")),
                    summary=s.get("summary", ""),
                    baseline=s.get("baseline"),
                    peak=s.get("peak"),
                    unit=s.get("unit", ""),
                    delta_pct=s.get("delta_pct"),
                    first_seen=_dt(s.get("first_seen")),
                    series=[(_dt(t), v) for t, v in s.get("series", [])],
                    evidence=list(s.get("evidence", [])),
                    tags=dict(s.get("tags", {})),
                )
            )
        for e in raw.get("events", []):
            snap.events.append(
                TimelineEvent(
                    at=_dt(e["at"]),
                    source=e.get("source", ""),
                    title=e.get("title", ""),
                    detail=e.get("detail", ""),
                    severity=Severity(e.get("severity", "info")),
                )
            )
        for f in raw.get("findings", []):
            snap.findings.append(
                Finding(
                    title=f["title"],
                    confidence=float(f.get("confidence", 0.0)),
                    rationale=f.get("rationale", ""),
                    evidence=list(f.get("evidence", [])),
                    next_steps=list(f.get("next_steps", [])),
                    source=f.get("source", "correlator"),
                )
            )
        return snap


def _dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _encode(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _encode(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return (obj if obj.tzinfo else obj.replace(tzinfo=UTC)).isoformat()
    if isinstance(obj, dict):
        return {k: _encode(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_encode(v) for v in obj]
    if isinstance(obj, float) and obj != obj:  # NaN is not valid JSON
        return None
    return obj
