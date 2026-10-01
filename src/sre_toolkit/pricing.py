"""Price table for every metered operation the toolkit can perform.

Numbers are us-east-1 list prices and are *estimates for guard-rails*, not a
billing source of truth. Override anything with ~/.config/sre-toolkit/pricing.json
(same shape) when you have negotiated or regional pricing.

Sources:
  CloudWatch   https://aws.amazon.com/cloudwatch/pricing/
  CloudTrail   https://aws.amazon.com/cloudtrail/pricing/
  Bedrock      https://aws.amazon.com/bedrock/pricing/
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# --- AWS API operations -------------------------------------------------
# Unit meanings:
#   per_1k_metrics   charged per 1,000 metrics requested (GetMetricData)
#   per_1k_requests  charged per 1,000 API requests
#   per_gb_scanned   charged per GB of log data scanned (Logs Insights)
#   free             no charge for this API call
API_PRICES: dict[str, dict] = {
    "cloudwatch:GetMetricData": {"unit": "per_1k_metrics", "usd": 0.01},
    "cloudwatch:GetMetricStatistics": {"unit": "per_1k_requests", "usd": 0.01},
    "cloudwatch:ListMetrics": {"unit": "per_1k_requests", "usd": 0.01},
    "logs:StartQuery": {"unit": "per_gb_scanned", "usd": 0.005},
    # FilterLogEvents / DescribeLogStreams are not charged per GB — this is why
    # the log collector prefers them and treats Insights as opt-in (--deep).
    "logs:FilterLogEvents": {"unit": "free", "usd": 0.0},
    "logs:DescribeLogGroups": {"unit": "free", "usd": 0.0},
    "logs:GetQueryResults": {"unit": "free", "usd": 0.0},
    "cloudtrail:LookupEvents": {"unit": "free", "usd": 0.0},
    "ecs:DescribeServices": {"unit": "free", "usd": 0.0},
    "ecs:DescribeTasks": {"unit": "free", "usd": 0.0},
    "ecs:ListTasks": {"unit": "free", "usd": 0.0},
    "ecs:DescribeTaskDefinition": {"unit": "free", "usd": 0.0},
    "rds:DescribeDBInstances": {"unit": "free", "usd": 0.0},
    "rds:DescribeEvents": {"unit": "free", "usd": 0.0},
    "rds:DescribeDBSnapshots": {"unit": "free", "usd": 0.0},
    "elbv2:DescribeTargetHealth": {"unit": "free", "usd": 0.0},
    "elbv2:DescribeLoadBalancers": {"unit": "free", "usd": 0.0},
    "elbv2:DescribeTargetGroups": {"unit": "free", "usd": 0.0},
    "ec2:DescribeInstances": {"unit": "free", "usd": 0.0},
    "ec2:DescribeInstanceStatus": {"unit": "free", "usd": 0.0},
    "s3:GetBucketVersioning": {"unit": "free", "usd": 0.0},
    "s3:GetBucketReplication": {"unit": "free", "usd": 0.0},
    "sts:GetCallerIdentity": {"unit": "free", "usd": 0.0},
    "k8s:api": {"unit": "free", "usd": 0.0},
    "net:probe": {"unit": "free", "usd": 0.0},
}

# --- Bedrock models (USD per 1M tokens) ---------------------------------
# Cheapest-capable first. `default` is what --ai uses unless you pass --model.
BEDROCK_MODELS: dict[str, dict] = {
    "anthropic.claude-haiku-4-5": {"input": 1.00, "output": 5.00, "context": 200_000},
    "anthropic.claude-sonnet-5": {"input": 3.00, "output": 15.00, "context": 1_000_000},
    "anthropic.claude-opus-5": {"input": 5.00, "output": 25.00, "context": 1_000_000},
}
DEFAULT_MODEL = "anthropic.claude-haiku-4-5"

_LOADED: dict | None = None


def _overrides() -> dict:
    global _LOADED
    if _LOADED is None:
        path = Path(
            os.environ.get("SRE_PRICING_FILE")
            or Path.home() / ".config" / "sre-toolkit" / "pricing.json"
        )
        try:
            _LOADED = json.loads(path.read_text())
        except (OSError, ValueError):
            _LOADED = {}
    return _LOADED


def api_price(operation: str) -> dict:
    over = _overrides().get("api", {})
    if operation in over:
        return over[operation]
    return API_PRICES.get(operation, {"unit": "unknown", "usd": 0.0})


def model_price(model_id: str) -> dict:
    over = _overrides().get("bedrock", {})
    if model_id in over:
        return over[model_id]
    if model_id in BEDROCK_MODELS:
        return BEDROCK_MODELS[model_id]
    # Unknown model: price it like the most expensive one we know so the
    # budget guard errs on the side of not spending money.
    return max(BEDROCK_MODELS.values(), key=lambda m: m["output"])


def cost_of(operation: str, quantity: float) -> float:
    """quantity is interpreted per the operation's unit (metrics, requests, GB)."""
    price = api_price(operation)
    unit, usd = price.get("unit"), float(price.get("usd", 0.0))
    if unit in ("per_1k_metrics", "per_1k_requests"):
        return usd * quantity / 1000.0
    if unit == "per_gb_scanned":
        return usd * quantity
    return 0.0


def token_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    price = model_price(model_id)
    return (input_tokens / 1e6) * price["input"] + (output_tokens / 1e6) * price["output"]
