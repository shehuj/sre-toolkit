"""Kubernetes collector.

The Kubernetes API is free to query — the only budget here is your own patience,
so the collector is aggressive about field selectors and small page sizes rather
than about call count.
"""

from __future__ import annotations

from datetime import timezone
from typing import Any

from ..errors import CollectorError, MissingDependency
from ..models import Resource, Severity, Signal, SignalKind, TimelineEvent, Window

UTC = timezone.utc

BAD_PHASES = {"Failed", "Unknown", "Pending"}
BAD_WAITING_REASONS = {
    "CrashLoopBackOff": Severity.CRIT,
    "ImagePullBackOff": Severity.CRIT,
    "ErrImagePull": Severity.CRIT,
    "CreateContainerConfigError": Severity.CRIT,
    "CreateContainerError": Severity.CRIT,
    "ContainerCreating": Severity.INFO,
    "PodInitializing": Severity.INFO,
}


def _api(ctx, context: str | None = None):
    try:
        from kubernetes import client, config  # type: ignore
    except ImportError as exc:
        raise MissingDependency("kubernetes", "k8s") from exc
    try:
        config.load_kube_config(context=context)
    except Exception:  # noqa: BLE001 - in-cluster is the normal fallback
        try:
            config.load_incluster_config()
        except Exception as exc:  # noqa: BLE001
            raise CollectorError(f"no usable kubeconfig or in-cluster config: {exc}") from exc
    ctx.ledger.charge("k8s:api", 1, detail="client init")
    return client.CoreV1Api(), client.AppsV1Api()


def collect_deployment(
    ctx, name: str, namespace: str = "default", window: Window | None = None,
    context: str | None = None,
) -> dict[str, Any]:
    core, apps = _api(ctx, context)
    out: dict[str, Any] = {"resources": [], "signals": [], "events": [], "pods": []}

    dep = apps.read_namespaced_deployment(name=name, namespace=namespace)
    ctx.ledger.charge("k8s:api", 1, detail=f"read deployment {namespace}/{name}")
    status = dep.status
    spec_replicas = dep.spec.replicas or 0
    ready = status.ready_replicas or 0
    out["resources"].append(
        Resource(
            kind="k8s:deployment",
            identifier=f"{namespace}/{name}",
            attributes={
                "replicas_desired": spec_replicas,
                "replicas_ready": ready,
                "replicas_updated": status.updated_replicas or 0,
                "replicas_unavailable": status.unavailable_replicas or 0,
                "generation": dep.metadata.generation,
                "observed_generation": status.observed_generation,
                "strategy": dep.spec.strategy.type if dep.spec.strategy else None,
                "images": [c.image for c in dep.spec.template.spec.containers],
            },
        )
    )
    if spec_replicas:
        severity = Severity.OK
        if ready < spec_replicas:
            severity = Severity.CRIT if ready == 0 or ready < spec_replicas * 0.7 else Severity.WARN
        out["signals"].append(
            Signal(
                name="Deployment readiness",
                kind=SignalKind.RESOURCE,
                source=f"k8s/{namespace}/{name}",
                severity=severity,
                summary=f"{ready}/{spec_replicas} replicas ready",
                baseline=float(spec_replicas), peak=float(ready), unit="Count",
            )
        )

    for condition in status.conditions or []:
        if condition.status != "True" and condition.type in ("Available", "Progressing"):
            out["signals"].append(
                Signal(
                    name=f"Deployment condition {condition.type}",
                    kind=SignalKind.RESOURCE,
                    source=f"k8s/{namespace}/{name}",
                    severity=Severity.CRIT,
                    summary=f"{condition.reason}: {condition.message}"[:200],
                )
            )

    selector = ",".join(f"{k}={v}" for k, v in (dep.spec.selector.match_labels or {}).items())
    pod_info = collect_pods(ctx, namespace, selector, core=core)
    out["pods"] = pod_info["pods"]
    out["signals"].extend(pod_info["signals"])
    out["events"].extend(
        collect_events(ctx, namespace, window, core=core,
                       involved_prefix=name)
    )
    return out


