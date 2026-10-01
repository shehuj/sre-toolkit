"""CloudWatch metrics collection, batched for cost.

Cost model: GetMetricData is billed per *metric requested* ($0.01 / 1,000), not
per API call. So the cheapest way to fetch 20 metrics is one request containing
20 queries — which is exactly what `fetch()` does. Two further savings:

  * `pick_period()` scales the period to the window so a 30-minute incident is
    not fetched at 1-second resolution it cannot use.
  * The request spans the incident window *plus an equal baseline window before
    it*, so "before vs. during" comes out of a single billed metric instead of
    two separate calls.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..models import Severity, Signal, SignalKind, Window

# CloudWatch only stores these resolutions long-term; anything else is rounded up.
ALLOWED_PERIODS = (60, 300, 900, 3600, 21600, 86400)
TARGET_POINTS = 60  # enough to see a shape in a sparkline, few enough to stay cheap


@dataclass
class MetricSpec:
    key: str
    namespace: str
    metric: str
    dimensions: dict[str, str]
    stat: str = "Average"
    unit: str = ""
    label: str = ""
    # Thresholds are evaluated against the *during* window.
    warn_above: float | None = None
    crit_above: float | None = None
    warn_below: float | None = None
    crit_below: float | None = None
    # A multiplicative jump vs. baseline that is interesting even below warn_above.
    spike_ratio: float | None = 2.0

    @property
    def display(self) -> str:
        return self.label or f"{self.metric}"


@dataclass
class MetricSeries:
    spec: MetricSpec
    timestamps: list[datetime] = field(default_factory=list)
    values: list[float] = field(default_factory=list)

    def split(self, at: datetime) -> tuple[list[float], list[float]]:
        before = [v for t, v in zip(self.timestamps, self.values, strict=False) if t < at]
        during = [v for t, v in zip(self.timestamps, self.values, strict=False) if t >= at]
        return before, during


def pick_period(window_seconds: int, points: int = TARGET_POINTS) -> int:
    raw = max(60, window_seconds // max(1, points))
    for allowed in ALLOWED_PERIODS:
        if raw <= allowed:
            return allowed
    return ALLOWED_PERIODS[-1]


def fetch(
    ctx,
    window: Window,
    specs: Iterable[MetricSpec],
    include_baseline: bool = True,
    period: int | None = None,
) -> list[MetricSeries]:
    """One batched GetMetricData covering baseline + incident window.

    `include_baseline=False` fetches only the window itself — used by long-range
    callers like SLO calculation, where doubling a 30-day window would double the
    returned data for no analytical gain.
    """
    specs = list(specs)
    if not specs:
        return []
    period = period or pick_period(window.seconds)
    baseline_start = (
        window.start - timedelta(seconds=window.seconds) if include_baseline else window.start
    )

    queries = [
        {
            "Id": f"m{i}",
            "MetricStat": {
                "Metric": {
                    "Namespace": spec.namespace,
                    "MetricName": spec.metric,
                    "Dimensions": [
                        {"Name": k, "Value": v} for k, v in sorted(spec.dimensions.items())
                    ],
                },
                "Period": period,
                "Stat": spec.stat,
            },
            "ReturnData": True,
        }
        for i, spec in enumerate(specs)
    ]

    result = ctx.aws.call(
        "cloudwatch",
        "get_metric_data",
        "cloudwatch:GetMetricData",
        quantity=len(queries),  # billed per metric requested
        detail=f"{len(queries)} metrics @ {period}s",
        MetricDataQueries=queries,
        StartTime=baseline_start,
        EndTime=window.end,
        ScanBy="TimestampAscending",
    )

    by_id = {r["Id"]: r for r in result.get("MetricDataResults", [])}
    out = []
    for i, spec in enumerate(specs):
        raw = by_id.get(f"m{i}", {})
        out.append(
            MetricSeries(
                spec=spec,
                timestamps=list(raw.get("Timestamps", [])),
                values=[float(v) for v in raw.get("Values", [])],
            )
        )
    return out


def to_signals(series_list: list[MetricSeries], window: Window, source: str) -> list[Signal]:
    signals = []
    for series in series_list:
        signal = to_signal(series, window, source)
        if signal is not None:
            signals.append(signal)
    return signals


def to_signal(series: MetricSeries, window: Window, source: str) -> Signal | None:
    spec = series.spec
    before, during = series.split(window.start)
    if not during:
        return None

    baseline = statistics.median(before) if before else None
    peak = max(during) if spec.warn_below is None else min(during)
    severity, summary = _classify(spec, baseline, peak, during)
    pairs = [
        (t, v) for t, v in zip(series.timestamps, series.values, strict=False) if t >= window.start
    ]
    first_seen = _onset(spec, series, window)

    delta = None
    if baseline not in (None, 0):
        delta = (peak - baseline) / abs(baseline) * 100

    return Signal(
        name=spec.display,
        kind=SignalKind.METRIC,
        source=source,
        severity=severity,
        summary=summary,
        baseline=baseline,
        peak=peak,
        unit=spec.unit,
        delta_pct=delta,
        first_seen=first_seen,
        series=pairs,
        tags={"namespace": spec.namespace, "metric": spec.metric, "stat": spec.stat},
    )


def _classify(
    spec: MetricSpec, baseline: float | None, peak: float, during: list[float]
) -> tuple[Severity, str]:
    unit = spec.unit
    from ..render import fmt_num

    shown = fmt_num(peak, unit)
    base_shown = fmt_num(baseline, unit) if baseline is not None else None

    if spec.crit_above is not None and peak >= spec.crit_above:
        return Severity.CRIT, f"peaked at {shown} (critical ≥ {fmt_num(spec.crit_above, unit)})"
    if spec.crit_below is not None and peak <= spec.crit_below:
        return Severity.CRIT, f"dropped to {shown} (critical ≤ {fmt_num(spec.crit_below, unit)})"
    if spec.warn_above is not None and peak >= spec.warn_above:
        return Severity.WARN, f"peaked at {shown} (warn ≥ {fmt_num(spec.warn_above, unit)})"
    if spec.warn_below is not None and peak <= spec.warn_below:
        return Severity.WARN, f"dropped to {shown} (warn ≤ {fmt_num(spec.warn_below, unit)})"
    if spec.spike_ratio and baseline not in (None, 0) and peak >= baseline * spec.spike_ratio:
        return Severity.WARN, f"rose from {base_shown} to {shown}"
    if baseline is not None and base_shown != shown:
        return Severity.INFO, f"{base_shown} → {shown}"
    return Severity.INFO, f"steady around {shown}"


def _onset(spec: MetricSpec, series: MetricSeries, window: Window) -> datetime | None:
    """First timestamp inside the window where the metric left its baseline band."""
    before, _ = series.split(window.start)
    if not before:
        return None
    base = statistics.median(before)
    spread = statistics.pstdev(before) if len(before) > 1 else 0.0
    threshold = base + max(spread * 3, abs(base) * 0.5, 1e-9)
    for ts, value in zip(series.timestamps, series.values, strict=False):
        if ts >= window.start and value >= threshold:
            return ts
    return None


# --- Reusable metric sets ----------------------------------------------
def alb_specs(target_group: str, load_balancer: str) -> list[MetricSpec]:
    dims = {"TargetGroup": target_group, "LoadBalancer": load_balancer}
    ns = "AWS/ApplicationELB"
    return [
        MetricSpec("requests", ns, "RequestCount", dims, "Sum", "Count", "ALB requests",
                   spike_ratio=3.0),
        MetricSpec("5xx", ns, "HTTPCode_Target_5XX_Count", dims, "Sum", "Count",
                   "HTTP 5xx (target)", warn_above=1, crit_above=50),
        MetricSpec("4xx", ns, "HTTPCode_Target_4XX_Count", dims, "Sum", "Count",
                   "HTTP 4xx (target)", spike_ratio=4.0),
        MetricSpec("elb_5xx", ns, "HTTPCode_ELB_5XX_Count", dims, "Sum", "Count",
                   "HTTP 5xx (ELB)", warn_above=1, crit_above=20),
        MetricSpec("p95", ns, "TargetResponseTime", dims, "p95", "Seconds",
                   "p95 latency", warn_above=1.0, crit_above=3.0),
        MetricSpec("healthy", ns, "HealthyHostCount", {"TargetGroup": target_group,
                   "LoadBalancer": load_balancer}, "Minimum", "Count",
                   "Healthy targets", warn_below=2, crit_below=1, spike_ratio=None),
        MetricSpec("unhealthy", ns, "UnHealthyHostCount", dims, "Maximum", "Count",
                   "Unhealthy targets", warn_above=1, crit_above=3),
        MetricSpec("conn_err", ns, "TargetConnectionErrorCount", dims, "Sum", "Count",
                   "Target connection errors", warn_above=1, crit_above=25),
    ]


def ecs_specs(cluster: str, service: str) -> list[MetricSpec]:
    dims = {"ClusterName": cluster, "ServiceName": service}
    return [
        MetricSpec("cpu", "AWS/ECS", "CPUUtilization", dims, "Average", "Percent",
                   "ECS CPU", warn_above=80, crit_above=95),
        MetricSpec("mem", "AWS/ECS", "MemoryUtilization", dims, "Average", "Percent",
                   "ECS memory", warn_above=80, crit_above=95),
        MetricSpec("cpu_max", "AWS/ECS", "CPUUtilization", dims, "Maximum", "Percent",
                   "ECS CPU (max task)", warn_above=90, crit_above=99),
    ]


def rds_specs(instance: str) -> list[MetricSpec]:
    dims = {"DBInstanceIdentifier": instance}
    ns = "AWS/RDS"
    return [
        MetricSpec("cpu", ns, "CPUUtilization", dims, "Average", "Percent", "RDS CPU",
                   warn_above=80, crit_above=95),
        MetricSpec("conns", ns, "DatabaseConnections", dims, "Maximum", "Count",
                   "DB connections", spike_ratio=1.5),
        MetricSpec("read_lat", ns, "ReadLatency", dims, "Average", "Seconds",
                   "Read latency", warn_above=0.02, crit_above=0.1),
        MetricSpec("write_lat", ns, "WriteLatency", dims, "Average", "Seconds",
                   "Write latency", warn_above=0.02, crit_above=0.1),
        MetricSpec("queue", ns, "DiskQueueDepth", dims, "Average", "Count",
                   "Disk queue depth", warn_above=5, crit_above=20),
        MetricSpec("mem", ns, "FreeableMemory", dims, "Minimum", "Bytes",
                   "Freeable memory", warn_below=512 * 1024**2, crit_below=128 * 1024**2,
                   spike_ratio=None),
        MetricSpec("storage", ns, "FreeStorageSpace", dims, "Minimum", "Bytes",
                   "Free storage", warn_below=10 * 1024**3, crit_below=2 * 1024**3,
                   spike_ratio=None),
        MetricSpec("replica_lag", ns, "ReplicaLag", dims, "Maximum", "Seconds",
                   "Replica lag", warn_above=30, crit_above=300),
    ]
