# Cost engineering

The design brief asked for this toolkit to be as cost-effective as possible. That
turned into eight decisions, each of which is visible in the code and checked by a
test.

## 1. Nothing runs when you are not using it

There is no agent, no collector daemon, no scheduler, no database and no dashboard
service. The toolkit is a CLI that starts, reads, prints and exits. Standing
infrastructure cost is **$0.00** — `infra/terraform` creates only IAM resources,
which are free.

## 2. Free APIs answer the question first

The most valuable fact during an incident — *what changed* — is free to collect.
`ecs:DescribeServices`, `rds:DescribeEvents`, `elasticloadbalancing:DescribeTargetHealth`
and `cloudtrail:LookupEvents` are not billed, so the pipeline runs them all before
it spends anything (`src/sre_toolkit/investigate.py`).

## 3. One metrics request, not twenty

`cloudwatch:GetMetricData` is billed **per metric requested**, not per call. So
every metric from every component — ECS, ALB and RDS — goes into a single request
with one query per metric (`collectors/metrics.py`). A full 19-metric
investigation costs **$0.00019**.

A test asserts this rather than trusting it:

```python
def test_metrics_are_requested_once_for_all_components(self):
    ...
    self.assertEqual(len(metric_calls), 1)
```

## 4. The period scales with the window

A 30-minute incident is fetched at 60-second resolution; a 30-day SLO window at
one day. `pick_period()` targets ~60 datapoints regardless of window length, which
keeps responses small and avoids paying for resolution no human will read.

## 5. Baseline comes free with the window

"Before vs. during" normally means two queries. Instead the single request spans
the incident window *plus an equal window before it*, and the series is split
locally. Same answer, half the billed metrics.

## 6. Logs Insights is opt-in; FilterLogEvents is the default

| API | Charge | Used |
| --- | --- | --- |
| `logs:FilterLogEvents` | no per-GB charge | always |
| `logs:StartQuery` (Insights) | **$0.005 per GB scanned** | only behind `--deep` |

On a chatty service, one careless Insights query over a wide window is the most
expensive thing this toolkit can do — easily more than every other call combined.
So the default path pulls bounded pages with `FilterLogEvents` and does the
grouping locally, for free. When you do pass `--deep`, the command prints the
estimated scan before running and the actual scan afterwards, and the estimate is
checked against `--max-spend` first.

## 7. Analysis is free by default; AI is opt-in and compacted

The correlator (`correlate/rules.py`) is twelve deterministic rules over the
snapshot. It costs nothing, runs offline, and returns the same answer every time —
which matters at 03:00 and makes it testable.

`--ai` adds a Bedrock narrative on top. Its cost controls:

- Default model is the cheapest capable Claude (`anthropic.claude-haiku-4-5`).
- The prompt is a **compacted** snapshot: one line per signal, one per timeline
  event, and the top 8 *normalised* log patterns — never raw log bodies. Input
  stays ~1–2k tokens no matter how large the incident is.
- `max_tokens` is capped at 900; the output is a structured summary, not an essay.
- Structured output (`output_config.format`) means one call returns parseable
  JSON. No "please reply in JSON" retry.
- Cost is estimated and budget-checked *before* the call, then re-charged against
  the real token usage the API reports.
- The IAM policy in `infra/iam/sre-toolkit-bedrock.json` names a single model ARN,
  so a misused credential cannot invoke a model that costs 25× more per token.

Typical `--ai` cost: **~$0.003 per incident.**

## 8. You can always see, cap, and avoid the spend

| Control | What it does |
| --- | --- |
| `--dry-run` | Plans and prices the whole investigation without calling AWS or Bedrock |
| `sre cost estimate` | The same thing, formatted as a bill of operations |
| `--max-spend 0.05` | Refuses any call that would take the run over the limit — a breach aborts, it never silently skips work |
| `--cache-ttl` | Re-running an investigation within the TTL re-reads from disk and charges nothing |
| `sre cost prices` | The price table being metered against, overridable in `~/.config/sre-toolkit/pricing.json` |
| end of every report | What the run actually cost, and what the cache avoided |

The cache matters more than it looks. Incident investigation is iterative — you
run the same window three or four times while you refine the service name or add
a flag. Without a cache you pay CloudWatch four times for identical data.

## What a real investigation costs

30-minute window, ECS + ALB + RDS + CloudTrail + 1,000 log events:

| Step | Cost |
| --- | --- |
| ECS / RDS / ELB / CloudTrail describes (≈30 calls) | $0.000000 |
| 1,000 log events via `FilterLogEvents` | $0.000000 |
| One `GetMetricData` with 19 metrics | $0.000190 |
| Correlation + deterministic narrative | $0.000000 |
| **Default total** | **$0.00019** |
| `--ai` narrative (Haiku 4.5, ~1.5k in / 400 out) | +$0.003 |
| `--deep` Insights query scanning 1 GB | +$0.005 |

Investigating ten incidents a day, every day, on the default path costs about
**$0.06 a month**. The same ten incidents a day with `--ai` costs about **$0.90 a
month**.

## Non-AWS costs worth naming

- **GitHub Actions**: one job, one Python version, no `pip install` on the test
  path, and `cancel-in-progress` concurrency so superseded runs stop billing.
- **Package install**: the core has zero runtime dependencies, so `pip install
  sre-toolkit` resolves nothing. Cold starts are fast and the supply-chain surface
  is empty. boto3, kubernetes, dnspython and anthropic are extras you opt into.