def collect_pods(
    ctx, namespace: str = "default", selector: str = "", core=None, limit: int = 100
) -> dict[str, Any]:
    if core is None:
        core, _ = _api(ctx)
    pods = core.list_namespaced_pod(
        namespace=namespace, label_selector=selector or None, limit=limit
    )
    ctx.ledger.charge("k8s:api", 1, detail=f"list pods {namespace}")

    rows, signals = [], []
    restart_total, bad = 0, []
    for pod in pods.items:
        statuses = pod.status.container_statuses or []
        restarts = sum(s.restart_count or 0 for s in statuses)
        restart_total += restarts
        waiting = [
            s.state.waiting.reason
            for s in statuses
            if s.state and s.state.waiting and s.state.waiting.reason
        ]
        terminated = [
            f"{s.state.terminated.reason}({s.state.terminated.exit_code})"
            for s in statuses
            if s.state and s.state.terminated and s.state.terminated.reason
        ]
        row = {
            "name": pod.metadata.name,
            "phase": pod.status.phase,
            "node": pod.spec.node_name,
            "restarts": restarts,
            "waiting": waiting,
            "terminated": terminated,
            "ready": sum(1 for s in statuses if s.ready),
            "containers": len(statuses),
            "started_at": pod.status.start_time.isoformat() if pod.status.start_time else None,
        }
        rows.append(row)
        if pod.status.phase in BAD_PHASES or waiting or restarts >= 3:
            bad.append(row)

    if bad:
        worst = Severity.INFO
        reasons: list[str] = []
        for row in bad:
            for reason in row["waiting"]:
                sev = BAD_WAITING_REASONS.get(reason, Severity.WARN)
                worst = max(worst, sev, key=lambda s: s.rank)
                reasons.append(reason)
            if row["restarts"] >= 3:
                worst = max(worst, Severity.CRIT, key=lambda s: s.rank)
                reasons.append(f"{row['name']} restarted {row['restarts']}×")
            if row["phase"] in BAD_PHASES:
                worst = max(worst, Severity.WARN, key=lambda s: s.rank)
        signals.append(
            Signal(
                name="Pod health",
                kind=SignalKind.RESOURCE,
                source=f"k8s/{namespace}",
                severity=worst,
                summary=f"{len(bad)}/{len(rows)} pods unhealthy; {restart_total} restarts total",
                peak=float(len(bad)), unit="Count",
                evidence=sorted(set(reasons))[:5],
                tags={"class": "crash_loop"} if "CrashLoopBackOff" in reasons else {},
            )
        )
    return {"pods": rows, "signals": signals}


def collect_events(
    ctx, namespace: str = "default", window: Window | None = None, core=None,
    involved_prefix: str = "", limit: int = 100,
) -> list[TimelineEvent]:
    if core is None:
        core, _ = _api(ctx)
    raw = core.list_namespaced_event(namespace=namespace, limit=limit)
    ctx.ledger.charge("k8s:api", 1, detail=f"list events {namespace}")
    out = []
    for item in raw.items:
        at = item.last_timestamp or item.event_time or item.first_timestamp
        if at is None:
            continue
        at = at if at.tzinfo else at.replace(tzinfo=UTC)
        if window and not (window.start <= at <= window.end):
            continue
        name = (item.involved_object.name or "") if item.involved_object else ""
        if involved_prefix and not name.startswith(involved_prefix):
            continue
        severity = Severity.WARN if item.type == "Warning" else Severity.INFO
        if item.reason in ("Failed", "FailedScheduling", "BackOff", "Unhealthy", "OOMKilling"):
            severity = Severity.CRIT
        out.append(
            TimelineEvent(
                at=at,
                source=f"k8s/{item.involved_object.kind if item.involved_object else 'event'}",
                title=f"{item.reason}: {name}",
                detail=(item.message or "")[:180],
                severity=severity,
            )
        )
    return sorted(out, key=lambda e: e.at)


def collect_nodes(ctx, core=None) -> dict[str, Any]:
    if core is None:
        core, _ = _api(ctx)
    nodes = core.list_node()
    ctx.ledger.charge("k8s:api", 1, detail="list nodes")
    rows, signals = [], []
    not_ready = []
    for node in nodes.items:
        conditions = {c.type: c.status for c in (node.status.conditions or [])}
        pressure = [k for k, v in conditions.items() if k.endswith("Pressure") and v == "True"]
        row = {
            "name": node.metadata.name,
            "ready": conditions.get("Ready") == "True",
            "pressure": pressure,
            "schedulable": not (node.spec.unschedulable or False),
            "kubelet": node.status.node_info.kubelet_version if node.status.node_info else "",
            "allocatable_cpu": (node.status.allocatable or {}).get("cpu"),
            "allocatable_mem": (node.status.allocatable or {}).get("memory"),
        }
        rows.append(row)
        if not row["ready"] or pressure or not row["schedulable"]:
            not_ready.append(row)
    if not_ready:
        signals.append(
            Signal(
                name="Node health",
                kind=SignalKind.RESOURCE,
                source="k8s/nodes",
                severity=Severity.CRIT if any(not r["ready"] for r in not_ready) else Severity.WARN,
                summary=f"{len(not_ready)}/{len(rows)} nodes not ready, cordoned or under pressure",
                peak=float(len(not_ready)), unit="Count",
                evidence=[f"{r['name']}: ready={r['ready']} pressure={r['pressure']}"
                          for r in not_ready[:5]],
            )
        )
    return {"nodes": rows, "signals": signals}
