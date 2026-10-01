# sre-toolkit

**OpsPilot** — an SRE incident investigation toolkit that collects telemetry,
correlates it into ranked hypotheses, and explains what it found. Read-only by
construction, metered to the fraction of a cent, and runnable with no AWS account
at all.

```
sre incident investigate --service customer-api \
  --cluster production --db prod-customer-db \
  --start "2026-10-01T06:30:00" --end "2026-10-01T07:00:00"
```

```
INCIDENT INVESTIGATION
────────────────────────────────────────
Service            customer-api
Window             2026-10-01 06:30 → 07:00 UTC
Collectors         ecs, alb, rds, cloudtrail, logs, cloudwatch

IMPACT
✗ HTTP 5xx (target): peaked at 226 (critical ≥ 50)
✗ p95 latency: peaked at 3.80s (critical ≥ 3.00s)
✗ DB connection utilisation: 188 of ~200 connections (94%)
▲ Healthy targets: dropped to 4.0 (warn ≤ 2.0)
▲ ECS CPU: peaked at 88.4% (warn ≥ 80.0%)

TIMELINE
06:31:00  ▲ UpdateService by deploy-bot/github-actions  production/customer-api
06:31:12  • Deployment completed (customer-api:184)  rollout=COMPLETED running=6 desired=6
06:35:00  ✗ First 'Error' log: <ts> ERROR HikariPool-<n> - Connection is not available
06:39:00  ✗ (service customer-api) (port 8080) is unhealthy in target-group customer-api-tg
06:48:00  ✓ (service customer-api) has reached a steady state.

CORRELATED SIGNALS
   SIGNAL                     SOURCE      BASELINE  PEAK     CHANGE  SHAPE
✗  HTTP 5xx (target)          cloudwatch  2         226.0    ×113.0  ▁▁▁▁▁▁▁▂▅▆▇███▅▄▃▂▁
✗  p95 latency                cloudwatch  240ms     3.80s    ×15.8   ▁▁▁▁▁▁▂▅▇▇███▇▅▄▃▂▁
✗  DB connection utilisation  cloudwatch  21.00%    94.00%   ×4.5    ▁▁▁▁▁▁▄▆█████▇▅▃▂▂▁
▲  ECS CPU                    cloudwatch  34.00%    88.40%   ×2.6    ▁▁▁▁▁▃▅▆▇████▆▄▃▂▂▁

LIKELY CONTRIBUTING FACTORS
1. Database connection exhaustion [high confidence]
   Connection demand reached the instance ceiling, so requests queued for a
   connection and timed out at the application tier. This presents as 5xx with
   high latency while database CPU stays unremarkable.
     ↳ DB connection utilisation: 188 of ~200 connections (94%)
     ↳ log:connection_pool_exhausted: 412 lines matched the signature
2. Change correlation: failure began shortly after a deployment [high confidence]
   Deployment completed (customer-api:184) at 06:31:12 precedes the first
   degraded signal at 06:33:00 by 1m48s.

RECOMMENDED INVESTIGATION
1. Compare the application connection-pool size × task count against the instance max_connections.
2. Check whether the deployment changed pool size, pool timeout, or task count.
3. Review RDS Performance Insights for the same window to confirm sessions waiting
   on connection acquisition rather than on queries.

COST OF THIS RUN
Spent              $0.0002

NO AUTOMATED REMEDIATION PERFORMED
```

See it for yourself, with no AWS account and no spend:

```bash
git clone https://github.com/Jenom/sre-toolkit && cd sre-toolkit
PYTHONPATH=src python3 -m sre_toolkit --demo incident investigate
```

---

## Why it is cheap

That investigation above costs **$0.00019**. Running ten of them a day, every day,
costs about **six cents a month**. The reasons are structural, not incidental:

