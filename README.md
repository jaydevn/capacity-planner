# asg-insights

Leadership-facing historical dashboard for AWS Auto Scaling Group capacity and
optimization. Discovers all prod ASGs (by tag), pulls CloudWatch metrics on a
schedule, stores rolled-up time-series in SQLite, and renders a monthly-review
dashboard.

**Not** a live control-plane tool. Read-only. Dashboard queries SQLite only —
never CloudWatch directly, so page loads are free and stay working during a
CloudWatch outage.

## Architecture at a glance

- **Flask app on one EC2 instance** (gunicorn behind nginx) in a chosen region.
- **SQLite** on the instance's EBS volume — the only datastore.
- **Poller** (`app/pull_metrics.py`) runs on a systemd timer and is the only
  AWS-facing code. It discovers ASGs **by tag key/value** and, by default,
  scans **every region enabled in the account** (`ASG_SCAN_REGIONS=all`),
  collecting target-tracking configuration and predictive-scaling forecasts.
- **Public Application Load Balancer** (internet-facing) forwards port 80 to
  the EC2 instance; the instance security group accepts traffic only from the
  ALB. Browse the dashboard at the ALB DNS name printed by `deploy.sh`.
  Restrict `ALLOWED_CIDR` (and add auth) before exposing real data.

## Cost tuning (already applied)

| Knob | Value | Why |
|---|---|---|
| EC2 instance | `t3.medium` | ~$30/mo; default, fits SQLite page cache for 50–150 ASGs |
| EBS volume | 20 GiB gp3 | ~$1.60/mo; SQLite fits years of hourly data |
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
./scripts/deploy.sh
```

Override defaults with env vars: `AWS_PROFILE_OVERRIDE`, `AWS_REGION`,
`ASG_SCAN_REGIONS` (`all` by default — every enabled region — or a
comma-separated allowlist), `ALLOWED_CIDR` (defaults to `0.0.0.0/0`;
restrict this to your office/VPN range), and `PUBLIC_SUBNET_IDS`
(auto-discovered from the default VPC otherwise).

After the stack is up, open the dashboard at the **public ALB DNS name** —
it's printed in the stack outputs as `DashboardUrl` / `LoadBalancerDns`:

```bash
aws --profile default cloudformation describe-stacks \
  --stack-name asg-insights \
  --query "Stacks[0].Outputs[?OutputKey=='DashboardUrl'].OutputValue" \
  --output text
# open the printed http://<alb-dns-name>/ URL in a browser
```

> The ALB is internet-facing and the app has no built-in authentication.
> Set `ALLOWED_CIDR` to a trusted range, and/or put OIDC/Cognito auth on the
> ALB listener, before pointing it at real data. SSM port-forward to the
> instance's private IP still works too, if you prefer keeping it internal.

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
  asg-puller.timer         hourly metric ingestion (all regions, by tag)
  asg-rollup.timer         nightly retention + monthly rollup at 03:15 UTC
scripts/
  user_data.sh             EC2 bootstrap
  deploy.sh                one-shot deploy from your workstation
infra/
  cloudformation.yaml      EC2 + public ALB + IAM + SGs + optional Route53 alias
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
3. **On-demand hourly rates** — resolved from the **AWS Pricing API**
   (`pricing:GetProducts`) by the poller and cached in the `pricing_cache`
   table, so recommendation savings use live list prices. The static
   `HOURLY_RATE_USD` dict in `pull_metrics.py` is only a fallback used when
   the Pricing API is unreachable or has no match for an instance type.
4. **No auth on Flask** — the dashboard is now fronted by an **internet-facing**
   ALB, so there is no network isolation by default. Before exposing real data,
   either tighten `ALLOWED_CIDR` to a trusted range, or add OIDC/Cognito
   authentication on the ALB listener (`authenticate-oidc` /
   `authenticate-cognito` actions), or front it with oauth2-proxy.
5. **Discovery is tag-based only** — if a prod ASG has no discovery tag,
   the dashboard won't see it. Either tag all prod ASGs
   (see [PREREQUISITES.md §5](docs/PREREQUISITES.md)) or override the
   discovery tag via `systemctl edit asg-puller.service`.
