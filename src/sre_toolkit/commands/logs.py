"""`sre logs investigate` — find the shape of the errors without paying to read
every line.

Default path uses FilterLogEvents (no per-GB charge) and groups results locally.
`--deep` adds a Logs Insights query, which *is* billed per GB scanned — the
command prints the estimate before running it and the actual scan after.
"""

from __future__ import annotations

from ..models import Severity
from ._shared import add_window_args, emit, exit_code_for, print_cost

# One Insights query that answers "what is erroring and how often" in a single
# scan. Insights is billed per GB scanned, so we ask once and ask well.
DEEP_QUERY = (
    "fields @timestamp, @message"
    " | filter @message like /(?i)(error|exception|fatal|timeout|refused|denied)/"
    " | stats count(*) as hits by bin(1m) as minute"
    " | sort minute asc"
)


def register(sub) -> None:
    parser = sub.add_parser(
        "logs", help="investigate log patterns for a service",
        description="Pull and group logs for an incident window.",
    )
    inner = parser.add_subparsers(dest="action", metavar="<action>")

    inv = inner.add_parser("investigate", help="group errors into patterns for a window")
    inv.add_argument("--service", required=True)
    inv.add_argument("--group", action="append", dest="log_groups", default=[],
                     help="log group (repeatable; default: discovered from --service)")
    inv.add_argument("--filter", dest="filter_pattern",
                     help="CloudWatch filter pattern (default: error/warn terms)")
    inv.add_argument("--limit", type=int, default=1000, help="max events per group")
    inv.add_argument("--top", type=int, default=10, help="patterns to show")
    add_window_args(inv)
    inv.set_defaults(handler=run_investigate)

    groups = inner.add_parser("groups", help="list candidate log groups for a service (free)")
    groups.add_argument("--service", required=True)
    groups.set_defaults(handler=run_groups)


def run_investigate(ctx, args) -> int:
    from ..collectors import logs as log_col

    window = ctx.resolve_window(args.start, args.end, args.minutes)
    con = ctx.console
    groups = args.log_groups or log_col.guess_log_groups(ctx, args.service)
    if not groups:
        con.error(
            f"no log groups found for '{args.service}'. Pass --group explicitly, or run "
            f"`sre logs groups --service {args.service}` to see what exists."
        )
        return 3

    con.title("LOG INVESTIGATION", f"{args.service} · {window}")
    con.kv("Log groups", ", ".join(groups[:3]))

    events = []
    for group in groups[:3]:
        events.extend(
            log_col.collect(ctx, group, window,
                            filter_pattern=args.filter_pattern or log_col.DEFAULT_FILTER,
                            limit=args.limit)
        )
    con.kv("Events scanned", f"{len(events)} (FilterLogEvents — no per-GB charge)")

    patterns = log_col.patterns(events, top=args.top)
    con.section("Patterns")
    con.table(
        ["count", "level", "classes", "first", "pattern"],
        [
            [p["count"], p["level"], ",".join(p["classes"]) or "-", p["first_seen"][11:19],
             p["pattern"][:70]]
            for p in patterns
        ],
    )

    signals = log_col.to_signals(events, window, "logs")
    if signals:
        con.section("Signals")
        for signal in sorted(signals, key=lambda s: -s.severity.rank):
            con.bullet(f"{signal.name}: {signal.summary}", signal.severity)

    deep = None
    if ctx.deep:
        con.section("Logs Insights (billed per GB scanned)")
        estimate = log_col.estimate_insights_gb(ctx, groups[0], window)
        con.kv("Estimated scan", f"{estimate:.3f} GB ≈ ${estimate * 0.005:.4f}")
        deep = log_col.insights(ctx, groups[0], window, DEEP_QUERY)
        con.kv("Actual scan", f"{deep.get('gb_scanned', 0):.3f} GB")
        rows = [[r.get("minute", "")[11:16], r.get("hits")] for r in deep.get("results", [])[:20]]
        con.table(["minute", "hits"], rows)
    elif events:
        con.out()
        con.out(con.style("add --deep for a Logs Insights aggregation (billed per GB scanned)",
                          "dim"))

    worst = max((s.severity for s in signals), key=lambda s: s.rank, default=Severity.OK)
    emit(ctx, args, {"service": args.service, "log_groups": groups[:3],
                     "events_scanned": len(events), "patterns": patterns,
                     "insights": deep, "cost": ctx.ledger.to_dict()})
    print_cost(ctx)
    return exit_code_for(worst)


def run_groups(ctx, args) -> int:
    from ..collectors import logs as log_col

    groups = log_col.guess_log_groups(ctx, args.service, limit=20)
    ctx.console.title("LOG GROUPS", args.service)
    for group in groups:
        ctx.console.bullet(group)
    if not groups:
        ctx.console.out(ctx.console.style("  (none matched)", "dim"))
    emit(ctx, args, {"service": args.service, "log_groups": groups})
    print_cost(ctx)
    return 0
