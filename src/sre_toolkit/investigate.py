"""The incident pipeline: collect → correlate → explain.

Ordering here is a cost decision, not an aesthetic one:

  1. Free control-plane data first (ECS/RDS/ELB describes, CloudTrail, log
     filters). Often this alone identifies the change that caused the incident.
  2. Exactly one billed CloudWatch GetMetricData request for *every* metric from
     *every* component, because that API is billed per metric, not per call.
  3. Local pattern extraction — unlimited analysis of already-paid-for data.
  4. Deterministic correlation — free.
  5. AI narrative — only with --ai, on a compacted context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .aws.client import safe
from .collectors import alb as alb_col
from .collectors import changes as change_col
from .collectors import ecs as ecs_col
from .collectors import logs as log_col
from .collectors import metrics as metric_col
from .collectors import rds as rds_col
from .correlate import correlate
from .models import Snapshot, Window


@dataclass
class Target:
    """What to look at. Anything left as None is simply not collected."""

    service: str
    cluster: str | None = None
    ecs_service: str | None = None
    db_instance: str | None = None
    target_group: str | None = None
    load_balancer: str | None = None
    log_groups: list[str] = field(default_factory=list)
    max_connections: int | None = None

    def resolved_ecs(self) -> tuple[str, str] | None:
        if self.cluster and (self.ecs_service or self.service):
            return self.cluster, (self.ecs_service or self.service)
        return None


def snapshot(ctx, target: Target, window: Window, log_limit: int = 1000) -> Snapshot:
    snap = Snapshot(service=target.service, window=window, region=_region(ctx))
    specs: list[metric_col.MetricSpec] = []

    # --- step 1: free control-plane data --------------------------------
    ecs_pair = target.resolved_ecs()
    if ecs_pair:
        cluster, service_name = ecs_pair
        result = safe(ctx, "ecs", lambda: ecs_col.collect(ctx, cluster, service_name, window), {})
        if result:
            snap.collectors_run.append("ecs")
            _merge(snap, result)
            specs += metric_col.ecs_specs(cluster, service_name)
            stopped = safe(ctx, "ecs:tasks",
                           lambda: ecs_col.stopped_tasks(ctx, cluster, service_name), []) or []
            snap.signals.extend(ecs_col.stopped_task_signals(stopped, window))
            snap.events.extend(ecs_col.task_events(stopped, window))
            if stopped:
                snap.meta["stopped_tasks"] = stopped[:5]
            if not target.target_group and result.get("target_groups"):
                target.target_group = result["target_groups"][0]
        else:
            snap.collector_errors["ecs"] = f"no ECS service {cluster}/{service_name}"

    if target.target_group:
        result = safe(ctx, "alb", lambda: alb_col.collect(ctx, target.target_group), {})
        if result and result.get("target_group"):
            snap.collectors_run.append("alb")
            _merge(snap, result)
            dims = result.get("dimensions") or {}
            if dims.get("TargetGroup") and dims.get("LoadBalancer"):
                specs += metric_col.alb_specs(dims["TargetGroup"], dims["LoadBalancer"])
        else:
            snap.collector_errors["alb"] = "target group not found or not readable"

    if target.db_instance:
        result = safe(
            ctx, "rds",
            lambda: rds_col.collect(ctx, target.db_instance, window, target.max_connections), {},
        )
        if result and result.get("instance"):
            snap.collectors_run.append("rds")
            _merge(snap, result)
            specs += metric_col.rds_specs(target.db_instance)
            snap.meta["rds_max_connections"] = result.get("max_connections")
            snap.meta["rds_max_connections_source"] = result.get("max_connections_source")
        else:
            snap.collector_errors["rds"] = f"instance {target.db_instance} not found"

    changes = safe(ctx, "cloudtrail", lambda: change_col.collect(ctx, window), []) or []
    if changes:
        snap.collectors_run.append("cloudtrail")
        snap.events.extend(changes)

    # --- step 2: logs (FilterLogEvents — no per-GB charge) --------------
    groups = target.log_groups or safe(
        ctx, "logs:discover", lambda: log_col.guess_log_groups(ctx, target.service), []
    ) or []
    events: list[dict[str, Any]] = []
    for group in groups[:2]:  # two groups is plenty; more is rarely new information
        batch = safe(ctx, f"logs:{group}",
                     lambda g=group: log_col.collect(ctx, g, window, limit=log_limit), []) or []
        events.extend(batch)
    if groups:
        snap.collectors_run.append("logs")
        snap.meta["log_groups"] = groups[:2]
    if events:
        snap.log_patterns = log_col.patterns(events)
        snap.signals.extend(log_col.to_signals(events, window, "logs"))
        snap.events.extend(log_col.to_events(snap.log_patterns))
        snap.meta["log_events_scanned"] = len(events)

    if ctx.dry_run:
        # Nothing can be discovered without calling anything, so price the
        # components that were named explicitly. Otherwise the estimate reads as
        # cheaper than the run it is estimating.
        if target.db_instance and not any(s.namespace == "AWS/RDS" for s in specs):
            specs += metric_col.rds_specs(target.db_instance)
        if target.target_group and not any(
            s.namespace == "AWS/ApplicationELB" for s in specs
        ):
            specs += metric_col.alb_specs(target.target_group, "unresolved")

    # --- step 3: exactly one billed metrics request ---------------------
    if specs:
        series = safe(ctx, "cloudwatch", lambda: metric_col.fetch(ctx, window, specs), []) or []
        if series:
            snap.collectors_run.append("cloudwatch")
            snap.signals.extend(metric_col.to_signals(series, window, "cloudwatch"))
            snap.meta["metrics_requested"] = len(specs)

        # Derived, free: connections as a percentage of the instance ceiling.
        pressure = rds_col.connection_pressure(
            snap.signal("DB connections"), snap.meta.get("rds_max_connections")
        )
        if pressure:
            snap.signals.append(pressure)

    return snap


def investigate(
    ctx, target: Target, window: Window, log_limit: int = 1000, max_findings: int = 5
) -> Snapshot:
    snap = snapshot(ctx, target, window, log_limit=log_limit)
    return analyse(ctx, snap, max_findings=max_findings)


def analyse(ctx, snap: Snapshot, max_findings: int = 5) -> Snapshot:
    """Correlate and narrate an existing snapshot. Free unless --ai is set."""
    from .ai import offline

    snap.findings = correlate(snap, max_findings=max_findings)
    snap.narrative = offline.narrate(snap)

    if ctx.use_ai:
        from .ai import analyzer

        result = analyzer.analyse(ctx, snap, model_id=ctx.model)
        if result.get("narrative"):
            snap.narrative = result["narrative"]
            snap.meta["narrative_source"] = f"bedrock:{ctx.model or 'default'}"
            snap.meta["deterministic_narrative"] = offline.narrate(snap)
        for finding in result.get("findings", []):
            snap.findings.append(finding)
        snap.findings.sort(key=lambda f: -f.confidence)
        snap.findings = snap.findings[:max_findings]
        snap.meta["ai_usage"] = result.get("usage", {})
    else:
        snap.meta["narrative_source"] = "deterministic (no model called)"

    snap.cost = ctx.finish()
    return snap


def _merge(snap: Snapshot, result: dict[str, Any]) -> None:
    snap.resources.extend(result.get("resources", []))
    snap.signals.extend(result.get("signals", []))
    snap.events.extend(result.get("events", []))


def _region(ctx) -> str:
    try:
        return ctx.aws.region
    except Exception:  # noqa: BLE001 - region is cosmetic without credentials
        return ctx.region or ""
