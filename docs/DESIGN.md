# sre-toolkit

sre-toolkit
├── health
│   ├── http
│   ├── tcp
│   ├── dns
│   └── tls
│
├── aws
│   ├── ecs
│   ├── ec2
│   ├── rds
│   ├── alb
│   └── s3
│
├── kubernetes
│   ├── pods
│   ├── nodes
│   ├── events
│   └── deployments
│
├── logs
│   ├── cloudwatch
│   ├── splunk
│   └── loki
│
├── database
│   ├── connections
│   ├── replication
│   └── storage
│
├── incident
│   ├── snapshot
│   ├── timeline
│   ├── summarize
│   └── postmortem
│
├── security
│   ├── certificates
│   ├── secrets
│   └── iam
│
└── reliability
    ├── slo
    ├── error-budget
    ├── backup
    └── dr

And the interface could be extremely simple:
sre health https://api.example.com
sre aws ecs --cluster production
sre aws rds --instance prod-db
sre k8s diagnose deployment/api
sre logs investigate --service payments
sre cert check --domain example.com
sre incident snapshot --service payments
sre incident summarize incident.json
sre slo calculate --service payments
sre dr validate production

 The part I’d make AI-powered

Your Incident Investigator could become the centerpiece.

                INCIDENT
                    │
                    ▼
          ┌──────────────────┐
          │ Incident Collector│
          └────────┬─────────┘
                   │
       ┌───────────┼───────────┐
       ▼           ▼           ▼
    Metrics       Logs      Changes
       │           │           │
       └───────────┼───────────┘
                   ▼
            Correlation Engine
                   │
                   ▼
              AI Analysis
                   │
       ┌───────────┼───────────┐
       ▼           ▼           ▼
   Timeline     Findings    Evidence
       │           │           │
       └───────────┼───────────┘
                   ▼
             SRE Report



For example:
sre incident investigate \
  --service customer-api \
  --start "2026-10-01T06:30:00" \
  --end "2026-10-01T07:00:00"


INCIDENT INVESTIGATION
────────────────────────────────────────

Service: customer-api
Incident window: 06:30 - 07:00

IMPACT
• HTTP 5xx increased from 0.2% → 18.7%
• p95 latency increased from 240ms → 3.8s
• 37% of requests affected

TIMELINE
06:31  Deployment completed
06:33  CPU began increasing
06:35  DB connection utilization >90%
06:37  HTTP 5xx began increasing
06:41  Autoscaling triggered
06:48  Error rate began declining

CORRELATED SIGNALS
✓ ECS deployment
✓ RDS connection saturation
✓ API latency
✓ HTTP 5xx

LIKELY CONTRIBUTING FACTOR
Database connection exhaustion following deployment.

EVIDENCE
[links to logs]
[links to metrics]
[deployment ID]

RECOMMENDED INVESTIGATION
1. Compare DB connection pool before/after deployment
2. Review application connection timeout settings
3. Compare affected task versions
4. Review RDS Performance Insights

NO AUTOMATED REMEDIATION PERFORMED

🚀 A practical tech stack

Given the type of AWS/SRE work you’ve been working with, I’d use:

Core

* Python
* Click/Typer for CLI
* boto3
* Kubernetes Python client
* requests
* dnspython
* psutil

Observability

* Prometheus
* Grafana
* CloudWatch
* Splunk
* Loki

Cloud

* AWS first
* Azure later
* GCP eventually

Automation

* Terraform
* GitHub Actions
* SSM
* Lambda

AI

* Amazon Bedrock
* Structured JSON incident context
* LLM-generated investigation summary

Security

* IAM least privilege
* OIDC
* Secrets Manager
* CloudTrail
* audit logging

💡 One particularly strong portfolio version

I’d call it something like:

OpsPilot — SRE Incident Investigation Toolkit

Start with only five commands:

ops incident snapshot
ops logs investigate
ops health check
ops aws diagnose
ops incident summarize


Then make the architecture:
CLI
 │
 ▼
SRE Toolkit API
 │
 ├── AWS Collector
 ├── Kubernetes Collector
 ├── Observability Collector
 ├── Deployment Collector
 └── Incident Correlator
          │
          ▼
       Bedrock
          │
          ▼
   Investigation Report



This would be much more compelling as an SRE portfolio project than simply building another monitoring dashboard because it demonstrates observability + troubleshooting + automation + cloud + incident management + AI, all in one system.
