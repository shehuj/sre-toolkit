"""Change collector — "what did someone or something do just before this broke?"

CloudTrail LookupEvents is free for management events, so correlating an
incident against the change log costs nothing. It is rate-limited (a couple of
requests per second), so lookups are narrow: one event-name filter per call,
capped pages, cached on disk.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..models import Severity, TimelineEvent, Window

UTC = timezone.utc

# Mutating API calls that plausibly cause an outage, grouped by what they touch.
WATCHED_EVENTS: dict[str, tuple[str, ...]] = {
    "deploy": ("UpdateService", "CreateService", "RegisterTaskDefinition",
               "CreateDeployment", "UpdateFunctionCode", "UpdateFunctionConfiguration"),
    "scaling": ("PutScalingPolicy", "RegisterScalableTarget", "UpdateAutoScalingGroup",
                "SetDesiredCapacity"),
    "database": ("ModifyDBInstance", "RebootDBInstance", "ModifyDBParameterGroup",
                 "FailoverDBCluster", "DeleteDBInstance"),
    "network": ("AuthorizeSecurityGroupIngress", "RevokeSecurityGroupIngress",
                "ModifyTargetGroupAttributes", "ModifyListener", "DeleteRule", "CreateRule"),
    "config": ("PutParameter", "UpdateSecret", "PutSecretValue", "UpdateStack",
               "PutBucketPolicy", "AttachRolePolicy", "PutRolePolicy"),
}

_SEVERITY = {
    "deploy": Severity.WARN,
    "scaling": Severity.INFO,
    "database": Severity.WARN,
    "network": Severity.WARN,
    "config": Severity.WARN,
}


def collect(
    ctx,
    window: Window,
    lookback_minutes: int = 60,
    categories: tuple[str, ...] = ("deploy", "scaling", "database", "network", "config"),
    resource_filter: str | None = None,
) -> list[TimelineEvent]:
    """Changes between (window.start - lookback) and window.end.

    The lookback matters: the deploy that broke you usually landed *before* the
    alert fired, so looking only inside the alert window hides the cause.
    """
    start = window.start - timedelta(minutes=lookback_minutes)
    events: list[TimelineEvent] = []
    seen: set[str] = set()

    for category in categories:
        for name in WATCHED_EVENTS.get(category, ()):
            payload = ctx.aws.call(
                "cloudtrail", "lookup_events", "cloudtrail:LookupEvents",
                detail=name,
                LookupAttributes=[{"AttributeKey": "EventName", "AttributeValue": name}],
                StartTime=start, EndTime=window.end, MaxResults=10,
            )
            for raw in payload.get("Events", []):
                event_id = raw.get("EventId", "")
                if event_id in seen:
                    continue
                seen.add(event_id)
                at = _as_dt(raw.get("EventTime"))
                if not at:
                    continue
                resources = ", ".join(
                    r.get("ResourceName", "") for r in raw.get("Resources", [])
                )[:120]
                if resource_filter and resource_filter not in (resources + raw.get("Username", "")):
                    continue
                events.append(
                    TimelineEvent(
                        at=at,
                        source=f"cloudtrail/{category}",
                        title=f"{name} by {raw.get('Username') or 'unknown principal'}",
                        detail=resources,
                        severity=_SEVERITY.get(category, Severity.INFO),
                    )
                )
    return sorted(events, key=lambda e: e.at)


def deployment_markers(events: list[TimelineEvent]) -> list[TimelineEvent]:
    return [e for e in events if "deploy" in e.source or "Deployment" in e.title]


def _as_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None
