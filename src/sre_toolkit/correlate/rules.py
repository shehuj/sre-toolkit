"""Correlation rules.

Each rule is a pure function of the snapshot, so it is free to run, trivially
testable, and produces the same answer every time — which is what you want at
03:00. The AI layer (optional) writes prose *about* these findings; it never
replaces them.

Confidence is assembled from evidence rather than guessed: a base score for the
pattern matching at all, plus bonuses for corroborating signals and correct
temporal ordering.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from ..models import Finding, Severity, Snapshot

Rule = Callable[[Snapshot], "Finding | None"]
RULES: list[Rule] = []


def rule(fn: Rule) -> Rule:
    RULES.append(fn)
    return fn


@dataclass
class Builder:
    """Small helper so rules read like prose and clamp their own confidence."""

    title: str
    base: float
    rationale: str
    evidence: list[str]
    next_steps: list[str]

    def finish(self, bonus: float = 0.0) -> Finding:
        return Finding(
            title=self.title,
            confidence=max(0.05, min(0.95, self.base + bonus)),
            rationale=self.rationale,
            evidence=self.evidence,
            next_steps=self.next_steps,
        )


# --- helpers ------------------------------------------------------------
def _by_class(snap: Snapshot, name: str):
    return [s for s in snap.signals if s.tags.get("class") == name]


def _log_class(snap: Snapshot, name: str):
    return [s for s in snap.signals if s.name == f"log:{name}"]


def _named(snap: Snapshot, *fragments: str):
    out = []
    for sig in snap.signals:
        lowered = sig.name.lower()
        if any(f.lower() in lowered for f in fragments):
            out.append(sig)
    return out


def _bad(signals) -> list:
    return [s for s in signals if s.severity.rank >= Severity.WARN.rank]


def _deploys(snap: Snapshot):
    return [
        e for e in snap.events
        if "deploy" in e.source.lower()
        or "deployment" in e.title.lower()
        or "UpdateService" in e.title
        or "RegisterTaskDefinition" in e.title
    ]


def _error_onset(snap: Snapshot):
    candidates = [
        s.first_seen
        for s in snap.signals
        if s.first_seen and s.severity.rank >= Severity.WARN.rank
    ]
    return min(candidates) if candidates else None


def _fmt_time(dt) -> str:
    return dt.strftime("%H:%M:%S") if dt else "unknown time"


# --- rules --------------------------------------------------------------
@rule
def change_correlation(snap: Snapshot) -> Finding | None:
    deploys = _deploys(snap)
    onset = _error_onset(snap)
    if not deploys or not onset:
        return None
    before = [d for d in deploys if d.at <= onset]
    if not before:
        return None
    deploy = max(before, key=lambda d: d.at)
    gap = onset - deploy.at
    if gap > timedelta(minutes=30):
        return None

    bonus = 0.0
    if gap <= timedelta(minutes=5):
        bonus += 0.15
    elif gap <= timedelta(minutes=15):
        bonus += 0.08
    impacted = _bad(snap.signals)
    bonus += min(0.1, 0.02 * len(impacted))
    return Builder(
        title="Change correlation: failure began shortly after a deployment",
        base=0.6,
        rationale=(
            f"{deploy.title} at {_fmt_time(deploy.at)} precedes the first degraded signal at "
            f"{_fmt_time(onset)} by {int(gap.total_seconds() // 60)}m"
            f"{int(gap.total_seconds() % 60)}s. Temporal adjacency is not causation, but this is "
            "the cheapest hypothesis to falsify first."
        ),
        evidence=[f"{_fmt_time(deploy.at)} {deploy.title} {deploy.detail}".strip()]
        + [
            f"{_fmt_time(s.first_seen)} {s.name} — {s.summary}"
            for s in impacted[:4]
            if s.first_seen
        ],
        next_steps=[
            "Diff the deployed revision against the previous one (task definition, image tag, "
            "config).",
            "Check whether the previous revision is still available for a rollback decision.",
            "Compare per-task metrics across the two revisions before rolling forward.",
        ],
    ).finish(bonus)


@rule
def db_connection_exhaustion(snap: Snapshot) -> Finding | None:
    saturation = _bad(_by_class(snap, "connection_saturation"))
    pool_logs = _log_class(snap, "connection_pool_exhausted")
    refused = _log_class(snap, "db_connection_refused")
    conns = _named(snap, "DB connections")
    if not (saturation or pool_logs or refused):
        return None

    bonus = 0.0
    evidence = []
    if saturation:
        bonus += 0.2
        evidence.append(f"{saturation[0].name}: {saturation[0].summary}")
    elif conns:
        evidence.append(f"{conns[0].name}: {conns[0].summary}")
    if pool_logs:
        bonus += 0.15
        evidence.append(f"{pool_logs[0].name}: {pool_logs[0].summary}")
    if refused:
        bonus += 0.1
        evidence.append(f"{refused[0].name}: {refused[0].summary}")
    if _bad(_named(snap, "5xx")):
        bonus += 0.05
        evidence.append("HTTP 5xx rose in the same window")

    return Builder(
        title="Database connection exhaustion",
        base=0.5,
        rationale=(
            "Connection demand reached the instance ceiling, so requests queued for a connection "
            "and timed out at the application tier. This presents as 5xx with high latency while "
            "database CPU stays unremarkable."
        ),
        evidence=evidence,
        next_steps=[
            "Compare the application connection-pool size × task count against the instance "
            "max_connections.",
            "Check whether the deployment changed pool size, pool timeout, or task count.",
            "Review RDS Performance Insights for the same window to confirm sessions waiting on "
            "connection acquisition rather than on queries.",
        ],
    ).finish(bonus)


@rule
def resource_saturation(snap: Snapshot) -> Finding | None:
    cpu = _bad(_named(snap, "cpu"))
    mem = _bad(_named(snap, "memory"))
    latency = _bad(_named(snap, "latency", "responsetime"))
    if not (cpu or mem):
        return None
    bonus = 0.1 if latency else 0.0
    bonus += 0.1 if any(s.severity == Severity.CRIT for s in cpu + mem) else 0.0
    return Builder(
        title="Compute resource saturation",
        base=0.45,
        rationale=(
            "CPU or memory utilisation left its baseline band during the window. Saturated tasks "
            "queue work, which shows up first as latency and then as timeouts upstream."
        ),
        evidence=[f"{s.name}: {s.summary}" for s in (cpu + mem + latency)[:4]],
        next_steps=[
            "Check whether traffic grew or per-request cost grew (requests vs. CPU per request).",
            "Confirm task/pod sizing and autoscaling thresholds for this service.",
            "Look for a regression in a hot code path introduced by the current revision.",
        ],
    ).finish(bonus)


@rule
def memory_kill_loop(snap: Snapshot) -> Finding | None:
    oom = _by_class(snap, "oom") + _log_class(snap, "oom")
    crash = _by_class(snap, "crash_loop") + _log_class(snap, "crash_loop")
    terminations = _bad(_named(snap, "task terminations", "pod health"))
    if not (oom or crash):
        return None
    bonus = 0.15 if terminations else 0.0
    return Builder(
        title="Container restart loop (out-of-memory or failing start)",
        base=0.6,
        rationale=(
            "Containers are being killed and restarted, so capacity drops while the restart loop "
            "continues. Each restart also discards warm caches and open connections, which "
            "amplifies latency for everything still running."
        ),
        evidence=[f"{s.name}: {s.summary}" for s in (oom + crash + terminations)[:4]],
        next_steps=[
            "Compare the container memory limit against actual peak RSS for the new revision.",
            "Check whether the restart reason is OOMKilled (exit 137) or a failing health check.",
            "Inspect the last log lines emitted before each termination.",
        ],
    ).finish(bonus)


@rule
def capacity_loss(snap: Snapshot) -> Finding | None:
    targets = _bad(_by_class(snap, "target_health"))
    capacity = _bad(_named(snap, "task capacity", "deployment readiness", "healthy targets"))
    if not (targets or capacity):
        return None
    bonus = 0.1 if targets and capacity else 0.0
    return Builder(
        title="Serving capacity below desired",
        base=0.5,
        rationale=(
            "Fewer instances are serving traffic than the service is configured for, so each "
            "remaining instance absorbs a larger share of load — which is how a partial failure "
            "becomes a total one."
        ),
        evidence=[f"{s.name}: {s.summary}" for s in (targets + capacity)[:4]]
        + [e for s in targets for e in s.evidence[:2]],
        next_steps=[
            "Check the health-check path, timeout and threshold against real response times.",
            "Confirm whether tasks are failing to start or failing their health check after start.",
            "Verify subnet/SG reachability from the load balancer to the new targets.",
        ],
    ).finish(bonus)


@rule
def dependency_failure(snap: Snapshot) -> Finding | None:
    upstream = _log_class(snap, "upstream_5xx")
    timeouts = _log_class(snap, "timeout")
    dns = _log_class(snap, "dns_failure")
    local_pressure = _bad(_named(snap, "cpu", "memory"))
    if not (upstream or timeouts or dns):
        return None
    if local_pressure and not upstream:
        return None  # resource_saturation explains this better
    bonus = 0.1 if upstream else 0.0
    bonus += 0.1 if dns else 0.0
    bonus -= 0.1 if local_pressure else 0.0
    return Builder(
        title="Downstream dependency failure",
        base=0.45,
        rationale=(
            "The service is reporting failures reaching something it depends on, while its own "
            "compute shows no saturation. The fault is likely outside this service's boundary."
        ),
        evidence=[f"{s.name}: {s.summary}" for s in (upstream + timeouts + dns)[:4]],
        next_steps=[
            "Identify the dependency from the failing request paths and check its own health.",
            "Verify client timeout and retry settings — aggressive retries turn a slow dependency "
            "into an outage.",
            "Check for a shared failure domain (same AZ, same NAT gateway, same database).",
        ],
    ).finish(bonus)


@rule
def throttling(snap: Snapshot) -> Finding | None:
    throttled = _log_class(snap, "throttled")
    if not throttled:
        return None
    return Builder(
        title="API or service quota throttling",
        base=0.55,
        rationale=(
            "Requests are being rejected with throttling errors, which means a quota or "
            "provisioned-capacity limit is the binding constraint rather than the service itself."
        ),
        evidence=[f"{s.name}: {s.summary}" for s in throttled[:3]],
        next_steps=[
            "Identify which API or table is throttling and check its quota vs. current rate.",
            "Add jittered backoff where retries are currently tight loops.",
            "Review whether the deployment increased call volume per request.",
        ],
    ).finish()


@rule
def traffic_surge(snap: Snapshot) -> Finding | None:
    requests = _named(snap, "requests")
    latency = _bad(_named(snap, "latency"))
    if not requests:
        return None
    ratio = requests[0].change_ratio()
    if ratio is None or ratio < 2.0:
        return None
    if _deploys(snap):
        return None  # a deploy is the better first hypothesis
    return Builder(
        title="Traffic surge beyond current capacity",
        base=0.5,
        rationale=(
            f"Request volume rose {ratio:.1f}× against baseline with no deployment in the window, "
            "so the trigger is demand rather than change."
        ),
        evidence=[f"{requests[0].name}: {requests[0].summary}"]
        + [f"{s.name}: {s.summary}" for s in latency[:2]],
        next_steps=[
            "Check whether the surge is organic, a retry storm, or a single abusive client.",
            "Compare autoscaling reaction time against the ramp rate.",
            "Consider rate limiting or shedding at the edge for the affected path.",
        ],
    ).finish(0.1 if latency else 0.0)


@rule
def storage_exhaustion(snap: Snapshot) -> Finding | None:
    storage = _bad(_named(snap, "free storage", "freeable memory"))
    disk_logs = _log_class(snap, "disk_full")
    if not (storage or disk_logs):
        return None
    return Builder(
        title="Storage or memory headroom exhausted",
        base=0.6,
        rationale=(
            "Free storage or freeable memory fell below its warning floor. Databases degrade "
            "non-linearly here and can stop accepting writes entirely."
        ),
        evidence=[f"{s.name}: {s.summary}" for s in (storage + disk_logs)[:3]],
        next_steps=[
            "Check growth rate and time-to-full, then extend storage or autoscaling limits.",
            "Look for an unrotated log, a runaway temp table, or a stalled vacuum.",
        ],
    ).finish()


@rule
def replication_lag(snap: Snapshot) -> Finding | None:
    lag = _bad(_named(snap, "replica lag"))
    if not lag:
        return None
    return Builder(
        title="Replication lag",
        base=0.55,
        rationale=(
            "A read replica is behind its source, so reads served from the replica return stale "
            "data and failover would lose the outstanding delta."
        ),
        evidence=[f"{s.name}: {s.summary}" for s in lag[:2]],
        next_steps=[
            "Check write volume on the primary and the replica's apply rate.",
            "Verify whether read traffic was recently shifted onto the replica.",
        ],
    ).finish()


@rule
def tls_or_auth(snap: Snapshot) -> Finding | None:
    tls = _log_class(snap, "tls_failure") + _bad(_by_class(snap, "certificate"))
    auth = _log_class(snap, "auth_failure")
    if not (tls or auth):
        return None
    return Builder(
        title="Credential or certificate failure",
        base=0.6,
        rationale=(
            "Failures match certificate or authorisation signatures. These fail cleanly at an "
            "expiry or rotation boundary, which is why they often start on a time boundary with "
            "no deployment nearby."
        ),
        evidence=[f"{s.name}: {s.summary}" for s in (tls + auth)[:3]],
        next_steps=[
            "Check certificate expiry and the rotation schedule of any secret used on this path.",
            "Confirm the task/pod role still has the permissions the current revision needs.",
        ],
    ).finish()


@rule
def deadlock(snap: Snapshot) -> Finding | None:
    locks = _log_class(snap, "deadlock")
    if not locks:
        return None
    return Builder(
        title="Database contention (deadlocks or lock waits)",
        base=0.5,
        rationale="Lock waits or deadlocks are being reported, so transactions are serialising "
                  "against each other rather than running out of capacity.",
        evidence=[f"{s.name}: {s.summary}" for s in locks[:2]],
        next_steps=[
            "Identify the competing statements and their transaction boundaries.",
            "Check for a long-running migration or batch job overlapping the window.",
        ],
    ).finish()
