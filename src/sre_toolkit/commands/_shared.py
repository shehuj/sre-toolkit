"""Helpers shared by command modules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models import Severity, Snapshot
from ..render import fmt_usd


def add_window_args(parser) -> None:
    parser.add_argument("--start", help="window start (ISO-8601 UTC, epoch, or '-45m')")
    parser.add_argument("--end", help="window end (default: now)")
    parser.add_argument(
        "--minutes", type=int, default=30,
        help="window length when --start is omitted (default: 30)",
    )


def add_target_args(parser) -> None:
    # Not `required=True`: --demo supplies its own service, and argparse would
    # otherwise reject `sre --demo incident investigate`.
    parser.add_argument("--service", help="service name (used for log discovery)")
    parser.add_argument("--cluster", help="ECS cluster name")
    parser.add_argument("--ecs-service", help="ECS service name if it differs from --service")
    parser.add_argument("--db", dest="db_instance", help="RDS DB instance identifier")
    parser.add_argument("--target-group", help="ALB target group name or ARN")
    parser.add_argument("--log-group", action="append", dest="log_groups", default=[],
                        help="CloudWatch log group (repeatable; default: discovered)")
    parser.add_argument("--max-connections", type=int,
                        help="DB max_connections, if you know it (otherwise estimated)")


def target_from_args(ctx, args):
    from ..errors import SreToolkitError
    from ..investigate import Target

    if not args.service:
        raise SreToolkitError(
            "--service is required (or use --demo to run against the bundled snapshot)"
        )
    target = Target(
        service=args.service,
        cluster=args.cluster,
        ecs_service=args.ecs_service,
        db_instance=args.db_instance,
        target_group=args.target_group,
        log_groups=list(args.log_groups or []),
        max_connections=args.max_connections,
    )
    if target.target_group and not target.target_group.startswith("arn:"):
        from ..collectors import alb as alb_col

        arns = alb_col.find_target_groups(ctx, target.target_group)
        target.target_group = arns[0] if arns else target.target_group
    return target


def emit(ctx, args, payload: dict[str, Any]) -> None:
    """Honour --json and -o/--output for any command's structured result."""
    if getattr(args, "output", None):
        path = Path(args.output)
        path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
        if not ctx.json_mode:
            ctx.console.out(f"\nwrote {path} ({path.stat().st_size // 1024 or 1} KiB)")
    if ctx.json_mode:
        ctx.console.json(payload)


def load_snapshot(path: str) -> Snapshot:
    raw = json.loads(Path(path).read_text())
    return Snapshot.from_dict(raw)


def demo_snapshot() -> Snapshot:
    from importlib import resources

    data = (
        resources.files("sre_toolkit.fixtures").joinpath("demo_incident.json").read_text()
    )
    return Snapshot.from_dict(json.loads(data))


def exit_code_for(worst: Severity) -> int:
    """0 ok, 1 warn, 2 crit — so these commands compose in shell pipelines."""
    return {Severity.OK: 0, Severity.INFO: 0, Severity.WARN: 1, Severity.CRIT: 2}[worst]


def print_cost(ctx) -> None:
    cost = ctx.ledger.to_dict()
    line = f"cost: {fmt_usd(cost['total_usd'])}"
    if cost["avoided_usd"]:
        line += f" (cache avoided {fmt_usd(cost['avoided_usd'])})"
    if ctx.dry_run:
        line += " — dry run, nothing was called"
    ctx.console.out()
    ctx.console.out(ctx.console.style(line, "dim"))
