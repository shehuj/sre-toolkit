"""`sre slo` and `sre dr` — reliability maths and disaster-recovery readiness.

SLO calculation is deliberately cheap: two metrics in one GetMetricData request
at a coarse period. A 30-day availability figure costs $0.00002, so this is safe
to run in CI on every deploy.
"""

from __future__ import annotations

from datetime import timedelta

from ..models import Severity, Window
from ._shared import emit, exit_code_for, print_cost


def register(sub) -> None:
    slo = sub.add_parser(
        "slo", help="availability, error budget and burn rate",
        description="Compute an availability SLO and error budget from ALB metrics.",
    )
    slo_inner = slo.add_subparsers(dest="action", metavar="<action>")
    calc = slo_inner.add_parser("calculate", aliases=["error-budget"],
                                help="availability and error budget over a period")
    calc.add_argument("--target-group", required=True,
                      help="ALB target group name or ARN serving the service")
    calc.add_argument("--objective", type=float, default=99.9,
                      help="availability objective in percent (default: 99.9)")
    calc.add_argument("--days", type=int, default=30, help="SLO window in days (default: 30)")
    calc.add_argument("--service", default="", help="label for the report")
    calc.set_defaults(handler=run_slo)

    dr = sub.add_parser(
        "dr", help="disaster-recovery and backup readiness (free Describe* calls)",
        description="Validate backup and recovery posture without changing anything.",
    )
    dr_inner = dr.add_subparsers(dest="action", metavar="<action>")
    validate = dr_inner.add_parser("validate", help="check backups, Multi-AZ, replicas, versioning")
    validate.add_argument("--db", action="append", dest="db_instances", default=[],
                          help="RDS instance identifier (repeatable)")
    validate.add_argument("--bucket", action="append", dest="buckets", default=[],
                          help="S3 bucket to check for versioning and replication (repeatable)")
    validate.add_argument("--max-snapshot-age-hours", type=int, default=26,
                          help="fail if the newest snapshot is older than this (default: 26)")
    validate.set_defaults(handler=run_dr)


def run_slo(ctx, args) -> int:
    from datetime import datetime, timezone

    from ..collectors import alb as alb_col
    from ..collectors import metrics as metric_col

    con = ctx.console
    end = datetime.now(timezone.utc)
    window = Window(end - timedelta(days=args.days), end)

    arns = alb_col.find_target_groups(ctx, args.target_group)
    if not arns:
        con.error(f"no target group found for {args.target_group}")
        return 3
    result = alb_col.collect(ctx, arns[0])
    dims = result.get("dimensions") or {}
    if not (dims.get("TargetGroup") and dims.get("LoadBalancer")):
        con.error("could not resolve CloudWatch dimensions for that target group")
        return 3

    ns = "AWS/ApplicationELB"
    specs = [
        metric_col.MetricSpec("total", ns, "RequestCount", dims, "Sum", "Count", "Requests"),
        metric_col.MetricSpec("errors", ns, "HTTPCode_Target_5XX_Count", dims, "Sum", "Count",
                              "Failed requests"),
        metric_col.MetricSpec("elb_errors", ns, "HTTPCode_ELB_5XX_Count", dims, "Sum", "Count",
                              "LB-generated 5xx"),
    ]
    series = metric_col.fetch(ctx, window, specs, include_baseline=False)
    totals = {s.spec.key: sum(s.values) for s in series}
    buckets = {s.spec.key: s for s in series}

    total = totals.get("total", 0.0)
    failed = totals.get("errors", 0.0) + totals.get("elb_errors", 0.0)
    if total <= 0:
        con.error("no request data in the window — check the target group and region")
        return 3

    availability = (1 - failed / total) * 100
    allowed_failures = total * (1 - args.objective / 100)
    budget_used = (failed / allowed_failures * 100) if allowed_failures else float("inf")
    budget_remaining = 100 - budget_used
    burn_rate = budget_used / 100 * (30 / args.days) if args.days else 0.0

    con.title("SLO / ERROR BUDGET", f"{args.service or args.target_group} · last {args.days} days")
    con.kv("Objective", f"{args.objective}% availability")
    con.kv("Achieved", f"{availability:.4f}%")
    con.kv("Requests", f"{total:,.0f}")
    con.kv("Failed", f"{failed:,.0f}")
    con.kv("Budget", f"{allowed_failures:,.0f} failed requests allowed")
    con.kv("Budget used", f"{budget_used:.1f}%")
    con.kv("Budget left", f"{max(0.0, budget_remaining):.1f}%")

    if burn_rate > 0:
        days_left = (budget_remaining / 100) * args.days / max(burn_rate, 1e-9)
        con.kv("Burn rate", f"{burn_rate:.2f}× budget per 30 days")
        if budget_remaining > 0:
            con.kv("Exhausted in", f"~{days_left:.1f} days at this rate")

    if availability < args.objective:
        severity = Severity.CRIT
        verdict = "objective missed — the budget is already spent"
    elif budget_used >= 75:
        severity = Severity.WARN
        verdict = "objective met but most of the budget is consumed"
    else:
        severity = Severity.OK
        verdict = "objective met with budget to spare"
    con.out()
    con.bullet(verdict, severity)

    daily = buckets.get("errors")
    if daily and daily.values:
        con.section("Failed requests over the window")
        con.out(con.sparkline(daily.values, width=40))

    emit(ctx, args, {
        "service": args.service or args.target_group,
        "objective_pct": args.objective, "window_days": args.days,
        "availability_pct": round(availability, 6), "requests": total, "failed": failed,
        "error_budget_requests": allowed_failures,
        "error_budget_used_pct": round(budget_used, 4),
        "error_budget_remaining_pct": round(max(0.0, budget_remaining), 4),
        "burn_rate_30d": round(burn_rate, 4), "verdict": verdict,
        "cost": ctx.ledger.to_dict(),
    })
    print_cost(ctx)
    return exit_code_for(severity)