| Decision | Effect |
| --- | --- |
| No agent, daemon, database or dashboard — a CLI that exits | $0 standing cost |
| Free `Describe*` and CloudTrail data collected first | "what changed" costs nothing |
| **All** metrics from ECS + ALB + RDS in **one** `GetMetricData` request | billed per metric, so 19 metrics = $0.00019 |
| Baseline fetched inside the same request as the window | half the billed metrics for before/after |
| Period scales to the window (~60 datapoints) | no paying for resolution nobody reads |
| `FilterLogEvents` by default; Logs Insights only behind `--deep` | avoids the $0.005/GB scan |
| Correlation is 12 deterministic rules, not a model | analysis is free, offline and repeatable |
| `--ai` sends a *compacted* snapshot, never raw logs | ~$0.003 per incident, input bounded |
| Disk cache with TTL | re-running an investigation is free |
| `--max-spend`, `--dry-run`, `sre cost estimate` | spend is capped, previewable and printed |
| Zero runtime dependencies in the core | nothing to resolve, fast cold starts |

Full reasoning, worked examples and the price table: **[docs/COST.md](docs/COST.md)**.

## Install

The core is stdlib-only. Extras are what cost weight, so you opt into them.

```bash
pip install -e .            # CLI, health/cert/TLS/DNS checks, demo mode — no dependencies
pip install -e '.[aws]'     # + boto3: ECS, RDS, ALB, CloudWatch, CloudTrail collectors
pip install -e '.[k8s]'     # + kubernetes client
pip install -e '.[ai]'      # + anthropic[bedrock] for optional AI narratives
pip install -e '.[all]'     # everything
```

Both `sre` and `ops` are installed as entry points. Without installing, use
`PYTHONPATH=src python3 -m sre_toolkit`.

## Commands

```
sre health https://api.example.com                  HTTP + DNS + TLS + TCP probe (free)
sre cert check --domain example.com                 certificate expiry (free)

sre aws ecs --cluster prod --service api            service state, deployments, task failures
sre aws rds --instance prod-db                      status, connections, events
sre aws alb --name api-lb                           target health and error rates
sre aws diagnose --service api --cluster prod --db prod-db

sre k8s diagnose deployment/api -n prod             replicas, pods, events
sre k8s pods -n prod | sre k8s nodes | sre k8s events -n prod

sre logs investigate --service payments             group errors into patterns
sre logs investigate --service payments --deep      + Logs Insights (billed per GB)
sre logs groups --service payments                  discover log groups (free)

sre incident snapshot --service api --cluster prod -o incident.json
sre incident investigate --service api --cluster prod
sre incident summarize incident.json                re-analyse a saved snapshot (free)
sre incident timeline incident.json
sre incident postmortem incident.json -o postmortem.md

sre slo calculate --target-group api-tg --objective 99.9 --days 30
sre dr validate --db prod-db --bucket my-backups

sre cost estimate --service api --cluster prod      price a run without making it
sre cost prices                                     the price table being metered
sre cache stats | sre cache clear
```

Global flags that matter: `--max-spend USD`, `--dry-run`, `--cache-ttl`, `--deep`,
`--ai`, `--model`, `--json`, `-o FILE`, `--demo`, `--region`, `--profile`.

Exit codes compose in shell pipelines and CI: `0` healthy, `1` warning, `2`
critical, `3` target not found, `4` missing optional dependency, `5` budget
exceeded.

## How it works

```
            sre incident investigate
                      │
        ┌─────────────┴─────────────┐
        │   1. free control plane   │   ECS · RDS · ELB · CloudTrail · log filters
        └─────────────┬─────────────┘   $0.00
                      │
        ┌─────────────┴─────────────┐
        │   2. one metrics request  │   every metric, one GetMetricData
        └─────────────┬─────────────┘   $0.00019
                      │
        ┌─────────────┴─────────────┐
        │   3. local pattern work   │   normalise → group → classify
        └─────────────┬─────────────┘   $0.00
                      │
        ┌─────────────┴─────────────┐
        │   4. correlation engine   │   12 deterministic rules
        └─────────────┬─────────────┘   $0.00
                      │
        ┌─────────────┴─────────────┐
        │   5. narrative            │   deterministic by default
        └─────────────┬─────────────┘   optional --ai → Bedrock, ~$0.003
                      ▼
                 SRE report  +  incident.json artefact
```

