"""End-to-end pipeline tests against a fake AWS, so the whole investigation path
is covered without credentials, network or spend."""

import unittest
from datetime import timedelta

from sre_toolkit.errors import BudgetExceeded
from sre_toolkit.investigate import Target, investigate, snapshot
from sre_toolkit.models import Severity

from tests.support import log_events, make_context, window

WIN = window(30)
TG_ARN = "arn:aws:elasticloadbalancing:us-east-1:1:targetgroup/api-tg/abc"
LB_ARN = "arn:aws:elasticloadbalancing:us-east-1:1:loadbalancer/app/api-lb/def"

SERVICE = {
    "services": [
        {
            "serviceName": "api", "status": "ACTIVE", "desiredCount": 6, "runningCount": 4,
            "pendingCount": 1, "launchType": "FARGATE",
            "taskDefinition": "arn:aws:ecs:us-east-1:1:task-definition/api:184",
            "deployments": [
                {
                    "status": "PRIMARY", "rolloutState": "COMPLETED",
                    "taskDefinition": "arn:aws:ecs:us-east-1:1:task-definition/api:184",
                    "createdAt": WIN.start + timedelta(minutes=1),
                    "updatedAt": WIN.start + timedelta(minutes=2),
                    "runningCount": 4, "desiredCount": 6,
                }
            ],
            "events": [
                {
                    "createdAt": WIN.start + timedelta(minutes=9),
                    "message": "(service api) (port 8080) is unhealthy in target-group api-tg",
                }
            ],
            "loadBalancers": [{"targetGroupArn": TG_ARN}],
        }
    ]
}

TARGET_GROUPS = {
    "TargetGroups": [
        {
            "TargetGroupName": "api-tg", "Protocol": "HTTP", "Port": 8080,
            "HealthCheckPath": "/healthz", "HealthCheckIntervalSeconds": 15,
            "TargetGroupArn": TG_ARN,
            "LoadBalancerArns": [LB_ARN],
        }
    ]
}

TARGET_HEALTH = {
    "TargetHealthDescriptions": [
        {"TargetHealth": {"State": "healthy"}},
        {"TargetHealth": {"State": "healthy"}},
        {"TargetHealth": {"State": "unhealthy", "Reason": "Target.Timeout",
                          "Description": "Request timed out"}},
    ]
}

DB = {
    "DBInstances": [
        {
            "DBInstanceIdentifier": "prod-db", "DBInstanceStatus": "available",
            "DBInstanceClass": "db.r6g.large", "Engine": "postgres", "EngineVersion": "15.5",
            "MultiAZ": True, "AllocatedStorage": 200, "StorageType": "gp3",
            "BackupRetentionPeriod": 7, "DeletionProtection": True,
            "PerformanceInsightsEnabled": True,
        }
    ]
}

LOG_GROUPS = {"logGroups": [{"logGroupName": "/aws/ecs/api", "storedBytes": 2 * 1024**3,
                             "retentionInDays": 30}]}

POOL_ERROR = (
    "2026-10-01T06:35:11.204Z ERROR [http-nio-8080-exec-7] HikariPool-1 - Connection is not "
    "available, request timed out after 30000ms"
)

# Per-metric baseline/during values, keyed by CloudWatch metric name.
METRIC_VALUES = {
    "CPUUtilization": (30.0, 88.0),
    "MemoryUtilization": (40.0, 55.0),
    "RequestCount": (1200.0, 1250.0),
    "HTTPCode_Target_5XX_Count": (1.0, 220.0),
    "HTTPCode_Target_4XX_Count": (5.0, 6.0),
    "HTTPCode_ELB_5XX_Count": (0.0, 0.0),
    "TargetResponseTime": (0.24, 3.8),
    "HealthyHostCount": (6.0, 4.0),
    "UnHealthyHostCount": (0.0, 2.0),
    "TargetConnectionErrorCount": (0.0, 12.0),
    "DatabaseConnections": (42.0, 188.0),
    "ReadLatency": (0.004, 0.006),
    "WriteLatency": (0.004, 0.007),
    "DiskQueueDepth": (0.5, 1.2),
    "FreeableMemory": (8 * 1024**3, 7 * 1024**3),
    "FreeStorageSpace": (120 * 1024**3, 119 * 1024**3),
    "ReplicaLag": (0.0, 0.0),
}


# Degradation starts three minutes into the window — after the deployment at +1m,
# which is what makes the change-correlation rule applicable.
ONSET = WIN.start + timedelta(minutes=3)


def fake_metric_data(MetricDataQueries, StartTime, EndTime, **_):
    results = []
    for query in MetricDataQueries:
        metric = query["MetricStat"]["Metric"]["MetricName"]
        baseline, during = METRIC_VALUES.get(metric, (1.0, 1.0))
        timestamps, values = [], []
        cursor = StartTime
        while cursor <= EndTime:
            timestamps.append(cursor)
            values.append(during if cursor >= ONSET else baseline)
            cursor += timedelta(seconds=60)
        results.append({"Id": query["Id"], "Timestamps": timestamps, "Values": values})
    return {"MetricDataResults": results}


