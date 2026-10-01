"""CloudWatch Logs collection and pattern extraction.

Cost model, and the reason this module looks the way it does:

  * `FilterLogEvents` has **no per-GB charge** — you pay for stored data, not for
    reading it. It is therefore the default path, bounded by a time window and a
    hard event limit.
  * `StartQuery` (Logs Insights) is billed **$0.005 per GB scanned**, which on a
    chatty service is the single most expensive thing this toolkit can do. It is
    opt-in behind `--deep`, pre-estimated for the budget guard, and then charged
    against the *actual* bytes scanned that the API reports back.

Pattern extraction happens locally, so the thousands of lines you pull cost
nothing extra to analyse — and the AI step only ever sees the ~15 deduplicated
patterns instead of raw log text.
"""

from __future__ import annotations

import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from ..models import Severity, Signal, SignalKind, TimelineEvent, Window

UTC = timezone.utc

_NORMALISERS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "<hash>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (re.compile(r"\b(?:arn:aws:[^\s\"']+)"), "<arn>"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "<email>"),
    (re.compile(r"/[\w./-]{8,}"), "<path>"),
    (re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|MB|GB|KB)\b"), "<qty>"),
    (re.compile(r"\b\d+\b"), "<n>"),
    (re.compile(r"\s+"), " "),
)

_LEVELS = (
    ("FATAL", Severity.CRIT),
    ("CRITICAL", Severity.CRIT),
    ("ERROR", Severity.CRIT),
    ("SEVERE", Severity.CRIT),
    ("WARN", Severity.WARN),
    ("EXCEPTION", Severity.CRIT),
    ("TRACEBACK", Severity.CRIT),
)

# Failure classes worth naming in a report. Each maps to a correlator rule.
ERROR_CLASSES: tuple[tuple[str, re.Pattern], ...] = (
    ("connection_pool_exhausted", re.compile(
        r"connection pool|pool (?:is )?exhaust|too many connections|no available connection"
        r"|timeout (?:waiting|acquiring) (?:for )?connection|HikariPool|QueuePool limit", re.I)),
    ("db_connection_refused", re.compile(
        r"(?:could not|cannot|unable to) connect to (?:the )?(?:database|postgres|mysql|rds)"
        r"|connection refused.*(?:5432|3306)|FATAL:\s+too many clients", re.I)),
    ("timeout", re.compile(
        r"\btimed? ?out\b|deadline exceeded|ETIMEDOUT|read timeout|gateway time-?out", re.I)),
    ("oom", re.compile(
        r"OutOfMemory|OOMKilled|Cannot allocate memory|heap (?:space|exhaust)|MemoryError", re.I)),
    ("throttled", re.compile(
        r"ThrottlingException|Rate ?exceeded|ProvisionedThroughputExceeded|429|too many requests"
        r"|SlowDown", re.I)),
    ("upstream_5xx", re.compile(
        r"upstream.*(?:50\d)|502 bad gateway|503 service unavailable|504|EHOSTUNREACH"
        r"|connection reset by peer", re.I)),
    ("auth_failure", re.compile(
        r"AccessDenied|UnauthorizedOperation|ExpiredToken|invalid signature|403 forbidden"
        r"|InvalidClientTokenId", re.I)),
    ("dns_failure", re.compile(
        r"(?:name or service not known|temporary failure in name resolution|EAI_AGAIN"
        r"|NXDOMAIN|dns lookup failed)", re.I)),
    ("tls_failure", re.compile(
        r"certificate (?:verify|has expired|unknown)|SSLError|handshake fail|TLS alert", re.I)),
    ("deadlock", re.compile(r"deadlock detected|lock wait timeout|serialization failure", re.I)),
    ("disk_full", re.compile(r"no space left on device|disk (?:is )?full|ENOSPC", re.I)),
    ("crash_loop", re.compile(r"CrashLoopBackOff|restarting failed container|exit code 1(?:37|43)", re.I)),
)

DEFAULT_FILTER = "?ERROR ?Error ?error ?FATAL ?Exception ?exception ?WARN ?Timeout ?timeout"


def normalize(message: str) -> str:
    """Collapse a log line to its shape so near-identical lines group together."""
    text = message.strip()
    for pattern, replacement in _NORMALISERS:
        text = pattern.sub(replacement, text)
    return text.strip()[:400]


def level_of(message: str) -> tuple[str, Severity]:
    upper = message[:200].upper()
    for token, severity in _LEVELS:
        if token in upper:
            return token.title(), severity
    return "Info", Severity.INFO


def classify(message: str) -> list[str]:
    return [name for name, pattern in ERROR_CLASSES if pattern.search(message)]


