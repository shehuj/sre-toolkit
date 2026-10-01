"""ALB / target group collector. DescribeTargetHealth is free and tells you
immediately whether an incident is "app is broken" or "nothing is registered"."""

from __future__ import annotations

from typing import Any

from ..models import Resource, Severity, Signal, SignalKind

_UNHEALTHY_HINTS = {
    "Target.Timeout": "health check timed out — app is up but too slow to answer",
    "Target.FailedHealthChecks": "health check returned a non-matching status",
    "Target.ResponseCodeMismatch": "health check path returned the wrong status code",
    "Target.NotRegistered": "target is no longer registered with the group",
    "Target.DeregistrationInProgress": "target is draining (deploy in progress)",
    "Elb.InternalError": "load balancer internal error",
    "Target.NotInUse": "target group is not attached to an active listener",
}


def dimensions(target_group_arn: str, load_balancer_arn: str) -> dict[str, str]:
    """CloudWatch wants the short forms: targetgroup/x/id and app/name/id."""
    return {
        "TargetGroup": "/".join(target_group_arn.split(":")[-1].split("/")[-3:])
        if "targetgroup" in target_group_arn else target_group_arn,
        "LoadBalancer": "/".join(load_balancer_arn.split(":")[-1].split("/")[1:])
        if load_balancer_arn else "",
    }


def describe_target_group(ctx, target_group_arn: str) -> dict[str, Any]:
    payload = ctx.aws.call(
        "elbv2", "describe_target_groups", "elbv2:DescribeTargetGroups",
        detail=target_group_arn.split("/")[-2] if "/" in target_group_arn else target_group_arn,
        TargetGroupArns=[target_group_arn],
    )
    groups = payload.get("TargetGroups", [])
    return groups[0] if groups else {}


def collect(ctx, target_group_arn: str) -> dict[str, Any]:
    group = describe_target_group(ctx, target_group_arn)
    out: dict[str, Any] = {"target_group": group, "resources": [], "signals": [],
                           "load_balancer_arn": "", "dimensions": {}}
    if not group:
        return out
    lb_arns = group.get("LoadBalancerArns", [])
    out["load_balancer_arn"] = lb_arns[0] if lb_arns else ""
    out["dimensions"] = dimensions(target_group_arn, out["load_balancer_arn"])

    health = ctx.aws.call(
        "elbv2", "describe_target_health", "elbv2:DescribeTargetHealth",
        detail=group.get("TargetGroupName", ""), TargetGroupArn=target_group_arn,
    ).get("TargetHealthDescriptions", [])

    states: dict[str, int] = {}
    reasons: dict[str, str] = {}
    for entry in health:
        state = entry.get("TargetHealth", {}).get("State", "unknown")
        states[state] = states.get(state, 0) + 1
        reason = entry.get("TargetHealth", {}).get("Reason")
        if reason:
            reasons[reason] = entry.get("TargetHealth", {}).get("Description", "")

    out["resources"].append(
        Resource(
            kind="elbv2:targetgroup",
            identifier=group.get("TargetGroupName", target_group_arn),
            attributes={
                "protocol": group.get("Protocol"), "port": group.get("Port"),
                "health_check_path": group.get("HealthCheckPath"),
                "health_check_interval_s": group.get("HealthCheckIntervalSeconds"),
                "health_check_timeout_s": group.get("HealthCheckTimeoutSeconds"),
                "healthy_threshold": group.get("HealthyThresholdCount"),
                "unhealthy_threshold": group.get("UnhealthyThresholdCount"),
                "target_states": states,
            },
        )
    )

    total = sum(states.values())
    healthy = states.get("healthy", 0)
    if total:
        if healthy == 0:
            severity = Severity.CRIT
        elif healthy < total:
            severity = Severity.WARN
        else:
            severity = Severity.OK
        hints = [
            f"{reason}: {_UNHEALTHY_HINTS.get(reason, desc or 'see ELB docs')}"
            for reason, desc in reasons.items()
        ]
        out["signals"].append(
            Signal(
                name="ALB target health",
                kind=SignalKind.HEALTH,
                source=f"elbv2/{group.get('TargetGroupName', '')}",
                severity=severity,
                summary=f"{healthy}/{total} targets healthy"
                        + (f" — {list(reasons)[0]}" if reasons else ""),
                baseline=float(total), peak=float(healthy), unit="Count",
                evidence=hints,
                tags={"class": "target_health"},
            )
        )
    return out


def find_target_groups(ctx, name_or_arn: str) -> list[str]:
    """Resolve an ALB name to its target group ARNs (free)."""
    if name_or_arn.startswith("arn:") and "targetgroup" in name_or_arn:
        return [name_or_arn]
    if name_or_arn.startswith("arn:"):
        kwargs = {"LoadBalancerArn": name_or_arn}
    else:
        lbs = ctx.aws.call(
            "elbv2", "describe_load_balancers", "elbv2:DescribeLoadBalancers",
            detail=name_or_arn, Names=[name_or_arn],
        ).get("LoadBalancers", [])
        if not lbs:
            return []
        kwargs = {"LoadBalancerArn": lbs[0]["LoadBalancerArn"]}
    groups = ctx.aws.call(
        "elbv2", "describe_target_groups", "elbv2:DescribeTargetGroups",
        detail=name_or_arn, **kwargs,
    ).get("TargetGroups", [])
    return [g["TargetGroupArn"] for g in groups]