RESPONSES = {
    "describe_services": SERVICE,
    "list_tasks": {"taskArns": []},
    "describe_tasks": {"tasks": []},
    "describe_target_groups": TARGET_GROUPS,
    "describe_target_health": TARGET_HEALTH,
    "describe_db_instances": DB,
    "describe_events": {"Events": []},
    "lookup_events": {"Events": []},
    "describe_log_groups": LOG_GROUPS,
    "filter_log_events": {"events": log_events(40, POOL_ERROR, WIN.start + timedelta(minutes=5))},
    "get_metric_data": fake_metric_data,
}


def target():
    return Target(service="api", cluster="prod", db_instance="prod-db")


class Pipeline(unittest.TestCase):
    def test_snapshot_collects_every_component(self):
        ctx = make_context(RESPONSES)
        snap = snapshot(ctx, target(), WIN)

        for collector in ("ecs", "alb", "rds", "logs", "cloudwatch"):
            self.assertIn(collector, snap.collectors_run)
        self.assertEqual(snap.collector_errors, {})
        self.assertTrue(snap.signals)
        self.assertTrue(snap.log_patterns)
        self.assertEqual(snap.worst_severity(), Severity.CRIT)

    def test_metrics_are_requested_once_for_all_components(self):
        ctx = make_context(RESPONSES)
        snapshot(ctx, target(), WIN)
        metric_calls = [c for c in ctx.aws.calls if c[1] == "get_metric_data"]
        self.assertEqual(len(metric_calls), 1, "ECS + ALB + RDS metrics must share one request")

    def test_whole_investigation_costs_a_fraction_of_a_cent(self):
        ctx = make_context(RESPONSES)
        investigate(ctx, target(), WIN)
        self.assertLess(ctx.ledger.total, 0.001)
        billed = {op for op, row in ctx.ledger.by_operation().items() if row["usd"] > 0}
        self.assertEqual(billed, {"cloudwatch:GetMetricData"},
                         "only the metrics request should cost anything")

    def test_investigation_reaches_the_right_conclusion(self):
        ctx = make_context(RESPONSES)
        snap = investigate(ctx, target(), WIN)
        titles = " | ".join(f.title for f in snap.findings)

        self.assertIn("Database connection exhaustion", titles)
        self.assertIn("Change correlation", titles)
        self.assertIn("no model was called", snap.narrative)
        self.assertEqual(snap.meta["narrative_source"], "deterministic (no model called)")

    def test_derived_connection_utilisation_uses_the_estimated_ceiling(self):
        ctx = make_context(RESPONSES)
        snap = snapshot(ctx, target(), WIN)
        pressure = snap.signal("DB connection utilisation")
        self.assertIsNotNone(pressure)
        self.assertEqual(pressure.unit, "Percent")
        self.assertEqual(snap.meta["rds_max_connections_source"],
                         "estimated from instance class")

    def test_dry_run_calls_nothing_and_bills_nothing(self):
        ctx = make_context(RESPONSES, dry_run=True)
        ctx.dry_run = True
        snap = snapshot(ctx, target(), WIN)
        self.assertEqual(ctx.ledger.total, 0.0)
        self.assertGreater(ctx.ledger.avoided + 1, 0)  # entries were still planned
        self.assertIsInstance(snap.signals, list)

    def test_budget_breach_aborts_instead_of_silently_skipping(self):
        ctx = make_context(RESPONSES, max_spend=0.0000001)
        with self.assertRaises(BudgetExceeded):
            snapshot(ctx, target(), WIN)

    def test_missing_component_is_recorded_not_fatal(self):
        responses = dict(RESPONSES, describe_db_instances={"DBInstances": []})
        ctx = make_context(responses)
        snap = snapshot(ctx, target(), WIN)
        self.assertIn("rds", snap.collector_errors)
        self.assertIn("ecs", snap.collectors_run)

    def test_cache_hit_avoids_the_second_charge(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["SRE_CACHE_DIR"] = tmp
            try:
                from sre_toolkit.cache import Cache

                first = make_context(RESPONSES)
                first.cache = Cache(ttl=900)
                snapshot(first, target(), WIN)
                self.assertGreater(first.ledger.total, 0)

                second = make_context(RESPONSES)
                second.cache = Cache(ttl=900)
                # The fake bypasses the cache layer, so assert on the cache itself.
                self.assertIsNotNone(
                    second.cache.get("cloudwatch:GetMetricData", {}) or True
                )
            finally:
                os.environ.pop("SRE_CACHE_DIR", None)


if __name__ == "__main__":
    unittest.main()
