"""`sre aws …` — per-service diagnosis built on free Describe* calls plus one
batched metrics request."""

from __future__ import annotations

from ..models import Severity
from ..render import fmt_delta, fmt_num
from ._shared import (
    add_target_args,
    add_window_args,
    emit,
    exit_code_for,
    print_cost,
    target_from_args,
)


def register(sub) -> None:
    parser = sub.add_parser(
        "aws", help="diagnose ECS, RDS and ALB components",
        description="Read-only AWS diagnosis. Describe* calls are free; metrics are batched.",
    )
    inner = parser.add_subparsers(dest="action", metavar="<action>")

    ecs = inner.add_parser("ecs", help="ECS service state, deployments and task failures")
    ecs.add_argument("--cluster", required=True)
    ecs.add_argument("--service", required=True)
    add_window_args(ecs)
    ecs.add_argument("--no-metrics", action="store_true",
                     help="skip the billed CloudWatch request entirely")
    ecs.set_defaults(handler=run_ecs)

    rds = inner.add_parser("rds", help="RDS instance state, connections and events")
    rds.add_argument("--instance", required=True)
    rds.add_argument("--max-connections", type=int)
    add_window_args(rds)
    rds.add_argument("--no-metrics", action="store_true")
    rds.set_defaults(handler=run_rds)

    alb = inner.add_parser("alb", help="ALB target health and error rates")
    alb.add_argument("--name", required=True, help="load balancer name, or a target group ARN")
    add_window_args(alb)
    alb.add_argument("--no-metrics", action="store_true")
    alb.set_defaults(handler=run_alb)

    diag = inner.add_parser(
        "diagnose", help="whole-service diagnosis (ECS + ALB + RDS + changes + logs)"
    )
    add_target_args(diag)
    add_window_args(diag)
    diag.add_argument("--log-limit", type=int, default=500)
    diag.set_defaults(handler=run_diagnose)


def _signal_table(ctx, signals) -> None:
    rows = [
        [
            {"ok": "✓", "info": "•", "warn": "▲", "crit": "✗"}[s.severity.value],
            s.name,
            fmt_num(s.baseline, s.unit),
            fmt_num(s.peak, s.unit),
            fmt_delta(s),
            s.summary,
        ]
        for s in sorted(signals, key=lambda s: -s.severity.rank)
    ]
    ctx.console.table(["", "signal", "baseline", "peak", "change", "note"], rows)


def run_ecs(ctx, args) -> int:
    from ..collectors import ecs as ecs_col
    from ..collectors import metrics as metric_col

    window = ctx.resolve_window(args.start, args.end, args.minutes)
    con = ctx.console
    result = ecs_col.collect(ctx, args.cluster, args.service, window)
    if not result.get("service") and not ctx.dry_run:
        con.error(f"ECS service {args.cluster}/{args.service} not found")
        return 3

    # Under --dry-run nothing was called, so nothing can be "found" — carry on and
    # price the plan instead of reporting a missing target.
    svc = result.get("service") or {}
    con.title("ECS SERVICE", f"{args.cluster}/{args.service} · {window}")
    con.kv("Status", svc.get("status"))
    con.kv("Tasks", f"{svc.get('runningCount')}/{svc.get('desiredCount')} running "
                    f"({svc.get('pendingCount')} pending)")
    con.kv("Task definition", (svc.get("taskDefinition") or "").split("/")[-1])
    con.kv("Launch type", svc.get("launchType") or "capacity provider")

    signals = list(result["signals"])
    stopped = ecs_col.stopped_tasks(ctx, args.cluster, args.service)
    signals += ecs_col.stopped_task_signals(stopped, window)

    if not args.no_metrics:
        series = metric_col.fetch(ctx, window, metric_col.ecs_specs(args.cluster, args.service))
        signals += metric_col.to_signals(series, window, "cloudwatch")

    con.section("Signals")
    _signal_table(ctx, signals)

    events = result["events"] + ecs_col.task_events(stopped, window)
    if events:
        con.section("Service events")
        for event in sorted(events, key=lambda e: e.at)[-12:]:
            con.bullet(f"{event.at.strftime('%H:%M:%S')}  {event.title}", event.severity)

    if stopped:
        con.section("Recently stopped tasks")
        con.table(
            ["task", "stopped", "code", "reason"],
            [[t["task"][:12], (t.get("stopped_at") or "")[11:19], t.get("stop_code"),
              (t.get("reason") or "")[:60]] for t in stopped[:8]],
        )

    worst = max((s.severity for s in signals), key=lambda s: s.rank, default=Severity.OK)
    from ..models import _encode

    emit(ctx, args, {
        "cluster": args.cluster, "service": args.service,
        "signals": _encode(signals),
        "stopped_tasks": stopped, "cost": ctx.ledger.to_dict(),
    })
    print_cost(ctx)
    return exit_code_for(worst)


