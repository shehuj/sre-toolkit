"""Deterministic, zero-cost incident narrative.

This is the default analysis path. It reads the same correlated snapshot the AI
would read and writes the summary in plain prose. No API call, no credentials,
no per-token cost, same output every time — which also makes it the thing the
AI output gets compared against.
"""

from __future__ import annotations

from ..models import Severity, SignalKind, Snapshot
from ..render import fmt_delta, fmt_num


def narrate(snap: Snapshot) -> str:
    parts: list[str] = []
    degraded = sorted(
        [s for s in snap.signals if s.severity.rank >= Severity.WARN.rank],
        key=lambda s: -s.severity.rank,
    )

    if not degraded:
        return (
            f"No signal for {snap.service} crossed a warning threshold between "
            f"{snap.window}. {len(snap.signals)} signals were collected from "
            f"{len(snap.collectors_run)} collectors."
        )

    worst = degraded[0]
    parts.append(
        f"{snap.service} degraded between {snap.window}. The strongest signal is "
        f"{worst.name} ({worst.summary})."
    )

    metrics = [s for s in degraded if s.kind == SignalKind.METRIC]
    if len(metrics) > 1:
        movers = ", ".join(
            f"{s.name} {fmt_num(s.baseline, s.unit)}→{fmt_num(s.peak, s.unit)} ({fmt_delta(s)})"
            for s in metrics[:4]
        )
        parts.append(f"Metrics that moved against baseline: {movers}.")

    onsets = [s for s in degraded if s.first_seen]
    if onsets:
        first = min(onsets, key=lambda s: s.first_seen)
        parts.append(
            f"The earliest degraded signal was {first.name} at "
            f"{first.first_seen.strftime('%H:%M:%S')} UTC."
        )

    changes = [
        e for e in snap.sorted_events()
        if "deploy" in e.source.lower() or "deployment" in e.title.lower()
    ]
    if changes:
        change = changes[0]
        parts.append(
            f"A change landed in or just before the window: {change.title} at "
            f"{change.at.strftime('%H:%M:%S')} UTC."
        )
    else:
        parts.append("No deployment or configuration change was found in the collected window.")

    if snap.log_patterns:
        top = snap.log_patterns[0]
        classes = ", ".join(top.get("classes", [])) or "unclassified"
        parts.append(
            f"The loudest log pattern occurred {top['count']}× and matches: {classes}."
        )

    if snap.findings:
        best = max(snap.findings, key=lambda f: f.confidence)
        parts.append(
            f"Leading hypothesis ({best.confidence_label} confidence): {best.title}. "
            f"{best.rationale}"
        )

    parts.append(
        "This summary is generated from the collected signals by fixed rules — no model was "
        "called and no remediation was performed."
    )
    return "\n\n".join(parts)