def run_dr(ctx, args) -> int:
    from datetime import datetime, timezone

    from ..collectors import rds as rds_col

    con = ctx.console
    con.title("DR / BACKUP VALIDATION")
    if not (args.db_instances or args.buckets):
        con.error("pass at least one --db or --bucket to validate")
        return 2

    worst = Severity.OK
    report: dict = {"databases": [], "buckets": []}
    now = datetime.now(timezone.utc)

    for instance in args.db_instances:
        window = Window(now - timedelta(hours=1), now)
        result = rds_col.collect(ctx, instance, window)
        info = result.get("instance") or {}
        if not info:
            con.bullet(f"{instance}: not found", Severity.CRIT)
            worst = Severity.CRIT
            continue
        snaps = rds_col.snapshots(ctx, instance)
        newest = snaps[0] if snaps else None
        age_hours = None
        if newest and newest.get("created_at"):
            created = datetime.fromisoformat(newest["created_at"])
            age_hours = (now - created).total_seconds() / 3600

        checks = [
            ("automated backups enabled",
             bool(info.get("backup_retention_days")), Severity.CRIT,
             f"retention = {info.get('backup_retention_days')} days"),
            ("snapshot newer than threshold",
             age_hours is not None and age_hours <= args.max_snapshot_age_hours, Severity.CRIT,
             f"newest snapshot {age_hours:.1f}h old" if age_hours is not None
             else "no snapshots found"),
            ("multi-AZ", bool(info.get("multi_az")), Severity.WARN,
             "single-AZ instance" if not info.get("multi_az") else "multi-AZ"),
            ("deletion protection", bool(info.get("deletion_protection")), Severity.WARN,
             "disabled" if not info.get("deletion_protection") else "enabled"),
            ("read replica present", bool(info.get("read_replicas")), Severity.INFO,
             f"{len(info.get('read_replicas') or [])} replica(s)"),
        ]
        con.section(f"RDS {instance}")
        passed = []
        for name, ok, failure_severity, detail in checks:
            severity = Severity.OK if ok else failure_severity
            worst = max(worst, severity, key=lambda s: s.rank)
            con.bullet(f"{name}: {detail}", severity)
            passed.append({"check": name, "ok": ok, "detail": detail,
                           "severity": severity.value})
        report["databases"].append(
            {"instance": instance, "checks": passed, "snapshots": snaps[:3]}
        )

    for bucket in args.buckets:
        con.section(f"S3 {bucket}")
        versioning = ctx.aws.call(
            "s3", "get_bucket_versioning", "s3:GetBucketVersioning", detail=bucket, Bucket=bucket,
        )
        enabled = versioning.get("Status") == "Enabled"
        severity = Severity.OK if enabled else Severity.WARN
        worst = max(worst, severity, key=lambda s: s.rank)
        con.bullet(f"versioning: {versioning.get('Status', 'Disabled')}", severity)

        replication = {}
        try:
            replication = ctx.aws.call(
                "s3", "get_bucket_replication", "s3:GetBucketReplication",
                detail=bucket, Bucket=bucket,
            )
        except Exception:  # noqa: BLE001 - absent replication config is a normal 404
            pass
        rules = (replication.get("ReplicationConfiguration") or {}).get("Rules", [])
        con.bullet(
            f"cross-region replication: {len(rules)} rule(s)",
            Severity.OK if rules else Severity.INFO,
        )
        report["buckets"].append(
            {"bucket": bucket, "versioning": versioning.get("Status", "Disabled"),
             "replication_rules": len(rules)}
        )

    emit(ctx, args, report | {"cost": ctx.ledger.to_dict()})
    print_cost(ctx)
    return exit_code_for(worst)
