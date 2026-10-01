"""`sre cost` — what this toolkit charges you, before and after.

`cost estimate` is the honest answer to "can I afford to run this during an
incident": it plans the same investigation with --dry-run semantics and prices
every call it would make, without making any of them.
"""

from __future__ import annotations

from ..pricing import API_PRICES, BEDROCK_MODELS, DEFAULT_MODEL
from ..render import fmt_usd, fmt_usd_cell
from ._shared import add_target_args, add_window_args, emit, target_from_args


def register(sub) -> None:
    parser = sub.add_parser(
        "cost", help="price a run before you make it, and list the price table",
        description="Cost planning and transparency.",
    )
    inner = parser.add_subparsers(dest="action", metavar="<action>")

    est = inner.add_parser("estimate", help="price an investigation without calling AWS")
    add_target_args(est)
    add_window_args(est)
    est.add_argument("--log-limit", type=int, default=1000)
    est.set_defaults(handler=run_estimate)

    prices = inner.add_parser("prices", help="show the price table this toolkit meters against")
    prices.set_defaults(handler=run_prices)


def run_estimate(ctx, args) -> int:
    from ..investigate import investigate

    ctx.dry_run = True
    ctx.ledger.dry_run = True
    window = ctx.resolve_window(args.start, args.end, args.minutes)
    con = ctx.console
    con.title("COST ESTIMATE", f"{args.service} · {window}")

    snap = investigate(ctx, target_from_args(ctx, args), window, log_limit=args.log_limit)
    ledger = ctx.ledger.to_dict()

    rows = [
        [op, row["calls"], fmt_usd_cell(row["usd"] + row["avoided_usd"])]
        for op, row in sorted(
            ledger["by_operation"].items(),
            key=lambda kv: -(kv[1]["usd"] + kv[1]["avoided_usd"]),
        )
    ]
    con.table(["operation", "calls", "estimated"], rows)
    con.out()
    con.kv("Metrics planned", snap.meta.get("metrics_requested", 0))
    con.kv("Total estimate", fmt_usd(ledger["avoided_usd"] + ledger["total_usd"]))
    con.out(con.style(
        "Nothing was called. Unbilled operations are listed as 'free' so the whole plan is visible.",
        "dim",
    ))
    if ctx.use_ai:
        con.out(con.style(f"AI step would use {DEFAULT_MODEL if not ctx.model else ctx.model}.",
                          "dim"))

    emit(ctx, args, {"service": args.service, "window": str(window), "estimate": ledger,
                     "planned_collectors": snap.collectors_run})
    return 0


def run_prices(ctx, args) -> int:
    con = ctx.console
    con.title("PRICE TABLE", "us-east-1 list prices; override in ~/.config/sre-toolkit/pricing.json")

    con.section("AWS APIs")
    con.table(
        ["operation", "unit", "usd"],
        [
            [op, price["unit"], f"${price['usd']:.4f}" if price["usd"] else "free"]
            for op, price in sorted(API_PRICES.items(), key=lambda kv: (-kv[1]["usd"], kv[0]))
        ],
    )

    con.section("Bedrock models (per 1M tokens)")
    con.table(
        ["model", "input", "output", "context", ""],
        [
            [model, f"${price['input']:.2f}", f"${price['output']:.2f}",
             f"{price['context']:,}", "default" if model == DEFAULT_MODEL else ""]
            for model, price in BEDROCK_MODELS.items()
        ],
    )

    con.section("What a typical 30-minute investigation costs")
    con.table(
        ["step", "cost"],
        [
            ["ECS / RDS / ELB / CloudTrail describes", "$0.0000 (not billed)"],
            ["Log collection via FilterLogEvents", "$0.0000 (not billed)"],
            ["One batched GetMetricData (19 metrics)", "$0.0002"],
            ["Deterministic correlation + narrative", "$0.0000"],
            [f"Optional --ai narrative ({DEFAULT_MODEL})", "~$0.0030"],
            ["Optional --deep Logs Insights (1 GB scanned)", "$0.0050"],
        ],
    )
    emit(ctx, args, {"api": API_PRICES, "bedrock": BEDROCK_MODELS, "default_model": DEFAULT_MODEL})
    return 0
