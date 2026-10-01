#!/usr/bin/env python3
"""Regenerate src/sre_toolkit/fixtures/demo_incident.json.

The demo snapshot is a real Snapshot artefact, not a mock report: `sre --demo`
runs the genuine correlator, renderer and (optionally) AI path over it. That
means the whole pipeline is exercised in CI and in a portfolio demo without an
AWS account and without spending a cent.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

UTC = timezone.utc
START = datetime(2026, 10, 1, 6, 30, tzinfo=UTC)
END = datetime(2026, 10, 1, 7, 0, tzinfo=UTC)
PERIOD = 60


def series(fn, start=START - timedelta(minutes=30), end=END):
    out, cursor = [], start
    while cursor <= end:
        out.append([cursor.isoformat(), round(float(fn(cursor)), 4)])
        cursor += timedelta(seconds=PERIOD)
    return out


def minutes_in(ts) -> float:
    """Minutes since the incident start; negative during the baseline window."""
    return (ts - START).total_seconds() / 60


def ramp(ts, onset, floor, ceiling, speed=4.0):
    m = minutes_in(ts)
    if m < onset:
        return floor
    return floor + (ceiling - floor) * (1 - math.exp(-(m - onset) / speed))


def recover(ts, onset, peak_min, floor, ceiling, speed=4.0):
    m = minutes_in(ts)
    if m < onset:
        return floor
    if m <= peak_min:
        return ramp(ts, onset, floor, ceiling, speed)
    decay = math.exp(-(m - peak_min) / 5.0)
    return floor + (ceiling - floor) * decay


def jitter(ts, amount=0.04):
    return 1 + amount * math.sin(ts.timestamp() / 97.0)


snapshot = {
    "service": "customer-api",
    "window": {"start": START.isoformat(), "end": END.isoformat()},
    "region": "us-east-1",
    "created_at": END.isoformat(),
    "collectors_run": ["ecs", "alb", "rds", "cloudtrail", "logs", "cloudwatch"],
    "collector_errors": {},
    "resources": [
        {
            "kind": "ecs:service",
            "identifier": "production/customer-api",
            "attributes": {
                "status": "ACTIVE", "launch_type": "FARGATE", "desired": 6, "running": 6,
                "pending": 0, "task_definition": "customer-api:184",
                "platform_version": "1.4.0",
            },
        },
        {
            "kind": "rds:instance",
            "identifier": "prod-customer-db",
            "attributes": {
                "status": "available", "class": "db.r6g.large", "engine": "postgres 15.5",
                "multi_az": True, "storage_gb": 200, "storage_type": "gp3",
                "backup_retention_days": 7, "performance_insights": True,
            },
        },
        {
            "kind": "elbv2:targetgroup",
            "identifier": "customer-api-tg",
            "attributes": {
                "protocol": "HTTP", "port": 8080, "health_check_path": "/healthz",
                "health_check_interval_s": 15, "health_check_timeout_s": 5,
                "healthy_threshold": 2, "unhealthy_threshold": 2,
                "target_states": {"healthy": 4, "unhealthy": 2},
            },
        },
    ],
    "signals": [
        {
            "name": "ALB requests", "kind": "metric", "source": "cloudwatch", "severity": "info",
            "summary": "1.2k → 1.3k", "baseline": 1210.0, "peak": 1318.0, "unit": "Count",
            "series": series(lambda t: 1210 * jitter(t, 0.06)),
            "tags": {"namespace": "AWS/ApplicationELB", "metric": "RequestCount"},
        },
        {
            "name": "HTTP 5xx (target)", "kind": "metric", "source": "cloudwatch",
            "severity": "crit", "summary": "peaked at 226 (critical ≥ 50)",
            "baseline": 2.0, "peak": 226.0, "unit": "Count",
            "first_seen": (START + timedelta(minutes=7)).isoformat(),
            "series": series(lambda t: recover(t, 7, 18, 2, 226)),
            "tags": {"namespace": "AWS/ApplicationELB", "metric": "HTTPCode_Target_5XX_Count"},
        },
        {
            "name": "p95 latency", "kind": "metric", "source": "cloudwatch", "severity": "crit",
            "summary": "peaked at 3.80s (critical ≥ 3.00s)",
            "baseline": 0.24, "peak": 3.8, "unit": "Seconds",
            "first_seen": (START + timedelta(minutes=5)).isoformat(),
            "series": series(lambda t: recover(t, 5, 18, 0.24, 3.8)),
            "tags": {"namespace": "AWS/ApplicationELB", "metric": "TargetResponseTime"},
        },
        {
            "name": "Healthy targets", "kind": "metric", "source": "cloudwatch",
            "severity": "warn", "summary": "dropped to 4.0 (warn ≤ 2.0)",
            "baseline": 6.0, "peak": 4.0, "unit": "Count",
            "first_seen": (START + timedelta(minutes=9)).isoformat(),
            "series": series(
                lambda t: 6 if minutes_in(t) < 9 else (4 if minutes_in(t) < 20 else 6)
            ),
            "tags": {"namespace": "AWS/ApplicationELB", "metric": "HealthyHostCount"},
        },
        {
            "name": "ECS CPU", "kind": "metric", "source": "cloudwatch", "severity": "warn",
            "summary": "peaked at 88.4% (warn ≥ 80.0%)",
            "baseline": 34.0, "peak": 88.4, "unit": "Percent",
            "first_seen": (START + timedelta(minutes=3)).isoformat(),
            "series": series(lambda t: recover(t, 3, 17, 34, 88.4) * jitter(t, 0.02)),
            "tags": {"namespace": "AWS/ECS", "metric": "CPUUtilization"},
        },
        {
            "name": "DB connections", "kind": "metric", "source": "cloudwatch", "severity": "warn",
            "summary": "rose from 42.0 to 188.0", "baseline": 42.0, "peak": 188.0, "unit": "Count",
            "first_seen": (START + timedelta(minutes=4)).isoformat(),
            "series": series(lambda t: recover(t, 4, 18, 42, 188, speed=3.0)),
            "tags": {"namespace": "AWS/RDS", "metric": "DatabaseConnections"},
        },
        {
            "name": "DB connection utilisation", "kind": "metric", "source": "cloudwatch",
            "severity": "crit", "summary": "188 of ~200 connections (94%)",
            "baseline": 21.0, "peak": 94.0, "unit": "Percent",
            "first_seen": (START + timedelta(minutes=4)).isoformat(),
            "series": series(lambda t: recover(t, 4, 18, 21, 94, speed=3.0)),
            "tags": {"class": "connection_saturation"},
        },
        {
            "name": "RDS CPU", "kind": "metric", "source": "cloudwatch", "severity": "info",
            "summary": "28.0% → 41.2%", "baseline": 28.0, "peak": 41.2, "unit": "Percent",
            "series": series(lambda t: recover(t, 5, 18, 28, 41.2) * jitter(t, 0.03)),
            "tags": {"namespace": "AWS/RDS", "metric": "CPUUtilization"},
        },
        {
            "name": "Log error volume", "kind": "log", "source": "logs", "severity": "crit",
            "summary": "1,284 error/warn lines across 26 buckets",
            "baseline": 0.0, "peak": 214.0, "unit": "Count",
            "first_seen": (START + timedelta(minutes=5)).isoformat(),
            "series": series(lambda t: recover(t, 5, 17, 0, 214), start=START),
            "evidence": [
                "ERROR HikariPool-1 - Connection is not available, request timed out after 30000ms",
                "ERROR o.s.w.s.m.s.DefaultHandlerExceptionResolver - 500 returned for "
                "/v2/customers",
            ],
        },
        {
            "name": "log:connection_pool_exhausted", "kind": "log", "source": "logs",
            "severity": "crit",
            "summary": "412 lines matched the connection pool exhausted signature",
            "peak": 412.0, "unit": "Count",
            "first_seen": (START + timedelta(minutes=5)).isoformat(),
            "tags": {"class": "connection_pool_exhausted"},
        },
        {
            "name": "log:timeout", "kind": "log", "source": "logs", "severity": "crit",
            "summary": "498 lines matched the timeout signature", "peak": 498.0, "unit": "Count",
            "first_seen": (START + timedelta(minutes=5)).isoformat(),
            "tags": {"class": "timeout"},
        },
        {
            "name": "ALB target health", "kind": "health", "source": "elbv2/customer-api-tg",
            "severity": "warn", "summary": "4/6 targets healthy — Target.Timeout",
            "baseline": 6.0, "peak": 4.0, "unit": "Count",
            "evidence": [
                "Target.Timeout: health check timed out — app is up but too slow to answer"
            ],
            "tags": {"class": "target_health"},
        },
    ],
    "events": [
        {
            "at": (START + timedelta(minutes=1)).isoformat(), "source": "cloudtrail/deploy",
            "title": "UpdateService by deploy-bot/github-actions",
            "detail": "production/customer-api", "severity": "warn",
        },
        {
            "at": (START + timedelta(minutes=1, seconds=12)).isoformat(), "source": "ecs",
            "title": "Deployment completed (customer-api:184)",
            "detail": "rollout=COMPLETED running=6 desired=6", "severity": "info",
        },
        {
            "at": (START + timedelta(minutes=5)).isoformat(), "source": "logs",
            "title": "First 'Error' log: <ts> ERROR HikariPool-<n> - Connection is not available",
            "detail": "412 occurrences in window", "severity": "crit",
        },
        {
            "at": (START + timedelta(minutes=9)).isoformat(), "source": "ecs",
            "title": "(service customer-api) (port 8080) is unhealthy in target-group "
                     "customer-api-tg due to (reason Request timed out)",
            "severity": "crit",
        },
        {
            "at": (START + timedelta(minutes=11)).isoformat(), "source": "cloudtrail/scaling",
            "title": "RegisterScalableTarget by application-autoscaling",
            "detail": "service/production/customer-api", "severity": "info",
        },
        {
            "at": (START + timedelta(minutes=18)).isoformat(), "source": "ecs",
            "title": "(service customer-api) has reached a steady state.", "severity": "ok",
        },
    ],
    "log_patterns": [
        {
            "pattern": "<ts> ERROR [http-nio-<n>-exec-<n>] c.e.c.r.CustomerRepository - "
                       "HikariPool-<n> - Connection is not available, request timed out "
                       "after <qty>",
            "count": 412, "level": "Error", "severity": "crit",
            "classes": ["connection_pool_exhausted", "timeout"],
            "first_seen": (START + timedelta(minutes=5)).isoformat(),
            "last_seen": (START + timedelta(minutes=20)).isoformat(),
            "sample": "2026-10-01T06:35:11.204Z ERROR [http-nio-8080-exec-7] "
                      "c.e.c.r.CustomerRepository - HikariPool-1 - Connection is not available, "
                      "request timed out after 30000ms",
            "streams": 6,
        },
        {
            "pattern": "<ts> ERROR o.s.w.s.m.s.DefaultHandlerExceptionResolver - "
                       "<n> returned for <path>",
            "count": 486, "level": "Error", "severity": "crit", "classes": [],
            "first_seen": (START + timedelta(minutes=5, seconds=30)).isoformat(),
            "last_seen": (START + timedelta(minutes=20)).isoformat(),
            "sample": "2026-10-01T06:35:41.880Z ERROR "
                      "o.s.w.s.m.s.DefaultHandlerExceptionResolver - 500 returned for "
                      "/v2/customers",
            "streams": 6,
        },
        {
            "pattern": "<ts> WARN c.z.h.p.PoolBase - HikariPool-<n> - Failed to validate "
                       "connection org.postgresql.jdbc.PgConnection@<hash> (This connection "
                       "has been closed.)",
            "count": 203, "level": "Warn", "severity": "warn",
            "classes": ["db_connection_refused"],
            "first_seen": (START + timedelta(minutes=6)).isoformat(),
            "last_seen": (START + timedelta(minutes=19)).isoformat(),
            "sample": "2026-10-01T06:36:02.117Z WARN c.z.h.p.PoolBase - HikariPool-1 - Failed to "
                      "validate connection",
            "streams": 5,
        },
        {
            "pattern": "<ts> WARN c.e.c.w.UpstreamClient - call to billing-service timed out after "
                       "<qty> (attempt <n>/<n>)",
            "count": 86, "level": "Warn", "severity": "warn", "classes": ["timeout"],
            "first_seen": (START + timedelta(minutes=7)).isoformat(),
            "last_seen": (START + timedelta(minutes=18)).isoformat(),
            "sample": "2026-10-01T06:37:44.009Z WARN c.e.c.w.UpstreamClient - call to "
                      "billing-service timed out after 2000ms (attempt 3/3)",
            "streams": 4,
        },
    ],
    "findings": [],
    "narrative": "",
    "cost": {"total_usd": 0.0, "avoided_usd": 0.0, "dry_run": False, "max_spend_usd": None,
             "by_operation": {}},
    "meta": {
        "demo": True,
        "log_groups": ["/aws/ecs/customer-api"],
        "log_events_scanned": 1284,
        "metrics_requested": 19,
        "rds_max_connections": 200,
        "rds_max_connections_source": "estimated from instance class",
        "stopped_tasks": [],
    },
}


def main() -> int:
    from sre_toolkit.models import Snapshot

    out = ROOT / "src" / "sre_toolkit" / "fixtures" / "demo_incident.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, indent=2) + "\n")
    # Round-trip it so a malformed fixture fails here rather than at runtime.
    Snapshot.from_dict(json.loads(out.read_text()))
    print(f"wrote {out.relative_to(ROOT)} ({out.stat().st_size // 1024} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