The artefact is the contract between the paid step and the free step:

```bash
sre incident snapshot --service api --cluster prod -o incident.json   # collect once
sre incident summarize incident.json                                  # analyse free
sre incident summarize incident.json --ai                             # once, with a model
sre incident postmortem incident.json -o postmortem.md
```

### Correlation rules

Twelve rules in `src/sre_toolkit/correlate/rules.py`, each a pure function of the
snapshot: change correlation, database connection exhaustion, compute saturation,
container restart loops, serving-capacity loss, downstream dependency failure,
quota throttling, traffic surge, storage exhaustion, replication lag, certificate
and credential failures, and database contention. Confidence is assembled from
corroborating evidence and temporal ordering — not guessed — and when nothing
matches, the report says so instead of inventing a cause.

### Optional AI layer

`--ai` adds a Bedrock narrative via the Anthropic SDK
(`AnthropicBedrockMantle`), defaulting to `anthropic.claude-haiku-4-5`. It
receives the compacted snapshot — signals, timeline, normalised log patterns and
the rule-based findings — and returns structured JSON validated against a schema,
so one call is always enough. It critiques the deterministic findings rather than
replacing them, and the deterministic narrative is preserved in the artefact
alongside it.

## Safety properties

- **Read-only by construction.** No `Create*`, `Update*`, `Delete*`, `Put*`,
  `Modify*` or `Reboot*` call exists anywhere in the codebase, and the IAM policy
  in `infra/iam/` grants none. "No automated remediation performed" is a property
  of the credential, not a promise about the code.
- **Budget breaches abort.** `--max-spend` raises rather than quietly skipping the
  expensive collectors, so a capped run never silently becomes a worse one.
- **Partial results beat tracebacks.** A collector that fails is recorded on the
  snapshot and reported; the rest of the investigation continues.
- **No secrets printed.** Collectors read resource configuration, never secret
  values, and log bodies are normalised before they are shown or sent anywhere.

## Infrastructure

| Path | What it is | Cost |
| --- | --- | --- |
| `infra/iam/sre-toolkit-readonly.json` | Least-privilege read-only policy | free |
| `infra/iam/sre-toolkit-bedrock.json` | `InvokeModel` on a single model ARN | free to hold |
| `infra/terraform/main.tf` | GitHub OIDC provider + role, region-boundary deny | free |
| `.github/workflows/ci.yml` | One job, no `pip install` on the test path | ~1 runner-minute |
| `.github/workflows/scheduled-checks.yml` | Daily cert expiry + error budget | ~$0.00003/run |

No long-lived AWS keys: GitHub exchanges an OIDC token for a short session on the
role, scoped to one repository and ref.

## Development

```bash
make test       # 69 tests, no dependencies, ~0.1s
make demo       # full pipeline on the bundled incident
make cost       # price table
make fixture    # regenerate the demo snapshot
```

Tests cover the pricing and budget maths, cache behaviour, log normalisation and
classification, metric batching and severity classification, every correlation
rule, prompt compaction, the CLI surface, and the whole investigation pipeline
against a fake AWS (`tests/support.py`) — so the AWS code paths are exercised
without credentials, network or spend.

## Status

Built: health (HTTP/TCP/DNS/TLS), certificates, ECS, RDS, ALB, CloudWatch metrics
and logs, CloudTrail change correlation, Kubernetes (deployments/pods/nodes/events),
incident snapshot/investigate/summarize/timeline/postmortem, SLO and error budget,
DR and backup validation, cost estimation and metering.

Not built yet, in rough priority order: Lambda and DynamoDB collectors, Prometheus
and Loki as metric/log sources, Splunk, `security iam` and `security secrets`
audits, Azure and GCP.

The original design brief this was built from is kept at
[docs/DESIGN.md](docs/DESIGN.md).

## License

MIT
