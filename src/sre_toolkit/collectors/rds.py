"""RDS collector — connection saturation, storage, replication, recent events.

All Describe* calls are free. The expensive part of an RDS investigation is
metrics, which go through collectors/metrics.py in one batched request.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..models import Resource, Severity, Signal, SignalKind, TimelineEvent, Window

UTC = timezone.utc

# max_connections for the default RDS parameter formula is derived from instance
# memory ({DBInstanceClassMemory/9531392} for MySQL, LEAST({DBInstanceClassMemory/9531392},5000)).
# Hard-coding a small table of common classes keeps this offline and free; pass
# --max-connections to be exact.
_CLASS_MEMORY_GIB: dict[str, float] = {
    "db.t3.micro": 1, "db.t3.small": 2, "db.t3.medium": 4, "db.t3.large": 8,
    "db.t4g.micro": 1, "db.t4g.small": 2, "db.t4g.medium": 4, "db.t4g.large": 8,
    "db.m5.large": 8, "db.m5.xlarge": 16, "db.m5.2xlarge": 32, "db.m5.4xlarge": 64,
    "db.m6g.large": 8, "db.m6g.xlarge": 16, "db.m6g.2xlarge": 32, "db.m6g.4xlarge": 64,
    "db.m6i.large": 8, "db.m6i.xlarge": 16, "db.m6i.2xlarge": 32, "db.m6i.4xlarge": 64,
    "db.r5.large": 16, "db.r5.xlarge": 32, "db.r5.2xlarge": 64, "db.r5.4xlarge": 128,
    "db.r6g.large": 16, "db.r6g.xlarge": 32, "db.r6g.2xlarge": 64, "db.r6g.4xlarge": 128,
    "db.r6i.large": 16, "db.r6i.xlarge": 32, "db.r6i.2xlarge": 64, "db.r6i.4xlarge": 128,
}

EVENT_SEVERITY: tuple[tuple[str, Severity], ...] = (
    ("failover", Severity.CRIT),
    ("restarted", Severity.CRIT),
    ("rebooted", Severity.WARN),
    ("out of storage", Severity.CRIT),
    ("low free storage", Severity.CRIT),
    ("replication", Severity.WARN),
    ("applying modification", Severity.WARN),
    ("backing up", Severity.INFO),
    ("backup completed", Severity.INFO),
    ("parameter group", Severity.WARN),
)


def estimate_max_connections(instance_class: str, engine: str = "") -> int | None:
    gib = _CLASS_MEMORY_GIB.get(instance_class)
    if not gib:
        return None
    raw = int(gib * 1024**3 / 9_531_392)  # the default MySQL/MariaDB formula
    if engine.startswith("postgres"):
        raw = min(int(gib * 1024**3 / 9_531_392), 5000)
    return min(raw, 5000)


def collect(
    ctx, instance: str, window: Window, max_connections: int | None = None
) -> dict[str, Any]:
    payload = ctx.aws.call(
        "rds", "describe_db_instances", "rds:DescribeDBInstances",
        detail=instance, DBInstanceIdentifier=instance,
    )
    instances = payload.get("DBInstances", [])
    out: dict[str, Any] = {"instance": {}, "resources": [], "signals": [], "events": [],
                           "max_connections": max_connections}
    if not instances:
        return out
    db = instances[0]
    out["instance"] = {
        "identifier": db.get("DBInstanceIdentifier"),
        "status": db.get("DBInstanceStatus"),
        "class": db.get("DBInstanceClass"),
        "engine": f"{db.get('Engine')} {db.get('EngineVersion', '')}".strip(),
        "multi_az": db.get("MultiAZ"),
        "storage_gb": db.get("AllocatedStorage"),
        "storage_type": db.get("StorageType"),
        "iops": db.get("Iops"),
        "read_replicas": db.get("ReadReplicaDBInstanceIdentifiers", []),
        "source_replica": db.get("ReadReplicaSourceDBInstanceIdentifier"),
        "backup_retention_days": db.get("BackupRetentionPeriod"),
        "deletion_protection": db.get("DeletionProtection"),
        "performance_insights": db.get("PerformanceInsightsEnabled"),
    }
    if out["max_connections"] is None:
        out["max_connections"] = estimate_max_connections(
            db.get("DBInstanceClass", ""), db.get("Engine", "")
        )
        out["max_connections_source"] = "estimated from instance class"
    else:
        out["max_connections_source"] = "supplied"

    out["resources"].append(
        Resource(kind="rds:instance", identifier=instance, attributes=out["instance"])
    )

    status = (db.get("DBInstanceStatus") or "").lower()
    if status not in ("available",):
        out["signals"].append(
            Signal(
                name="RDS instance status",
                kind=SignalKind.RESOURCE,
                source=f"rds/{instance}",
                severity=Severity.CRIT if status in ("failed", "incompatible-parameters")
                else Severity.WARN,
                summary=f"instance status is '{status}'",
                tags={"class": "rds_status"},
            )
        )
    if db.get("BackupRetentionPeriod", 0) == 0:
        out["signals"].append(
            Signal(
                name="RDS automated backups",
                kind=SignalKind.RESOURCE,
                source=f"rds/{instance}",
                severity=Severity.WARN,
                summary="automated backups are disabled (retention = 0 days)",
                tags={"class": "backup"},
            )
        )

    out["events"] = recent_events(ctx, instance, window)
    return out


def recent_events(ctx, instance: str, window: Window) -> list[TimelineEvent]:
    minutes = max(5, min(14 * 24 * 60, int(window.seconds / 60) + 60))
    payload = ctx.aws.call(
        "rds", "describe_events", "rds:DescribeEvents", detail=instance,
        SourceIdentifier=instance, SourceType="db-instance", Duration=minutes, MaxRecords=60,
    )
    out = []
    for raw in payload.get("Events", []):
        at = _as_dt(raw.get("Date"))
        if not at or at > window.end:
            continue
        message = raw.get("Message", "")
        out.append(
            TimelineEvent(at=at, source="rds", title=f"RDS: {message[:110]}",
                          detail=",".join(raw.get("EventCategories", [])),
                          severity=_severity(message))
        )
    return out


def connection_pressure(
    connections_signal: Signal | None, max_connections: int | None
) -> Signal | None:
    """Turn 'DatabaseConnections peaked at N' into 'N is 94% of the ceiling'.

    This is the finding that most often explains a post-deploy 5xx spike, and it
    costs nothing extra: it reuses a metric already fetched.
    """
    if connections_signal is None or not max_connections or connections_signal.peak is None:
        return None
    pct = connections_signal.peak / max_connections * 100
    if pct >= 90:
        severity = Severity.CRIT
    elif pct >= 75:
        severity = Severity.WARN
    else:
        severity = Severity.INFO
    return Signal(
        name="DB connection utilisation",
        kind=SignalKind.METRIC,
        source=connections_signal.source,
        severity=severity,
        summary=f"{connections_signal.peak:.0f} of ~{max_connections} connections ({pct:.0f}%)",
        baseline=(connections_signal.baseline / max_connections * 100)
        if connections_signal.baseline else None,
        peak=pct,
        unit="Percent",
        first_seen=connections_signal.first_seen,
        series=[(t, v / max_connections * 100) for t, v in connections_signal.series],
        tags={"class": "connection_saturation"},
    )


def snapshots(ctx, instance: str, limit: int = 10) -> list[dict[str, Any]]:
    payload = ctx.aws.call(
        "rds", "describe_db_snapshots", "rds:DescribeDBSnapshots", detail=instance,
        DBInstanceIdentifier=instance, MaxRecords=max(20, limit),
    )
    rows = []
    for snap in payload.get("DBSnapshots", []):
        rows.append(
            {
                "id": snap.get("DBSnapshotIdentifier"),
                "type": snap.get("SnapshotType"),
                "status": snap.get("Status"),
                "created_at": _iso(snap.get("SnapshotCreateTime")),
                "encrypted": snap.get("Encrypted"),
                "size_gb": snap.get("AllocatedStorage"),
            }
        )
    rows.sort(key=lambda r: r["created_at"] or "", reverse=True)
    return rows[:limit]


def _severity(message: str) -> Severity:
    lowered = message.lower()
    for needle, severity in EVENT_SEVERITY:
        if needle in lowered:
            return severity
    return Severity.INFO


def _as_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _iso(value: Any) -> str | None:
    dt = _as_dt(value)
    return dt.isoformat() if dt else None
