# asg-insights — data model, puller, and scheduler

Everything about how data flows from CloudWatch into SQLite, and how the two
background jobs are scheduled. Companion to
[OPERATIONS.md](OPERATIONS.md) (day-2 tasks) and
[PREREQUISITES.md](PREREQUISITES.md) (first-time setup).

---

## 1. SQLite database

Single file, on the instance:

- **Path**: `/var/lib/asg-insights/asg_insights.db`
- **Journal mode**: WAL (`asg_insights.db-wal` and `.db-shm` are companion files)
- **Owned by**: `asginsights:asginsights`
- **Growth rate**: ~5 MB per year per 50 ASGs (hourly rollup dominates)
- **DDL source**: [app/schema.sql](../app/schema.sql) — re-applied on Flask startup (`CREATE TABLE IF NOT EXISTS`, so it's idempotent)

### Tables

#### `asg_config` — one row per discovered ASG
Purpose: canonical metadata. Refreshed on every puller run.

| Column | Type | Meaning |
|---|---|---|
| `asg_name` | TEXT PK | Auto Scaling group name |
| `region` | TEXT | AWS region |
| `instance_type` | TEXT | e.g. `m5.4xlarge`; used for cost math |
| `min_size`, `max_size` | INTEGER | ASG capacity bounds |
| `tt_target` | REAL | Target-tracking `TargetValue` (req/inst/min) |
| `ps_enabled` | INTEGER | 0/1 — has a Predictive Scaling policy |
| `scale_in_enabled` | INTEGER | 0/1 — target-tracking may remove capacity |
| `tg_arns` | TEXT | JSON list of attached Target Group ARNs |
| `tags` | TEXT | JSON object of all ASG tags |
| `first_seen`, `last_seen` | INTEGER | epoch seconds |
| `hourly_rate_usd` | REAL | On-demand hourly rate for `instance_type` |
| `metric_period_sec` | INTEGER | 60 for high-res ASGs (`asg_snapshots`), 300 otherwise (`asg_snapshots_5m`) |

#### `asg_snapshots_5m` — 5-min time-series (last 30 days)
Purpose: primary metric store. The puller writes here directly.

| Column | Type | Meaning |
|---|---|---|
| `ts` | INTEGER PK | epoch seconds, aligned to 5-min bucket |
| `asg_name` | TEXT PK | FK to `asg_config` |
| `in_service_avg`, `in_service_max` | REAL/INTEGER | InService instance count |
| `desired_avg` | REAL | Desired capacity |
| `alb_rcpt_avg`, `alb_rcpt_max` | REAL | Requests per target per **minute** (normalized) |
| `ps_predicted_avg` | REAL | Predictive Scaling capacity forecast |
| `cost_hourly_usd` | REAL | `in_service_avg × hourly_rate_usd` |

#### `asg_snapshots_1h` — 1-hour rollup (30+ days, kept forever)
Same columns as `asg_snapshots_5m`. Written by the nightly rollup job, not by
the puller. This is the table monthly reviews scan.

#### `asg_snapshots` — 1-min raw (high-res ASGs only)
Only ASGs tagged `AsgInsightsResolution=1min` land here (60-second datapoints).
Rows are rolled into `asg_snapshots_5m` after 7 days and then deleted from
this table. See "Selective 1-min polling" below.

| Column | Type | Meaning |
|---|---|---|
| `ts` | INTEGER PK | epoch seconds, aligned to 1-min bucket |
| `asg_name` | TEXT PK | FK to `asg_config` |
| `in_service` | INTEGER | InService instance count |
| `desired` | INTEGER | Desired capacity |
| `alb_rcpt` | REAL | RCPT (requests per target per minute) |
| `ps_predicted` | INTEGER | Predictive Scaling capacity forecast (unused at 1-min today) |
| `cost_hourly_usd` | REAL | `in_service × hourly_rate_usd` |

#### `asg_monthly_rollup` — one row per (month, asg)
Purpose: pre-computed monthly aggregates so the fleet dashboard renders
instantly. Refreshed nightly by [app/rollup.py](../app/rollup.py).

| Column | Type | Meaning |
|---|---|---|
| `month` | TEXT PK | `YYYY-MM` |
| `asg_name` | TEXT PK | FK to `asg_config` |
| `avg_in_service`, `peak_in_service` | REAL / INTEGER | over the month |
| `p95_utilization` | REAL | `avg_rcpt / tt_target` |
| `over_prov_hours` | REAL | hours running more than needed |
| `sla_risk_minutes` | INTEGER | minutes where RCPT exceeded target |
| `estimated_cost_usd` | REAL | sum of hourly costs |
| `prior_month_delta` | REAL | cost change vs previous month |
| `computed_at` | INTEGER | epoch seconds |

#### `asg_recommendations` — auto-generated tuning suggestions
Refreshed nightly. Rules live in [`regenerate_recommendations()`](../app/rollup.py).

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | autoincrement |
| `asg_name` | TEXT | FK to `asg_config` |
| `category` | TEXT | `reduce_util_gap` \| `lower_min` \| `lower_max` \| `enable_ps` \| `enable_scale_in` |
| `headline` | TEXT | short imperative title |
| `detail` | TEXT | one-sentence rationale |
| `estimated_savings_usd` | REAL | per month |
| `confidence` | TEXT | `high` \| `medium` \| `low` |
| `generated_at` | INTEGER | epoch seconds |
| `status` | TEXT | `open` \| `accepted` \| `dismissed` |

#### `ingestion_status` — per-source data freshness
Purpose: powers the "Data freshness" strip on every page.

| Column | Type | Meaning |
|---|---|---|
| `asg_name` | TEXT PK | which ASG |
| `source` | TEXT PK | `asg_config` \| `cw_metrics` \| `ps_policy` \| `ps_forecast` \| `tt_policy` |
| `last_success_ts` | INTEGER | when the last pull ran (epoch s) |
| `last_success_data_ts` | INTEGER | newest datapoint we stored (epoch s) |
| `last_error_ts` | INTEGER | epoch s of most recent failure |
| `last_error_msg` | TEXT | truncated error text |

#### `asg_target_groups` — target groups attached to each ASG
Resolved from `elbv2:DescribeTargetGroups` on every puller run and shown on the
ASG detail page.

| Column | Type | Meaning |
|---|---|---|
| `region`, `asg_name` | TEXT PK | owning ASG |
| `tg_arn` | TEXT PK | target-group ARN |
| `tg_name` | TEXT | target-group name |
| `protocol`, `port` | TEXT / INTEGER | listener protocol + port |
| `target_type` | TEXT | `instance` \| `ip` \| `lambda` |
| `lb_arns` | TEXT | JSON list of attached load-balancer ARNs |
| `updated_at` | INTEGER | epoch seconds |

#### `pricing_cache` — on-demand hourly rates from the AWS Pricing API
Written by the poller (`pricing:GetProducts`) so the read-only UI and the
recommendation engine use live list prices without calling AWS at render time.

| Column | Type | Meaning |
|---|---|---|
| `region`, `instance_type` | TEXT PK | the rate's scope |
| `hourly_rate_usd` | REAL | Linux shared-tenancy on-demand $/hour |
| `source` | TEXT | `pricing_api` \| `fallback` (static table) |
| `updated_at` | INTEGER | epoch seconds |

### Useful ad-hoc queries

```bash
sudo -u asginsights sqlite3 /var/lib/asg-insights/asg_insights.db
```

```sql
-- how much data is in each table
SELECT 'snapshots_5m' t, COUNT(*) n FROM asg_snapshots_5m
UNION ALL SELECT 'snapshots_1h', COUNT(*) FROM asg_snapshots_1h
UNION ALL SELECT 'monthly',      COUNT(*) FROM asg_monthly_rollup
UNION ALL SELECT 'recs (open)',  COUNT(*) FROM asg_recommendations WHERE status='open';

-- current freshness
SELECT source, datetime(MIN(last_success_ts),'unixepoch') AS oldest_ok
FROM ingestion_status GROUP BY source;

-- top 5 ASGs by cost this month
SELECT asg_name, printf('$%,.0f', estimated_cost_usd) cost, peak_in_service
FROM asg_monthly_rollup WHERE month = strftime('%Y-%m','now')
ORDER BY estimated_cost_usd DESC LIMIT 5;
```

---

## 2. Puller — [app/pull_metrics.py](../app/pull_metrics.py)

**Only module** in the app that talks to AWS. The Flask dashboard reads from
SQLite exclusively — a CloudWatch outage doesn't affect page loads.

### What it does per run

1. `DescribeAutoScalingGroups` — list groups matching the discovery tag.
2. `DescribePolicies` per ASG — capture TT `TargetValue` and whether PS is on.
3. `UPSERT` into `asg_config`.
4. `GetMetricData` per ASG — 3 series at `Period=300`:
   - `AWS/AutoScaling · GroupInServiceInstances` (avg)
   - `AWS/AutoScaling · GroupDesiredCapacity` (avg)
   - `AWS/ApplicationELB · RequestCountPerTarget` summed over TGs (sum → per-min rate)
5. `GetPredictiveScalingForecast` per ASG where PS is enabled — next 48 h.
6. `UPSERT` into `asg_snapshots_5m` and `ingestion_status`.

### Configuration (all env vars — no code changes needed)

| Env var | Default | Purpose |
|---|---|---|
| `AWS_REGION` | (instance metadata) | Region for CloudWatch/ASG API calls |
| `ASG_INSIGHTS_DB` | `/var/lib/asg-insights/asg_insights.db` | SQLite file path |
| `ASG_DISCOVERY_TAG_KEY` | `Environment` | Which tag key identifies ASGs to monitor |
| `ASG_DISCOVERY_TAG_VAL` | `prod` | Which value that tag must have |
| `ASG_POLL_WINDOW_MIN` | `60` | Minutes to look back per poll |
| `ASG_METRIC_PERIOD_SEC` | `300` | Default CloudWatch aggregation period (overridden per-ASG by tag) |
| `ASG_RESOLUTION_TAG_KEY` | `AsgInsightsResolution` | Tag key that opts an ASG into high-res |
| `ASG_RESOLUTION_TAG_HIGH_VAL` | `1min` | Tag value that means "pull at Period=60" |

### Selective 1-min polling

Tag any ASG with `AsgInsightsResolution=1min` and the puller pulls it at
`Period=60`, storing rows in `asg_snapshots` (60-second granularity). All
other ASGs stay at `Period=300` in `asg_snapshots_5m`.

**Cost impact — essentially zero.** `GetMetricData` bills per metric in the
call, not per datapoint. Period 60 vs 300 costs identically; you just get
5× more datapoints back per API call. Storage grows ~5× for a high-res ASG
but SQLite tolerates that easily; the 7-day retention on 1-min rows
(after which they roll into 5-min) keeps the raw table bounded.

Enable it on one ASG:

```bash
aws autoscaling create-or-update-tags --tags \
  ResourceId=my-asg,ResourceType=auto-scaling-group,\
Key=AsgInsightsResolution,Value=1min,PropagateAtLaunch=false
```

Next puller run picks up the tag and starts writing 1-min rows. Disable by
removing the tag (or setting to `5min`) — the puller reverts on its next
discovery pass.

Verify what each ASG is set to:

```sql
SELECT asg_name, metric_period_sec FROM asg_config ORDER BY metric_period_sec, asg_name;
```

Set them per-service via `systemctl edit asg-puller.service` — see
[OPERATIONS.md §6](OPERATIONS.md#6-config-changes).

### Cost characteristics

`GetMetricData` is billed **per metric in the call, not per datapoint**. So
widening `ASG_POLL_WINDOW_MIN` is free; only call *frequency* affects the bill.

At 50 ASGs × ~5 metrics per poll = 250 metrics/call:

| Timer cadence | Polls/day | Metrics/month | Bill |
|---|---|---|---|
| Every 15 min | 96 | 720 k | ~$7.20 |
| Every 1 hour | 24 | 180 k | ~$1.80 |
| Every 6 h | 4 | 30 k | ~$0.30 |

Current default is **hourly** — 4× cheaper than 15-min, with 6× safety
margin over PS's true 6-h refresh cadence.

---

## 3. Rollup job — [app/rollup.py](../app/rollup.py)

Runs nightly. Four things, in order:

1. **`rollup_5m_from_1m()`** — aggregate 1-min raw rows (high-res ASGs)
   older than 7 days into 5-min buckets, then delete the aged 1-min rows.
2. **`rollup_1h()`** — aggregate 5-min buckets older than 30 days into
   1-hour buckets in `asg_snapshots_1h`, then delete the aged 5-min rows.
3. **`compute_monthly()`** — recompute `asg_monthly_rollup` for the current
   and previous month. Idempotent — safe to re-run.
4. **`regenerate_recommendations()`** — wipe `asg_recommendations` and
   regenerate open items from the last 30 days. Rules today:
   - **`reduce_util_gap`** — the headline cost recommendation. For a
     target-tracking ASG whose utilization (avg req-per-target ÷ TT target)
     runs below an 85 % ideal, estimate the "ideal" instance count
     (`current × util ÷ 0.85`), the instances that can be shed, and the
     monthly dollar saving at the **AWS Pricing API** on-demand rate.
   - **`lower_min`** — p95 utilization < 40 % and min_size > 2 → suggest
     dropping the floor and estimate the savings.
   - **`enable_ps`** — no Predictive Scaling policy attached.
   - **`enable_scale_in`** — target-tracking policy has `DisableScaleIn=true`.

Retention constants at the top of `rollup.py`:

| Constant | Default | Behavior |
|---|---|---|
| `RETENTION_1M` | 180 days (6 months) | 1-min raw rows older than this roll into 5-min then delete |
| `RETENTION_5M` | 365 days (1 year) | 5-min buckets older than this roll into 1-hour then delete |

1-hour rows are never deleted — they're small enough to keep forever.
See [OPERATIONS.md §12](OPERATIONS.md#12-capacity-planning) for the storage math these retentions imply.

---

## 4. Scheduler — systemd

Two timers, both in [systemd/](../systemd/) and installed by
[scripts/user_data.sh](../scripts/user_data.sh):

### `asg-puller.timer`

```ini
[Timer]
OnBootSec=2min
OnUnitActiveSec=1h
AccuracySec=1min
```

Fires 2 min after boot, then every hour on the hour (± 1 min). Runs
`asg-puller.service`, a `Type=oneshot` unit that executes
`python -m app.pull_metrics` as user `asginsights`.

### `asg-rollup.timer`

```ini
[Timer]
OnCalendar=*-*-* 03:15:00 UTC
Persistent=true
```

Fires at 03:15 UTC daily. `Persistent=true` means if the instance was down at
that time, the rollup runs at next boot. Runs
`asg-rollup.service`, a `Type=oneshot` unit that executes
`python -m app.rollup` as `asginsights`.

### Inspecting the schedule

```bash
systemctl list-timers 'asg-*' --no-pager
journalctl -u asg-puller.service --since '24 hours ago' --no-pager
journalctl -u asg-rollup.service --since '7 days ago' --no-pager
```

### Changing the schedule

Preferred: `systemctl edit asg-puller.timer` (creates a drop-in override that
survives package updates). Example — poll every 30 min:

```ini
[Timer]
OnUnitActiveSec=      # clear the base value
OnUnitActiveSec=30min
```

Then `systemctl daemon-reload && systemctl restart asg-puller.timer`.

### Force a run right now

```bash
sudo systemctl start asg-puller.service    # or asg-rollup.service
sudo journalctl -u asg-puller.service -n 40 --no-pager
```

---

## 5. Data flow at a glance

```
CloudWatch  ──────────► pull_metrics.py ─────► asg_snapshots_5m
    ▲           hourly                  │              │
    │                                   │              │ nightly at 03:15 UTC
    │                                   │              ▼
    │                                   │      asg_snapshots_1h  (>30 days old)
    │                                   │              │
AutoScaling ─────────► pull_metrics.py ─┤              │
    (Describe*)         (asg_config,    ├─────► asg_monthly_rollup
                         freshness)     │
                                        └─────► asg_recommendations
                                                       │
                                                       ▼
                                                Flask + Jinja + ECharts
                                                       │
                                                    Browser
                                                       (SSM port-forward
                                                        or private-zone
                                                        DNS record —
                                                        see DNS_ACCESS.md)
```

Every arrow labeled `hourly` or `nightly` is a systemd timer. Every arrow
into `asg_snapshots_*` is followed by a write to `ingestion_status` so the
UI can display the last-updated time per source.
