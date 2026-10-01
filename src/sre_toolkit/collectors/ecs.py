"""ECS collector. Every API used here is free — Describe* calls are not billed.

That is deliberate: the toolkit answers "what changed and what is unhealthy"
from free control-plane data first, and only reaches for billed metric/log data
to quantify the impact.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..models import Resource, Severity, Signal, SignalKind, TimelineEvent, Window

UTC = timezone.utc

# ECS service events worth putting on an incident timeline, with a severity.
_EVENT_RULES: tuple[tuple[str, Severity], ...] = (
    ("has begun draining connections", Severity.INFO),
    ("has started", Severity.INFO),
    ("has stopped", Severity.WARN),
    ("deployment completed", Severity.INFO),
    ("deployment failed", Severity.CRIT),
    ("rolling back", Severity.CRIT),
    ("unable to place a task", Severity.CRIT),
    ("unable to consistently start tasks", Severity.CRIT),
    ("was unable to place a task because no container instance met", Severity.CRIT),
    ("has reached a steady state", Severity.OK),
    ("is unhealthy", Severity.CRIT),
    ("failed container health checks", Severity.CRIT),
    ("registered", Severity.INFO),
    ("deregistered", Severity.INFO),
)


def describe_service(ctx, cluster: str, service: str) -> dict[str, Any]:
    payload = ctx.aws.call(
        "ecs", "describe_services", "ecs:DescribeServices",
        detail=f"{cluster}/{service}", cluster=cluster, services=[service],
    )
    services = payload.get("services", [])
    if not services:
        return {}
    return services[0]


def collect(ctx, cluster: str, service: str, window: Window) -> dict[str, Any]:
    """Returns {'service':…, 'resources':[…], 'signals':[…], 'events':[…], 'target_groups':[…]}"""
    svc = describe_service(ctx, cluster, service)
    out: dict[str, Any] = {
        "service": svc, "resources": [], "signals": [], "events": [], "target_groups": [],
    }
    if not svc:
        return out

    desired = svc.get("desiredCount", 0)
    running = svc.get("runningCount", 0)
    pending = svc.get("pendingCount", 0)
    task_def = (svc.get("taskDefinition") or "").split("/")[-1]

    out["resources"].append(
        Resource(
            kind="ecs:service",
            identifier=f"{cluster}/{service}",
            attributes={
                "status": svc.get("status"),
                "launch_type": svc.get("launchType") or "capacity-provider",
                "desired": desired, "running": running, "pending": pending,
                "task_definition": task_def,
                "platform_version": svc.get("platformVersion"),
            },
        )
    )

    if desired:
        severity = Severity.OK
        if running < desired:
            severity = Severity.CRIT if running < desired * 0.7 else Severity.WARN
        out["signals"].append(
            Signal(
                name="ECS task capacity",
                kind=SignalKind.RESOURCE,
                source=f"ecs/{service}",
                severity=severity,
                summary=f"{running}/{desired} tasks running ({pending} pending)",
                baseline=float(desired),
                peak=float(running),
                unit="Count",
            )
        )

    # Deployments: the single most valuable free signal during an incident.
    for deployment in svc.get("deployments", []):
        created = _as_dt(deployment.get("createdAt"))
        updated = _as_dt(deployment.get("updatedAt"))
        rollout = deployment.get("rolloutState", "")
        if created and created <= window.end and (updated or created) >= window.start - _hour():
            severity = {
                "FAILED": Severity.CRIT, "IN_PROGRESS": Severity.WARN,
            }.get(rollout, Severity.INFO)
            out["events"].append(
                TimelineEvent(
                    at=created,
                    source="ecs",
                    title=f"Deployment {deployment.get('status', '').lower()} "
                          f"({(deployment.get('taskDefinition') or '').split('/')[-1]})",
                    detail=f"rollout={rollout or 'n/a'} "
                           f"running={deployment.get('runningCount')} "
                           f"desired={deployment.get('desiredCount')}",
                    severity=severity,
                )
            )
            if rollout == "COMPLETED" and updated and updated >= window.start - _hour():
                out["events"].append(
                    TimelineEvent(
                        at=updated, source="ecs", title="Deployment completed",
                        detail=(deployment.get("rolloutStateReason") or "")[:160],
                        severity=Severity.INFO,
                    )
                )

    for raw in svc.get("events", [])[:40]:
        at = _as_dt(raw.get("createdAt"))
        message = raw.get("message", "")
        if not at or at < window.start - _hour() or at > window.end:
            continue
        out["events"].append(
            TimelineEvent(at=at, source="ecs", title=_trim(message), severity=_severity(message))
        )

    for lb in svc.get("loadBalancers", []):
        arn = lb.get("targetGroupArn")
        if arn:
            out["target_groups"].append(arn)

    return out


def stopped_tasks(ctx, cluster: str, service: str, limit: int = 20) -> list[dict[str, Any]]:
    """Why did tasks die? ListTasks + DescribeTasks, both free."""
    arns = ctx.aws.call(
        "ecs", "list_tasks", "ecs:ListTasks", detail=f"{cluster}/{service} stopped",
        cluster=cluster, serviceName=service, desiredStatus="STOPPED", maxResults=limit,
    ).get("taskArns", [])
    if not arns:
        return []
    tasks = ctx.aws.call(
        "ecs", "describe_tasks", "ecs:DescribeTasks", detail=f"{len(arns)} stopped tasks",
        cluster=cluster, tasks=arns[:limit],
    ).get("tasks", [])
    out = []
    for task in tasks:
        containers = task.get("containers", [])
        out.append(
            {
                "task": (task.get("taskArn") or "").split("/")[-1],
                "stopped_at": _iso(task.get("stoppedAt")),
                "stop_code": task.get("stopCode"),
                "reason": task.get("stoppedReason", ""),
                "exit_codes": {
                    c.get("name"): c.get("exitCode") for c in containers if "exitCode" in c
                },
                "task_definition": (task.get("taskDefinitionArn") or "").split("/")[-1],
            }
        )
    return out


def stopped_task_signals(stopped: list[dict[str, Any]], window: Window) -> list[Signal]:
    in_window = [
        t for t in stopped
        if t.get("stopped_at") and window.start <= _parse(t["stopped_at"]) <= window.end
    ]
    if not in_window:
        return []
    oom = [t for t in in_window if "OutOfMemory" in (t.get("reason") or "")
           or 137 in t.get("exit_codes", {}).values()]
    severity = Severity.CRIT if oom or len(in_window) >= 3 else Severity.WARN
    reason = oom[0]["reason"] if oom else in_window[0].get("reason", "")
    return [
        Signal(
            name="ECS task terminations",
            kind=SignalKind.RESOURCE,
            source="ecs",
            severity=severity,
            summary=f"{len(in_window)} task(s) stopped in window"
                    + (f"; {len(oom)} OOM-killed" if oom else ""),
            peak=float(len(in_window)), unit="Count",
            first_seen=_parse(in_window[0]["stopped_at"]),
            evidence=[f"{t['task']}: {t.get('reason', '')[:120]}" for t in in_window[:3]],
            tags={"class": "oom"} if oom else {},
        )
    ]


def task_events(stopped: list[dict[str, Any]], window: Window) -> list[TimelineEvent]:
    out = []
    for task in stopped:
        at = _parse(task.get("stopped_at"))
        if not at or not (window.start <= at <= window.end):
            continue
        out.append(
            TimelineEvent(
                at=at, source="ecs",
                title=f"Task {task['task'][:12]} stopped ({task.get('stop_code') or 'unknown'})",
                detail=(task.get("reason") or "")[:160],
                severity=Severity.CRIT if task.get("stop_code") == "TaskFailedToStart"
                else Severity.WARN,
            )
        )
    return out


def _severity(message: str) -> Severity:
    lowered = message.lower()
    for needle, severity in _EVENT_RULES:
        if needle in lowered:
            return severity
    return Severity.INFO


def _trim(message: str, width: int = 110) -> str:
    text = message.split(") ", 1)[-1] if message.startswith("(service") else message
    return text if len(text) <= width else text[: width - 1] + "…"


def _as_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC)
    if isinstance(value, str):
        return _parse(value)
    return None


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(value: Any) -> str | None:
    dt = _as_dt(value)
    return dt.isoformat() if dt else None


def _hour():
    from datetime import timedelta

    return timedelta(hours=1)
