"""Context compaction for the AI step.

The expensive mistake in an LLM-backed ops tool is pasting raw telemetry into the
prompt. A 30-minute incident can be 50MB of logs; sending even 1% of that is
both costly and worse for quality. So the model receives:

  * one line per signal (baseline → peak, severity, onset),
  * one line per timeline event,
  * the top N *normalised* log patterns with counts, never raw log bodies,
  * the deterministic findings, so the model critiques rather than re-derives.

That is a bounded ~2-4k input tokens regardless of incident size.
"""

from __future__ import annotations

from ..models import Severity, Snapshot
from ..render import fmt_delta, fmt_num

SYSTEM = (
    "You are an SRE assistant summarising a collected incident snapshot for an on-call engineer. "
    "Rules you must follow:\n"
    "1. Use only the evidence in the snapshot. If something is not in the snapshot, say it is "
    "unknown rather than inferring it.\n"
    "2. Never recommend an automated remediation, and never imply any action was taken.\n"
    "3. Distinguish correlation from causation explicitly.\n"
    "4. Prefer the cheapest falsifiable next check over a broad investigation.\n"
    "5. Be concise and concrete. No pleasantries, no restating the question."
)

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "3-6 sentences: what happened, impact, and the most likely mechanism.",
        },
        "confidence": {
            "type": "string",
            "enum": ["low", "medium", "high"],
            "description": "Confidence that the stated mechanism is correct.",
        },
        "contributing_factors": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "why": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["title", "why", "confidence"],
                "additionalProperties": False,
            },
        },
        "next_checks": {
            "type": "array",
            "maxItems": 4,
            "items": {"type": "string"},
            "description": "Specific, cheap checks an engineer can run next.",
        },
        "missing_evidence": {
            "type": "array",
            "maxItems": 3,
            "items": {"type": "string"},
            "description": "What was not collected that would most change the conclusion.",
        },
    },
    "required": ["summary", "confidence", "contributing_factors", "next_checks"],
    "additionalProperties": False,
}


def build(snap: Snapshot, max_log_patterns: int = 8, max_chars: int = 6000) -> str:
    lines: list[str] = [
        f"service: {snap.service}",
        f"window_utc: {snap.window.start.isoformat()} .. {snap.window.end.isoformat()}",
        f"region: {snap.region or 'unknown'}",
        f"collectors: {', '.join(snap.collectors_run) or 'none'}",
    ]
    if snap.collector_errors:
        failures = "; ".join(f"{k}={v}" for k, v in snap.collector_errors.items())
        lines.append(f"collector_errors: {failures}")

    lines.append("\nSIGNALS (name | severity | baseline -> peak | change | onset_utc)")
    for sig in sorted(snap.signals, key=lambda s: -s.severity.rank)[:25]:
        onset = sig.first_seen.strftime("%H:%M:%S") if sig.first_seen else "-"
        lines.append(
            f"- {sig.name} | {sig.severity.value} | "
            f"{fmt_num(sig.baseline, sig.unit)} -> {fmt_num(sig.peak, sig.unit)} | "
            f"{fmt_delta(sig)} | {onset}"
        )

    if snap.events:
        lines.append("\nTIMELINE (utc | source | event)")
        for ev in snap.sorted_events()[:25]:
            stamp = ev.at.strftime("%H:%M:%S")
            lines.append(f"- {stamp} | {ev.source} | {ev.title} {ev.detail}".rstrip())

    if snap.log_patterns:
        lines.append("\nLOG PATTERNS (count | level | classes | normalised pattern)")
        for entry in snap.log_patterns[:max_log_patterns]:
            classes = ",".join(entry.get("classes", [])) or "-"
            lines.append(
                f"- {entry['count']} | {entry.get('level', '-')} | {classes} | "
                f"{entry['pattern'][:180]}"
            )

    if snap.resources:
        lines.append("\nRESOURCES")
        for res in snap.resources[:8]:
            attrs = ", ".join(f"{k}={v}" for k, v in list(res.attributes.items())[:8])
            lines.append(f"- {res.kind} {res.identifier}: {attrs}"[:300])

    if snap.findings:
        lines.append("\nRULE-BASED FINDINGS (these came from deterministic correlation)")
        for finding in snap.findings:
            label = finding.confidence_label
            lines.append(f"- [{label}] {finding.title}: {finding.rationale}"[:400])

    lines.append(
        "\nTASK: Confirm, refine or contradict the rule-based findings using only the evidence "
        "above, and name the cheapest next checks."
    )

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[context truncated to stay within the token budget]"
    return text


def estimate_tokens(text: str) -> int:
    """~4 characters per token is close enough for a pre-flight budget check."""
    return max(1, len(text) // 4)


def severity_hint(snap: Snapshot) -> str:
    worst = snap.worst_severity()
    return {
        Severity.CRIT: "customer-impacting",
        Severity.WARN: "degraded",
        Severity.INFO: "nominal",
        Severity.OK: "nominal",
    }[worst]
