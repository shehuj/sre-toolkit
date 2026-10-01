"""`sre incident` — the centrepiece.

The subcommands split deliberately along the cost boundary:

  snapshot   collects and saves the artefact (billed once)
  investigate collect + correlate + explain in one go
  summarize  re-analyses a saved artefact (free, repeatable, works offline)
  timeline   prints just the ordered events from an artefact (free)

Collect once, analyse many times.
"""

from __future__ import annotations

from ..errors import SreToolkitError
from ..models import Severity
from ..render import render_snapshot
from ._shared import (
    add_target_args,
    add_window_args,
    demo_snapshot,
    emit,
    exit_code_for,
    load_snapshot,
    print_cost,
    target_from_args,
)


def register(sub) -> None:
    parser = sub.add_parser(
        "incident", help="collect, correlate and explain an incident",
        description="Incident collection and investigation.",
    )
    inner = parser.add_subparsers(dest="action", metavar="<action>")

    snap = inner.add_parser("snapshot", help="collect an incident artefact and save it")
    add_target_args(snap)
    add_window_args(snap)
    snap.add_argument("--log-limit", type=int, default=1000,
                      help="max log events to pull per group (default: 1000)")
    snap.set_defaults(handler=run_snapshot)

    inv = inner.add_parser("investigate", help="collect, correlate and explain in one run")
    add_target_args(inv)
    add_window_args(inv)
    inv.add_argument("--log-limit", type=int, default=1000)
    inv.add_argument("--max-findings", type=int, default=5)
    inv.set_defaults(handler=run_investigate)

    summ = inner.add_parser(
        "summarize", aliases=["summarise"],
        help="re-analyse a saved snapshot (free; add --ai for a model narrative)",
    )
    summ.add_argument("file", nargs="?", help="snapshot JSON from `incident snapshot`")
    summ.add_argument("--max-findings", type=int, default=5)
    summ.set_defaults(handler=run_summarize)

    tl = inner.add_parser("timeline", help="print the ordered timeline from a snapshot")
    tl.add_argument("file", nargs="?")
    tl.set_defaults(handler=run_timeline)

    pm = inner.add_parser("postmortem", help="emit a Markdown post-mortem skeleton from a snapshot")
    pm.add_argument("file", nargs="?")
    pm.set_defaults(handler=run_postmortem)


def _source_snapshot(ctx, args):
    if ctx.demo:
        return demo_snapshot()
    path = getattr(args, "file", None)
    if not path:
        raise SreToolkitError(
            "pass a snapshot file, or use --demo to work on the bundled example:\n"
            "    sre --demo incident summarize"
        )
    return load_snapshot(path)


def run_snapshot(ctx, args) -> int:
    from ..investigate import snapshot

    if ctx.demo:
        snap = demo_snapshot()
    else:
        window = ctx.resolve_window(args.start, args.end, args.minutes)
        snap = snapshot(ctx, target_from_args(ctx, args), window, log_limit=args.log_limit)
        snap.cost = ctx.finish()

    con = ctx.console
    con.title("INCIDENT SNAPSHOT", f"{snap.service} · {snap.window}")
    con.kv("Collectors", ", ".join(snap.collectors_run) or "none")
    con.kv("Signals", len(snap.signals))
    con.kv("Events", len(snap.events))
    con.kv("Log patterns", len(snap.log_patterns))
    for name, message in snap.collector_errors.items():
        con.warn(f"{name}: {message}")
    con.out()
    con.out(con.style("analyse it for free with:  sre incident summarize <file>", "dim"))

    emit(ctx, args, snap.to_dict())
    print_cost(ctx)
    return exit_code_for(snap.worst_severity())


def run_investigate(ctx, args) -> int:
    from ..investigate import Target, analyse, investigate

    if ctx.demo:
        snap = analyse(ctx, demo_snapshot(), max_findings=args.max_findings)
    else:
        window = ctx.resolve_window(args.start, args.end, args.minutes)
        target: Target = target_from_args(ctx, args)
        snap = investigate(
            ctx, target, window, log_limit=args.log_limit, max_findings=args.max_findings
        )

    render_snapshot(ctx.console, snap)
    emit(ctx, args, snap.to_dict())
    return exit_code_for(snap.worst_severity())


def run_summarize(ctx, args) -> int:
    from ..investigate import analyse

    snap = _source_snapshot(ctx, args)
    snap = analyse(ctx, snap, max_findings=args.max_findings)
    render_snapshot(ctx.console, snap)
    emit(ctx, args, snap.to_dict())
    return exit_code_for(snap.worst_severity())


def run_timeline(ctx, args) -> int:
    snap = _source_snapshot(ctx, args)
    con = ctx.console
    con.title("INCIDENT TIMELINE", f"{snap.service} · {snap.window}")
    marks = {"ok": "✓", "info": "•", "warn": "▲", "crit": "✗"}
    for event in snap.sorted_events():
        con.out(
            f"{con.style(event.at.strftime('%H:%M:%S'), 'dim')}  "
            f"{marks[event.severity.value]} {event.title}"
            + (con.style(f"  {event.detail}", "dim") if event.detail else "")
        )
    emit(ctx, args, {"service": snap.service, "events": snap.to_dict()["events"]})
    return 0


def run_postmortem(ctx, args) -> int:
    """A skeleton, filled with the facts already collected — never invented prose."""
    from ..investigate import analyse

    snap = _source_snapshot(ctx, args)
    if not snap.findings:
        snap = analyse(ctx, snap)
    events = snap.sorted_events()
    worst = snap.worst_severity()
    impacted = [s for s in snap.signals if s.severity.rank >= Severity.WARN.rank]

    lines = [
        f"# Post-incident review: {snap.service}",
        "",
        f"- **Window (UTC):** {snap.window}",
        f"- **Severity observed:** {worst.value}",
        f"- **Region:** {snap.region or 'n/a'}",
        f"- **Data sources:** {', '.join(snap.collectors_run) or 'none'}",
        "",
        "## Impact",
        "",
    ]
    lines += [f"- {s.name}: {s.summary}" for s in impacted] or ["- _to be completed_"]
    lines += ["", "## Timeline (UTC)", ""]
    lines += [
        f"- `{e.at.strftime('%H:%M:%S')}` {e.title}{(' — ' + e.detail) if e.detail else ''}"
        for e in events
    ] or ["- _to be completed_"]
    lines += ["", "## Contributing factors", ""]
    for finding in sorted(snap.findings, key=lambda f: -f.confidence):
        lines += [
            f"### {finding.title} ({finding.confidence_label} confidence)",
            "",
            finding.rationale,
            "",
        ]
        lines += [f"- Evidence: {e}" for e in finding.evidence]
        lines.append("")
    lines += [
        "## Open questions",
        "",
    ]
    lines += [f"- [ ] {step}" for f in snap.findings for step in f.next_steps][:8]
    lines += [
        "",
        "## Action items",
        "",
        "| Action | Owner | Due | Ticket |",
        "| --- | --- | --- | --- |",
        "|  |  |  |  |",
        "",
        "## Notes",
        "",
        "_Generated by sre-toolkit from collected telemetry. Findings are correlations, "
        "not confirmed causes, and no remediation was performed._",
    ]

    text = "\n".join(lines)
    if args.output:
        from pathlib import Path

        Path(args.output).write_text(text + "\n")
        ctx.console.out(f"wrote {args.output}")
    else:
        print(text)
    return 0
