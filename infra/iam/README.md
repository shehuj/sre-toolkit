# IAM for sre-toolkit

Two policies, split so the expensive permission is a separate, deliberate grant.

| File | Grants | Cost exposure |
| --- | --- | --- |
| `sre-toolkit-readonly.json` | Everything the toolkit needs to collect an incident | Only `cloudwatch:GetMetricData` ($0.01 / 1,000 metrics) and `logs:StartQuery` ($0.005 / GB scanned) are billable |
| `sre-toolkit-bedrock.json` | `bedrock:InvokeModel` on **one** model ARN | Per-token; attach only to principals you want to be able to spend on inference |

Notes worth knowing before you attach these:

- **`Resource: "*"` is not laziness here.** `cloudwatch:GetMetricData`, every
  `Describe*` action in the list, and `cloudtrail:LookupEvents` do not support
  resource-level permissions. Constrain them with condition keys
  (`aws:RequestedRegion`, `aws:PrincipalTag`) or a permissions boundary instead.
- **`LogsInsightsOptional` is separable.** Drop that statement entirely if you
  never want anyone running `--deep`; the toolkit degrades to the free
  `FilterLogEvents` path and says so.
- **Nothing here can mutate anything.** There is no `Create*`, `Update*`,
  `Delete*`, `Put*`, `Modify*` or `Reboot*` action in either policy, which is why
  the tool can state "no automated remediation performed" as a property of the
  credential rather than a promise about the code.
- **Scope by region** where you can. Adding
  `"Condition": {"StringEquals": {"aws:RequestedRegion": "us-east-1"}}` to the
  observability statements bounds both blast radius and bill.

## Why the Bedrock policy names a single model

Pinning the resource to one foundation-model ARN is a spend control as much as a
security control: a misused credential cannot reach for a model that costs 25×
more per token than the default. Add further model ARNs only when you intend to
pay for them.
