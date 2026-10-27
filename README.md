# asg-insights

Leadership-facing historical dashboard for AWS Auto Scaling Group capacity and
optimization. Discovers all prod ASGs (by tag), pulls CloudWatch metrics on a
schedule, stores rolled-up time-series in SQLite, and renders a monthly-review
dashboard.

**Not** a live control-plane tool. Read-only. Dashboard queries SQLite only —
never CloudWatch directly, so page loads are free and stay working during a
CloudWatch outage.

## Cost tuning (already applied)

| Knob | Value | Why |
|---|---|---|
| EC2 instance | `t3.micro` | ~$7.50/mo, plenty for gunicorn + SQLite |
| EBS volume | 12 GiB gp3 | ~$1.00/mo; SQLite fits years of hourly data |
| Puller cadence | every 1 hour | Aligns with PS 6-h refresh; 4× cheaper than 15-min polling |
| Metric period | `Period=300` (5-min) | 12 datapoints/hour preserved; server-side aggregation is free |
| PS forecast | 48-h window per poll | Regenerated every 6 h by AWS; hourly poll is a safety margin |
| Retention | 5-min for 30d → 1-hr forever | Skip the 1-min table entirely |
| Dashboard AWS calls | zero | UI hits SQLite only; puller is the only AWS-facing code |

Rough monthly bill for ~50 prod ASGs in one region: EC2 ~$30 (t3.medium) +
EBS ~$1.60 + CloudWatch API ~$1.80 + S3 ~$0.05 = **≈$33/month**.

## Local dev

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m app.seed_fake            # 45 days of fake data + freshness rows
FLASK_APP=app.main flask run --port 8080
# open http://127.0.0.1:8080
```

## Deploy to AWS

The deploy script does everything — bucket create, source sync, template
validate, `cloudformation deploy`, print outputs. Account, region, and bucket
name are discovered from your AWS profile — nothing is hardcoded:

```bash
cd claude-output
./scripts/deploy.sh
```

Override defaults with env vars: `AWS_PROFILE_OVERRIDE`, `AWS_REGION`,
`ALLOWED_CIDR` (defaults to the default-VPC block 172.31.0.0/16).

After the stack is up, reach the dashboard via SSM port-forward:
## This will change for production deployment and UI configuration setup.

```bash
INST=$(aws --profile default cloudformation describe-stack-resources \
  --stack-name asg-insights --logical-resource-id Dashboard \
  --query 'StackResources[0].PhysicalResourceId' --output text)
aws --profile default ssm start-session --target "$INST" \
  --document-name AWS-StartPortForwardingSession \
  --parameters portNumber=80,localPortNumber=8080
# open http://127.0.0.1:8080
```

## Layout

```
app/
  main.py            Flask routes (SQLite-only, no AWS calls)
  db.py              SQLite connection helpers, WAL mode
  schema.sql         DDL: config, snapshots_5m, snapshots_1h, monthly_rollup,
                     recommendations, ingestion_status (freshness)
  pull_metrics.py    THE ONLY module that talks to AWS. Runs on a timer.
  rollup.py          Nightly: 1-min → 5-min after 180d, 5-min → 1-hour after 365d, monthly aggs
  seed_fake.py       Synthetic data for local UI dev
  templates/         Jinja templates (base has fleet-wide freshness strip;
                     asg_detail has per-source freshness cards)
  static/style.css
systemd/
  asg-insights.service     gunicorn on 127.0.0.1:8000 behind nginx :80
  asg-puller.timer         15-min metric ingestion
  asg-rollup.timer         nightly retention + monthly rollup at 03:15 UTC
scripts/
  user_data.sh             EC2 bootstrap
  deploy.sh                one-shot deploy from your workstation
infra/
  cloudformation.yaml      EC2 + IAM + SG + S3 + optional Route53 record
```

## Data freshness (reliability)

Every pull writes a row to `ingestion_status(asg_name, source, last_success_ts,
last_success_data_ts, last_error_ts, last_error_msg)`. The base template renders
a strip across the top of every page with per-source ages (green under 30 min,
red beyond). The ASG detail page adds a 4-card grid: **ASG config**,
**CloudWatch metrics**, **Predictive Scaling**, **Target Tracking**.

If leadership asks "is this data current?", the answer is on-screen.

## Documentation

- **Prerequisites & first-time setup** for a new customer:
  [docs/PREREQUISITES.md](docs/PREREQUISITES.md)
- **Day-2 operations** (config changes, backup/restore, troubleshooting):
  [docs/OPERATIONS.md](docs/OPERATIONS.md)
- **Data model, puller, and scheduler internals**:
  [docs/DATA_AND_JOBS.md](docs/DATA_AND_JOBS.md)
- **Private DNS + browser access without port-forward**:
  [docs/DNS_ACCESS.md](docs/DNS_ACCESS.md)

## Known limitations (read before shipping)

These are documented gaps, not bugs — the app runs cleanly with them.
Address each when the corresponding data starts to matter to your customer.

1. **Instance-type resolution** ([app/pull_metrics.py:66](app/pull_metrics.py:66)) —
   the puller reads `instance_type` from an ASG tag rather than resolving
   it via `DescribeLaunchTemplateVersions`. If the tag is absent,
   `hourly_rate_usd` falls back to 0 and cost columns read as $0. Add the
   real resolution when accurate costs matter.
2. **ALB `LoadBalancer` dimension** ([app/pull_metrics.py:228](app/pull_metrics.py:228)) —
   the RCPT query today omits the `LoadBalancer` dimension. CloudWatch
   usually finds the metric anyway, but for reliable results in accounts
   with many ALBs, resolve via `elbv2:DescribeTargetGroups.LoadBalancerArns[0]`
   and add the dimension.
3. **On-demand hourly rates** — hardcoded in `HOURLY_RATE_USD` (top of
   `pull_metrics.py`). Add rows for any instance families you run.
   Swap for the Pricing API when ±10 % cost accuracy isn't enough.
4. **No auth on Flask** — relies on being in a private subnet reached only
   by SSM port-forward or via the private-zone DNS. Add oauth2-proxy or
   put an internal ALB with OIDC in front for SSO.
5. **Discovery is tag-based only** — if a prod ASG has no discovery tag,
   the dashboard won't see it. Either tag all prod ASGs
   (see [PREREQUISITES.md §5](docs/PREREQUISITES.md)) or override the
   discovery tag via `systemctl edit asg-puller.service`.
