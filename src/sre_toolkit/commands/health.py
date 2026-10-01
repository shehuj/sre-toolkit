"""`sre health` — the zero-cost first question: is it up, and which layer is broken?"""

from __future__ import annotations

from ..models import Severity
from ._shared import emit, exit_code_for, print_cost

CHECKS = ("dns", "tcp", "tls", "http")


def register(sub) -> None:
    parser = sub.add_parser(
        "health",
        help="probe a URL or host: HTTP, DNS, TLS and TCP (free, no credentials)",
        description="Probe a target from this machine. Costs nothing, needs no AWS access.",
    )
    parser.add_argument("target", help="URL or hostname, e.g. https://api.example.com")
    parser.add_argument(
        "--only", help=f"comma-separated subset of checks: {','.join(CHECKS)}"
    )
    parser.add_argument("--timeout", type=float, default=5.0, help="per-probe timeout seconds")
    parser.add_argument("--expect", type=int, metavar="STATUS",
                        help="treat only this HTTP status as healthy")
    parser.add_argument("--port", type=int, help="override the TCP port probed")
    parser.add_argument("--warn-days", type=int, default=30,
                        help="warn when the certificate expires within N days (default: 30)")
    parser.set_defaults(handler=run)


def run(ctx, args) -> int:
    from ..collectors import network

    selected = [c.strip() for c in (args.only.split(",") if args.only else CHECKS)]
    host, port, url = network._hostport(args.target)
    if args.port:
        port = args.port
    results: list[dict] = []

    con = ctx.console
    con.title("HEALTH CHECK", f"{url}")

    if "dns" in selected:
        results.append(network.dns_check(host))
    if "tcp" in selected:
        results.append(network.tcp_check(host, port, args.timeout))
    if "tls" in selected and (url.startswith("https") or port == 443):
        results.append(network.tls_check(f"{host}:{port}", args.timeout, args.warn_days))
    if "http" in selected:
        results.append(network.http_check(url, args.timeout, expect=args.expect))

    worst = Severity.OK
    for result in results:
        severity = _severity(result)
        worst = max(worst, severity, key=lambda s: s.rank)
        con.bullet(f"{result['check']:<5} {_describe(result)}", severity)

    for result in results:
        ctx.ledger.charge("net:probe", 1, detail=result["check"])

    if any(r["check"] == "http" and r.get("total_ms") for r in results):
        http = next(r for r in results if r["check"] == "http")
        con.section("Timing")
        con.kv("DNS", f"{http.get('dns_ms', '—')} ms")
        con.kv("TCP connect", f"{http.get('connect_ms', '—')} ms")
        con.kv("Total", f"{http.get('total_ms', '—')} ms")
        if http.get("resolved_ip"):
            con.kv("Resolved IP", http["resolved_ip"])

    emit(ctx, args, {"target": url, "checks": results, "worst": worst.value})
    print_cost(ctx)
    return exit_code_for(worst)


def _severity(result: dict) -> Severity:
    if result.get("ok") is False:
        return Severity.CRIT
    if result["check"] == "tls" and result.get("expiring_soon"):
        return Severity.WARN
    if result["check"] == "http":
        status = result.get("status", 0)
        if status >= 500:
            return Severity.CRIT
        if status >= 400:
            return Severity.WARN
        if result.get("total_ms", 0) > 2000:
            return Severity.WARN
    return Severity.OK


def _describe(result: dict) -> str:
    check = result["check"]
    if error := result.get("error"):
        return f"{result.get('target', '')} — {error}"
    if check == "dns":
        records = result.get("records", {})
        answer = ", ".join(records.get("A", []) or records.get("AAAA", []))[:60]
        return f"{result['target']} → {answer} ({result.get('lookup_ms')} ms)"
    if check == "tcp":
        return f"{result['target']} reachable in {result.get('connect_ms')} ms"
    if check == "tls":
        return (
            f"{result.get('subject') or result['target']} valid "
            f"{result.get('days_remaining')} more days, issued by "
            f"{result.get('issuer')}, {result.get('protocol')}"
        )
    if check == "http":
        return (
            f"HTTP {result.get('status')} in {result.get('total_ms')} ms "
            f"({result.get('body_bytes', 0)} bytes{', ' + result['server'] if result.get('server') else ''})"
        )
    return str(result)
