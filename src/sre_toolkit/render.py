"""Terminal rendering. Stdlib only — no rich, no colorama.

Honours NO_COLOR, non-TTY pipes and --json.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from typing import Any, Iterable, Sequence

from .models import Finding, Severity, Snapshot

RESET = "\033[0m"
_STYLES = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
}
_SEV_STYLE = {
    Severity.OK: "green",
    Severity.INFO: "cyan",
    Severity.WARN: "yellow",
    Severity.CRIT: "red",
}
_SEV_MARK = {Severity.OK: "✓", Severity.INFO: "•", Severity.WARN: "▲", Severity.CRIT: "✗"}


class Console:
    def __init__(self, stream=None, color: bool | None = None, json_mode: bool = False):
        self.stream = stream or sys.stdout
        self.json_mode = json_mode
        if color is None:
            color = (
                self.stream.isatty()
                and os.environ.get("NO_COLOR") is None
                and os.environ.get("TERM") != "dumb"
            )
        self.color = color

    @property
    def width(self) -> int:
        try:
            return min(shutil.get_terminal_size((100, 24)).columns, 100)
        except OSError:
            return 100

    def style(self, text: str, *names: str) -> str:
        if not self.color or not names:
            return text
        return "".join(_STYLES.get(n, "") for n in names) + text + RESET

    def out(self, text: str = "") -> None:
        if self.json_mode:
            return
        print(text, file=self.stream)

    def title(self, text: str, subtitle: str = "") -> None:
        self.out()
        self.out(self.style(text.upper(), "bold"))
        self.out(self.style("─" * min(self.width, max(len(text), 40)), "dim"))
        if subtitle:
            self.out(self.style(subtitle, "dim"))

    def section(self, text: str) -> None:
        self.out()
        self.out(self.style(text.upper(), "bold"))

    def kv(self, key: str, value: Any, width: int = 18) -> None:
        self.out(f"{self.style(key.ljust(width), 'dim')} {value}")

    def bullet(self, text: str, severity: Severity | None = None, indent: int = 0) -> None:
        pad = " " * indent
        if severity is None:
            self.out(f"{pad}• {text}")
        else:
            mark = self.style(_SEV_MARK[severity], _SEV_STYLE[severity])
            self.out(f"{pad}{mark} {text}")

    def table(self, headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
        rows = [[("" if c is None else str(c)) for c in r] for r in rows]
        if not rows:
            self.out(self.style("  (none)", "dim"))
            return
        widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row[: len(widths)]):
                widths[i] = max(widths[i], len(cell))
        head = "  ".join(h.upper().ljust(w) for h, w in zip(headers, widths))
        self.out(self.style(head, "dim"))
        for row in rows:
            self.out("  ".join(c.ljust(w) for c, w in zip(row, widths)))

    def json(self, payload: Any) -> None:
        print(json.dumps(payload, indent=2, default=str), file=self.stream)

    def warn(self, text: str) -> None:
        print(self.style(f"warning: {text}", "yellow"), file=sys.stderr)

    def error(self, text: str) -> None:
        print(self.style(f"error: {text}", "red"), file=sys.stderr)

    def sparkline(self, values: Sequence[float], width: int = 32) -> str:
        pts = [v for v in values if v is not None]
        if not pts:
            return ""
        if len(pts) > width:  # decimate rather than interpolate; cheap and honest
            step = len(pts) / width
            pts = [pts[int(i * step)] for i in range(width)]
        blocks = "▁▂▃▄▅▆▇█"
        lo, hi = min(pts), max(pts)
        span = (hi - lo) or 1.0
        return "".join(blocks[min(7, int((v - lo) / span * 7.999))] for v in pts)


def fmt_num(value: float | None, unit: str = "") -> str:
    if value is None:
        return "—"
    if unit in ("Percent", "%"):
        return f"{value:.2f}%"
    if unit in ("Seconds", "s"):
        return f"{value * 1000:.0f}ms" if value < 1 else f"{value:.2f}s"
    if unit in ("Milliseconds", "ms"):
        return f"{value:.0f}ms"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}k"
    if abs(value) >= 10:
        return f"{value:.1f}"
    return f"{value:.3g}"


def fmt_delta(signal) -> str:
    ratio = signal.change_ratio()
    if ratio is None:
        return "—"
    if ratio == float("inf"):
        return "new"
    if ratio >= 1:
        return f"×{ratio:.1f}"
    return f"−{(1 - ratio) * 100:.0f}%"


def fmt_usd(usd: float) -> str:
    if usd == 0:
        return "$0.00 (free-tier APIs only)"
    if usd < 0.0001:
        return f"${usd:.6f}"
    if usd < 0.01:
        return f"${usd:.4f}"
    return f"${usd:.2f}"


def fmt_usd_cell(usd: float) -> str:
    """Table-friendly: 'free' instead of a sentence, full precision below a cent."""
    if usd == 0:
        return "free"
    if usd < 0.0001:
        return f"${usd:.6f}"
    if usd < 0.01:
        return f"${usd:.4f}"
    return f"${usd:.2f}"


def render_snapshot(con: Console, snap: Snapshot, show_series: bool = True) -> None:
    """The human-readable incident report."""
    con.title("INCIDENT INVESTIGATION" if snap.findings else "INCIDENT SNAPSHOT")
    con.kv("Service", snap.service)
    con.kv("Window", str(snap.window))
    if snap.region:
        con.kv("Region", snap.region)
    con.kv("Collectors", ", ".join(snap.collectors_run) or "none")
    if snap.collector_errors:
        for name, msg in snap.collector_errors.items():
            con.warn(f"{name}: {msg}")

    impact = [s for s in snap.signals if s.severity.rank >= Severity.WARN.rank]
    con.section("Impact")
    if not impact:
        con.bullet("No signal crossed a warning threshold in this window.", Severity.OK)
    for sig in sorted(impact, key=lambda s: -s.severity.rank):
        line = f"{sig.name}: {sig.summary}" if sig.summary else sig.name
        con.bullet(line, sig.severity)

    if snap.events:
        con.section("Timeline")
        for ev in snap.sorted_events():
            stamp = ev.at.strftime("%H:%M:%S")
            mark = con.style(_SEV_MARK[ev.severity], _SEV_STYLE[ev.severity])
            detail = con.style(f"  {ev.detail}", "dim") if ev.detail else ""
            con.out(f"{con.style(stamp, 'dim')}  {mark} {ev.title}{detail}")

    if snap.signals:
        con.section("Correlated signals")
        rows = []
        for sig in sorted(snap.signals, key=lambda s: -s.severity.rank):
            row = [
                _SEV_MARK[sig.severity],
                sig.name,
                sig.source,
                fmt_num(sig.baseline, sig.unit),
                fmt_num(sig.peak, sig.unit),
                fmt_delta(sig),
            ]
            if show_series:
                row.append(con.sparkline([v for _, v in sig.series]))
            rows.append(row)
        headers = ["", "signal", "source", "baseline", "peak", "change"]
        if show_series:
            headers.append("shape")
        con.table(headers, rows)

    if snap.log_patterns:
        con.section("Top log patterns")
        con.table(
            ["count", "level", "pattern"],
            [
                [p.get("count"), p.get("level", ""), _truncate(p.get("pattern", ""), con.width - 20)]
                for p in snap.log_patterns[:8]
            ],
        )

    if snap.findings:
        con.section("Likely contributing factors")
        for i, f in enumerate(sorted(snap.findings, key=lambda f: -f.confidence), 1):
            label = con.style(f"[{f.confidence_label} confidence]", "dim")
            con.out(f"{i}. {con.style(f.title, 'bold')} {label}")
            con.out(f"   {f.rationale}")
            for ev in f.evidence:
                con.out(con.style(f"     ↳ {ev}", "dim"))

    if snap.narrative:
        con.section("Analysis")
        for line in _wrap(snap.narrative, con.width):
            con.out(line)

    # Ordered by the confidence of the finding that suggested them, capped so the
    # report ends with a short list someone will actually work through.
    ranked = sorted(snap.findings, key=lambda f: -f.confidence)
    steps = _dedupe([s for f in ranked for s in f.next_steps])[:6]
    if steps:
        con.section("Recommended investigation")
        for i, step in enumerate(steps, 1):
            con.out(f"{i}. {step}")

    con.section("Cost of this run")
    con.kv("Spent", fmt_usd(snap.cost.get("total_usd", 0.0)))
    if snap.cost.get("avoided_usd"):
        con.kv("Avoided (cache)", fmt_usd(snap.cost["avoided_usd"]))
    con.out()
    con.out(con.style("NO AUTOMATED REMEDIATION PERFORMED", "dim"))


def render_findings(con: Console, findings: list[Finding]) -> None:
    for f in sorted(findings, key=lambda f: -f.confidence):
        con.out(f"• {con.style(f.title, 'bold')} ({f.confidence_label})")
        con.out(f"  {f.rationale}")


def _truncate(text: str, width: int) -> str:
    width = max(20, width)
    return text if len(text) <= width else text[: width - 1] + "…"


def _wrap(text: str, width: int) -> list[str]:
    import textwrap

    out: list[str] = []
    for para in text.split("\n"):
        out.extend(textwrap.wrap(para, width=width) or [""])
    return out


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
