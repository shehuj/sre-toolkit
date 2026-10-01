"""`sre cert check` — certificate expiry across any number of domains, for free.

Worth having as its own command because expiry is the one outage you can see
coming: run it on a schedule and it costs nothing but a TLS handshake.
"""

from __future__ import annotations

from ..models import Severity
from ._shared import emit, exit_code_for, print_cost


def register(sub) -> None:
    parser = sub.add_parser(
        "cert", help="certificate expiry and chain checks (free)",
        description="Check TLS certificates on one or more domains.",
    )
    inner = parser.add_subparsers(dest="action", metavar="<action>")

    check = inner.add_parser("check", help="check certificate validity and expiry")
    check.add_argument("--domain", action="append", required=True, dest="domains",
                       help="domain to check (repeatable)")
    check.add_argument("--port", type=int, default=443)
    check.add_argument("--warn-days", type=int, default=30,
                       help="warn at N days remaining (default: 30)")
    check.add_argument("--crit-days", type=int, default=7,
                       help="fail at N days remaining (default: 7)")
    check.add_argument("--timeout", type=float, default=5.0)
    check.set_defaults(handler=run_check)


def run_check(ctx, args) -> int:
    from ..collectors import network

    con = ctx.console
    con.title("CERTIFICATE CHECK")
    rows, results = [], []
    worst = Severity.OK

    for domain in args.domains:
        result = network.tls_check(f"{domain}:{args.port}", args.timeout, args.warn_days)
        ctx.ledger.charge("net:probe", 1, detail=f"tls {domain}")
        days = result.get("days_remaining")
        if not result.get("ok"):
            severity = Severity.CRIT
        elif days is not None and days <= args.crit_days:
            severity = Severity.CRIT
        elif days is not None and days <= args.warn_days:
            severity = Severity.WARN
        else:
            severity = Severity.OK
        worst = max(worst, severity, key=lambda s: s.rank)
        result["severity"] = severity.value
        results.append(result)
        rows.append(
            [
                {"ok": "✓", "info": "•", "warn": "▲", "crit": "✗"}[severity.value],
                domain,
                f"{days:.0f}d" if days is not None else "—",
                (result.get("expires_at") or "")[:10],
                result.get("issuer", "")[:28],
                result.get("protocol", ""),
                result.get("error", "")[:40],
            ]
        )

    con.table(["", "domain", "remaining", "expires", "issuer", "tls", "error"], rows)
    emit(ctx, args, {"certificates": results, "worst": worst.value})
    print_cost(ctx)
    return exit_code_for(worst)
