"""`sre k8s …` — Kubernetes diagnosis. The API is free; the cost is only latency."""

from __future__ import annotations

from ..models import Severity
from ._shared import add_window_args, emit, exit_code_for, print_cost


def register(sub) -> None:
    parser = sub.add_parser(
        "k8s", help="diagnose deployments, pods, nodes and events",
        description="Read-only Kubernetes diagnosis via the current kube context.",
    )
    parser.add_argument("-n", "--namespace", default="default")
    parser.add_argument("--context", help="kube context (default: current)")
    inner = parser.add_subparsers(dest="action", metavar="<action>")

    diag = inner.add_parser("diagnose", help="diagnose a deployment, e.g. deployment/api")
    diag.add_argument("target", help="'deployment/name' or just 'name'")
    add_window_args(diag)
    diag.set_defaults(handler=run_diagnose)

    pods = inner.add_parser("pods", help="list pod health in a namespace")
    pods.add_argument("--selector", default="", help="label selector")
    pods.set_defaults(handler=run_pods)

    nodes = inner.add_parser("nodes", help="node readiness and pressure")
    nodes.set_defaults(handler=run_nodes)

    events = inner.add_parser("events", help="recent namespace events")
    add_window_args(events)
    events.set_defaults(handler=run_events)


def run_diagnose(ctx, args) -> int:
    from ..collectors import kubernetes as k8s

    name = args.target.split("/", 1)[-1]
    window = ctx.resolve_window(args.start, args.end, args.minutes)
    con = ctx.console
    result = k8s.collect_deployment(ctx, name, args.namespace, window, args.context)

    con.title("KUBERNETES DIAGNOSIS", f"{args.namespace}/{name} · {window}")
    for resource in result["resources"]:
        for key, value in resource.attributes.items():
            con.kv(key.replace("_", " "), value)

    con.section("Signals")
    for signal in sorted(result["signals"], key=lambda s: -s.severity.rank):
        con.bullet(f"{signal.name}: {signal.summary}", signal.severity)
        for hint in signal.evidence:
            con.out(con.style(f"    ↳ {hint}", "dim"))

    if result["pods"]:
        con.section("Pods")
        con.table(
            ["pod", "phase", "ready", "restarts", "node", "waiting"],
            [
                [p["name"][:40], p["phase"], f"{p['ready']}/{p['containers']}", p["restarts"],
                 (p["node"] or "")[:24], ",".join(p["waiting"])]
                for p in result["pods"][:15]
            ],
        )

    if result["events"]:
        con.section("Events")
        for event in result["events"][-12:]:
            con.bullet(f"{event.at.strftime('%H:%M:%S')}  {event.title} — {event.detail}",
                       event.severity)

    worst = max((s.severity for s in result["signals"]), key=lambda s: s.rank,
                default=Severity.OK)
    from ..models import _encode

    emit(ctx, args, {"deployment": f"{args.namespace}/{name}", "pods": result["pods"],
                     "signals": _encode(result["signals"]),
                     "events": _encode(result["events"])})
    print_cost(ctx)
    return exit_code_for(worst)


def run_pods(ctx, args) -> int:
    from ..collectors import kubernetes as k8s

    result = k8s.collect_pods(ctx, args.namespace, args.selector)
    con = ctx.console
    con.title("PODS", args.namespace)
    con.table(
        ["pod", "phase", "ready", "restarts", "node", "waiting"],
        [
            [p["name"][:40], p["phase"], f"{p['ready']}/{p['containers']}", p["restarts"],
             (p["node"] or "")[:24], ",".join(p["waiting"])]
            for p in result["pods"]
        ],
    )
    for signal in result["signals"]:
        con.out()
        con.bullet(f"{signal.name}: {signal.summary}", signal.severity)
    worst = max((s.severity for s in result["signals"]), key=lambda s: s.rank,
                default=Severity.OK)
    emit(ctx, args, {"namespace": args.namespace, "pods": result["pods"]})
    print_cost(ctx)
    return exit_code_for(worst)


def run_nodes(ctx, args) -> int:
    from ..collectors import kubernetes as k8s

    result = k8s.collect_nodes(ctx)
    con = ctx.console
    con.title("NODES")
    con.table(
        ["node", "ready", "schedulable", "pressure", "kubelet", "cpu", "memory"],
        [
            [n["name"][:32], "yes" if n["ready"] else "NO",
             "yes" if n["schedulable"] else "cordoned", ",".join(n["pressure"]) or "-",
             n["kubelet"], n["allocatable_cpu"], n["allocatable_mem"]]
            for n in result["nodes"]
        ],
    )
    for signal in result["signals"]:
        con.out()
        con.bullet(f"{signal.name}: {signal.summary}", signal.severity)
    worst = max((s.severity for s in result["signals"]), key=lambda s: s.rank,
                default=Severity.OK)
    emit(ctx, args, {"nodes": result["nodes"]})
    print_cost(ctx)
    return exit_code_for(worst)


def run_events(ctx, args) -> int:
    from ..collectors import kubernetes as k8s

    window = ctx.resolve_window(args.start, args.end, args.minutes)
    events = k8s.collect_events(ctx, args.namespace, window)
    con = ctx.console
    con.title("EVENTS", f"{args.namespace} · {window}")
    for event in events:
        con.bullet(f"{event.at.strftime('%H:%M:%S')}  {event.title} — {event.detail}",
                   event.severity)
    if not events:
        con.out(con.style("  (no events in window)", "dim"))
    from ..models import _encode

    emit(ctx, args, {"namespace": args.namespace, "events": _encode(events)})
    print_cost(ctx)
    worst = max((e.severity for e in events), key=lambda s: s.rank, default=Severity.OK)
    return exit_code_for(worst)
