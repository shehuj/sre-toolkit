"""`sre cache` — inspect and clear the response cache that keeps re-runs free."""

from __future__ import annotations

from ._shared import emit


def register(sub) -> None:
    parser = sub.add_parser(
        "cache", help="inspect or clear the API response cache",
        description="The cache is why re-running an investigation costs nothing.",
    )
    inner = parser.add_subparsers(dest="action", metavar="<action>")

    stats = inner.add_parser("stats", help="show cache location, size and TTL")
    stats.set_defaults(handler=run_stats)

    clear = inner.add_parser("clear", help="delete all cached responses")
    clear.set_defaults(handler=run_clear)


def run_stats(ctx, args) -> int:
    root = ctx.cache.root
    files = list(root.rglob("*.json")) if root.exists() else []
    size = sum(f.stat().st_size for f in files)
    con = ctx.console
    con.title("RESPONSE CACHE")
    con.kv("Path", str(root))
    con.kv("Entries", len(files))
    con.kv("Size", f"{size / 1024:.1f} KiB")
    con.kv("TTL", f"{ctx.cache.ttl}s")
    con.kv("Enabled", ctx.cache.enabled)
    by_op: dict[str, int] = {}
    for path in files:
        by_op[path.parent.name] = by_op.get(path.parent.name, 0) + 1
    if by_op:
        con.section("Entries by operation")
        con.table(["operation", "entries"],
                  sorted(by_op.items(), key=lambda kv: -kv[1]))
    emit(ctx, args, {"path": str(root), "entries": len(files), "bytes": size,
                     "ttl_seconds": ctx.cache.ttl, "by_operation": by_op})
    return 0


def run_clear(ctx, args) -> int:
    removed = ctx.cache.clear()
    ctx.console.out(f"removed {removed} cached response(s) from {ctx.cache.root}")
    emit(ctx, args, {"removed": removed})
    return 0