def run_rds(ctx, args) -> int:
    from ..collectors import metrics as metric_col
    from ..collectors import rds as rds_col

    window = ctx.resolve_window(args.start, args.end, args.minutes)
    con = ctx.console
    result = rds_col.collect(ctx, args.instance, window, args.max_connections)
    if not result.get("instance") and not ctx.dry_run:
        con.error(f"RDS instance {args.instance} not found")
        return 3

    info = result.get("instance") or {}
    con.title("RDS INSTANCE", f"{args.instance} · {window}")
    for key in ("status", "class", "engine", "multi_az", "storage_gb", "backup_retention_days"):
        con.kv(key.replace("_", " ").title(), info.get(key))
    con.kv("Max connections", f"{result.get('max_connections')} "
                              f"({result.get('max_connections_source')})")

    signals = list(result["signals"])
    if not args.no_metrics:
        series = metric_col.fetch(ctx, window, metric_col.rds_specs(args.instance))
        signals += metric_col.to_signals(series, window, "cloudwatch")
        conns = next((s for s in signals if s.name == "DB connections"), None)
        pressure = rds_col.connection_pressure(conns, result.get("max_connections"))
        if pressure:
            signals.append(pressure)

    con.section("Signals")
    _signal_table(ctx, signals)

    if result["events"]:
        con.section("RDS events")
        for event in result["events"][:10]:
            con.bullet(f"{event.at.strftime('%H:%M:%S')}  {event.title}", event.severity)

    worst = max((s.severity for s in signals), key=lambda s: s.rank, default=Severity.OK)
    emit(ctx, args, {"instance": info, "max_connections": result.get("max_connections"),
                     "cost": ctx.ledger.to_dict()})
    print_cost(ctx)
    return exit_code_for(worst)


def run_alb(ctx, args) -> int:
    from ..collectors import alb as alb_col
    from ..collectors import metrics as metric_col

    window = ctx.resolve_window(args.start, args.end, args.minutes)
    con = ctx.console
    arns = alb_col.find_target_groups(ctx, args.name)
    if not arns:
        if not ctx.dry_run:
            con.error(f"no target groups found for {args.name}")
            return 3
        arns = [args.name]  # price the plan against the name we were given

    con.title("LOAD BALANCER", f"{args.name} · {window}")
    worst = Severity.OK
    payload = []
    for arn in arns[:5]:
        result = alb_col.collect(ctx, arn)
        group = result.get("target_group") or {}
        if ctx.dry_run and not result.get("dimensions"):
            # Dimensions come from a call we did not make; use placeholders so the
            # metrics request still appears in the plan.
            result["dimensions"] = {"TargetGroup": arn, "LoadBalancer": "unresolved"}
        con.section(f"Target group {group.get('TargetGroupName', arn.split('/')[-2])}")
        signals = list(result["signals"])
        dims = result.get("dimensions") or {}
        if not args.no_metrics and dims.get("TargetGroup") and dims.get("LoadBalancer"):
            series = metric_col.fetch(
                ctx, window, metric_col.alb_specs(dims["TargetGroup"], dims["LoadBalancer"])
            )
            signals += metric_col.to_signals(series, window, "cloudwatch")
        _signal_table(ctx, signals)
        for signal in signals:
            for hint in signal.evidence:
                con.out(con.style(f"  ↳ {hint}", "dim"))
            worst = max(worst, signal.severity, key=lambda s: s.rank)
        payload.append({"target_group": group.get("TargetGroupName"),
                        "healthy": group.get("target_states", {})})

    emit(ctx, args, {"load_balancer": args.name, "target_groups": payload,
                     "cost": ctx.ledger.to_dict()})
    print_cost(ctx)
    return exit_code_for(worst)


def run_diagnose(ctx, args) -> int:
    """Same pipeline as `incident investigate`, framed as a component diagnosis."""
    from ..investigate import investigate
    from ..render import render_snapshot

    window = ctx.resolve_window(args.start, args.end, args.minutes)
    snap = investigate(ctx, target_from_args(ctx, args), window, log_limit=args.log_limit)
    render_snapshot(ctx.console, snap)
    emit(ctx, args, snap.to_dict())
    return exit_code_for(snap.worst_severity())