def collect(
    ctx,
    log_group: str,
    window: Window,
    filter_pattern: str | None = DEFAULT_FILTER,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Pull up to `limit` matching events with FilterLogEvents (no per-GB charge)."""
    kwargs: dict[str, Any] = {
        "logGroupName": log_group,
        "startTime": int(window.start.timestamp() * 1000),
        "endTime": int(window.end.timestamp() * 1000),
        "limit": min(limit, 10_000),
    }
    if filter_pattern:
        kwargs["filterPattern"] = filter_pattern

    events: list[dict[str, Any]] = []
    token: str | None = None
    pages = 0
    while len(events) < limit and pages < 10:  # hard stop: never walk a whole log group
        if token:
            kwargs["nextToken"] = token
        page = ctx.aws.call(
            "logs",
            "filter_log_events",
            "logs:FilterLogEvents",
            detail=f"{log_group} page {pages + 1}",
            **kwargs,
        )
        batch = page.get("events", [])
        events.extend(batch)
        token = page.get("nextToken")
        pages += 1
        if not token or not batch:
            break

    return [
        {
            "at": datetime.fromtimestamp(e["timestamp"] / 1000, UTC),
            "message": e.get("message", "").rstrip(),
            "stream": e.get("logStreamName", ""),
            "group": log_group,
        }
        for e in events[:limit]
    ]


def estimate_insights_gb(ctx, log_group: str, window: Window) -> float:
    """Pre-price an Insights query from stored bytes and retention.

    Deliberately pessimistic: if we cannot work out retention we assume all
    stored bytes fall inside the window, so --max-spend blocks rather than
    surprises you.
    """
    groups = ctx.aws.call(
        "logs",
        "describe_log_groups",
        "logs:DescribeLogGroups",
        logGroupNamePrefix=log_group,
        limit=5,
    ).get("logGroups", [])
    match = next((g for g in groups if g.get("logGroupName") == log_group), None)
    if not match:
        return 0.0
    stored = float(match.get("storedBytes", 0))
    retention_days = match.get("retentionInDays")
    if not retention_days:
        return stored / 1024**3
    fraction = min(1.0, window.seconds / (retention_days * 86400))
    return stored * fraction / 1024**3


def insights(
    ctx,
    log_group: str,
    window: Window,
    query: str,
    limit: int = 200,
    poll_seconds: float = 1.0,
    max_wait: float = 60.0,
) -> dict[str, Any]:
    """Run a Logs Insights query. Billed per GB scanned — hence --deep only."""
    estimated_gb = estimate_insights_gb(ctx, log_group, window)
    ctx.ledger.check("logs:StartQuery", estimated_gb, f"estimate for {log_group}")
    if ctx.dry_run:
        ctx.ledger.charge("logs:StartQuery", estimated_gb,
                          detail=f"{log_group} (dry-run estimate)", billed=False)
        return {"results": [], "statistics": {}, "estimated_gb": estimated_gb}

    cached = ctx.cache.get("logs:insights", {"group": log_group, "q": query,
                                            "s": window.start, "e": window.end, "l": limit})
    if cached is not None:
        ctx.ledger.charge("logs:StartQuery", cached.get("gb_scanned", estimated_gb),
                          detail=f"{log_group} (cached)", billed=False)
        return cached

    started = ctx.aws.call(
        "logs", "start_query", "logs:StartQuery", quantity=0, cacheable=False,
        detail=f"{log_group} ~{estimated_gb:.2f}GB",
        logGroupName=log_group,
        startTime=int(window.start.timestamp()),
        endTime=int(window.end.timestamp()),
        queryString=query,
        limit=limit,
    )
    query_id = started.get("queryId")
    deadline = time.monotonic() + max_wait
    payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        payload = ctx.aws.call(
            "logs", "get_query_results", "logs:GetQueryResults", quantity=0,
            cacheable=False, queryId=query_id,
        )
        if payload.get("status") in ("Complete", "Failed", "Cancelled", "Timeout"):
            break
        time.sleep(poll_seconds)

    stats = payload.get("statistics", {}) or {}
    gb_scanned = float(stats.get("bytesScanned", 0)) / 1024**3
    # Charge what the query actually scanned, not the estimate.
    ctx.ledger.charge("logs:StartQuery", gb_scanned,
                      detail=f"{log_group} scanned {gb_scanned:.3f}GB")
    out = {
        "status": payload.get("status"),
        "results": [
            {f["field"]: f["value"] for f in row} for row in payload.get("results", [])
        ],
        "gb_scanned": gb_scanned,
        "records_matched": stats.get("recordsMatched"),
    }
    ctx.cache.set("logs:insights", {"group": log_group, "q": query, "s": window.start,
                                    "e": window.end, "l": limit}, out)
    return out


def patterns(events: list[dict[str, Any]], top: int = 15) -> list[dict[str, Any]]:
    """Group events into normalised patterns with counts, levels and classes."""
    counter: Counter[str] = Counter()
    meta: dict[str, dict[str, Any]] = {}
    for event in events:
        shape = normalize(event["message"])
        if not shape:
            continue
        counter[shape] += 1
        entry = meta.setdefault(
            shape,
            {"first_seen": event["at"], "last_seen": event["at"], "classes": set(),
             "sample": event["message"][:500], "streams": set()},
        )
        entry["first_seen"] = min(entry["first_seen"], event["at"])
        entry["last_seen"] = max(entry["last_seen"], event["at"])
        entry["classes"].update(classify(event["message"]))
        if event.get("stream"):
            entry["streams"].add(event["stream"])

    out = []
    for shape, count in counter.most_common(top):
        entry = meta[shape]
        label, severity = level_of(entry["sample"])
        out.append(
            {
                "pattern": shape,
                "count": count,
                "level": label,
                "severity": severity.value,
                "classes": sorted(entry["classes"]),
                "first_seen": entry["first_seen"].isoformat(),
                "last_seen": entry["last_seen"].isoformat(),
                "sample": entry["sample"],
                "streams": len(entry["streams"]),
            }
        )
    return out


def to_signals(events: list[dict[str, Any]], window: Window, source: str) -> list[Signal]:
    """Error-rate-shaped signals derived from log volume, plus one per error class."""
    if not events:
        return []
    errors = [e for e in events if level_of(e["message"])[1].rank >= Severity.WARN.rank]
    signals: list[Signal] = []

    buckets: defaultdict[datetime, int] = defaultdict(int)
    bucket_seconds = max(60, window.seconds // 30)
    for event in errors:
        epoch = int(event["at"].timestamp())
        slot = datetime.fromtimestamp(epoch - epoch % bucket_seconds, UTC)
        buckets[slot] += 1
    series = sorted(buckets.items())
    if series:
        counts = [c for _, c in series]
        signals.append(
            Signal(
                name="Log error volume",
                kind=SignalKind.LOG,
                source=source,
                severity=Severity.CRIT if len(errors) > 50 else Severity.WARN,
                summary=f"{len(errors)} error/warn lines across {len(series)} buckets",
                baseline=min(counts),
                peak=max(counts),
                unit="Count",
                first_seen=series[0][0],
                series=series,
                evidence=[e["message"][:200] for e in errors[:3]],
            )
        )

    class_counts: Counter[str] = Counter()
    class_first: dict[str, datetime] = {}
    for event in events:
        for name in classify(event["message"]):
            class_counts[name] += 1
            class_first.setdefault(name, event["at"])
    for name, count in class_counts.most_common():
        signals.append(
            Signal(
                name=f"log:{name}",
                kind=SignalKind.LOG,
                source=source,
                severity=Severity.CRIT if count >= 10 else Severity.WARN,
                summary=f"{count} lines matched the {name.replace('_', ' ')} signature",
                peak=float(count),
                unit="Count",
                first_seen=class_first.get(name),
                tags={"class": name},
            )
        )
    return signals


def to_events(log_patterns: list[dict[str, Any]], top: int = 3) -> list[TimelineEvent]:
    """Promote the first occurrence of the loudest error patterns onto the timeline."""
    out = []
    for entry in log_patterns[:top]:
        if entry["severity"] not in (Severity.WARN.value, Severity.CRIT.value):
            continue
        out.append(
            TimelineEvent(
                at=datetime.fromisoformat(entry["first_seen"]),
                source="logs",
                title=f"First '{entry['level']}' log: {entry['pattern'][:70]}",
                detail=f"{entry['count']} occurrences in window",
                severity=Severity(entry["severity"]),
            )
        )
    return out


def guess_log_groups(ctx, service: str, limit: int = 5) -> list[str]:
    """Find plausible log groups for a service name. DescribeLogGroups is free."""
    candidates: list[str] = []
    for prefix in (f"/aws/ecs/{service}", f"/ecs/{service}", f"/aws/lambda/{service}",
                   f"/aws/eks/{service}", service, f"/{service}"):
        groups = ctx.aws.call(
            "logs", "describe_log_groups", "logs:DescribeLogGroups",
            detail=prefix, logGroupNamePrefix=prefix, limit=limit,
        ).get("logGroups", [])
        candidates.extend(g["logGroupName"] for g in groups)
        if candidates:
            break
    seen, out = set(), []
    for name in candidates:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out[:limit]
