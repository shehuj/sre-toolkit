"""Run the rule set over a snapshot and rank the results."""

from __future__ import annotations

from ..models import Finding, Severity, Snapshot
from .rules import RULES


def correlate(snap: Snapshot, max_findings: int = 5) -> list[Finding]:
    findings: list[Finding] = []
    for rule in RULES:
        try:
            finding = rule(snap)
        except Exception:  # noqa: BLE001 - one broken rule must not kill a report
            continue
        if finding is not None:
            findings.append(finding)

    findings.sort(key=lambda f: -f.confidence)
    if not findings:
        findings.append(_nothing_found(snap))
    return findings[:max_findings]


def _nothing_found(snap: Snapshot) -> Finding:
    checked = ", ".join(snap.collectors_run) or "no collectors"
    degraded = [s for s in snap.signals if s.severity.rank >= Severity.WARN.rank]
    if degraded:
        return Finding(
            title="Degradation observed but no known pattern matched",
            confidence=0.2,
            rationale=(
                "Signals left their baseline but did not match a known failure signature. Treat "
                "the signal list as the starting point rather than the conclusion."
            ),
            evidence=[f"{s.name}: {s.summary}" for s in degraded[:5]],
            next_steps=[
                "Widen the window with --start/--end to catch the onset.",
                "Add --deep to run a Logs Insights query over the same window (this costs money "
                "per GB scanned).",
                "Check the dependencies of this service directly.",
            ],
        )
    return Finding(
        title="No degradation detected in this window",
        confidence=0.1,
        rationale=(
            f"Collectors that ran: {checked}. Nothing crossed a warning threshold, so either the "
            "window misses the event or the affected component was not collected."
        ),
        evidence=[],
        next_steps=[
            "Confirm the incident window in UTC — a local-time window is the usual culprit.",
            "Name the component explicitly (--cluster/--service/--instance/--target-group).",
        ],
    )
