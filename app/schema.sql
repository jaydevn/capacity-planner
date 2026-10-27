-- asg-insights SQLite schema. Times are UTC epoch seconds.

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS asg_config (
  region            TEXT NOT NULL,
  asg_name          TEXT NOT NULL,
  instance_type     TEXT,
  min_size          INTEGER,
  max_size          INTEGER,
  tt_target         REAL,
  ps_enabled        INTEGER,
  scale_in_enabled  INTEGER,
  tg_arns           TEXT,
  tags              TEXT,
  first_seen        INTEGER NOT NULL,
  last_seen         INTEGER NOT NULL,
  hourly_rate_usd   REAL,
  metric_period_sec INTEGER DEFAULT 300,
  PRIMARY KEY (region, asg_name)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS asg_snapshots (
  region            TEXT NOT NULL,
  ts                INTEGER NOT NULL,
  asg_name          TEXT NOT NULL,
  in_service        INTEGER,
  desired           INTEGER,
  alb_rcpt          REAL,
  ps_predicted      INTEGER,
  tt_high_state     TEXT,
  tt_low_state      TEXT,
  cost_hourly_usd   REAL,
  PRIMARY KEY (region, ts, asg_name)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_snapshots_asg_ts
  ON asg_snapshots (region, asg_name, ts);

CREATE TABLE IF NOT EXISTS asg_snapshots_5m (
  region            TEXT NOT NULL,
  ts                INTEGER NOT NULL,
  asg_name          TEXT NOT NULL,
  in_service_avg    REAL,
  in_service_max    INTEGER,
  desired_avg       REAL,
  alb_rcpt_avg      REAL,
  alb_rcpt_max      REAL,
  ps_predicted_avg  REAL,
  cost_hourly_usd   REAL,
  PRIMARY KEY (region, ts, asg_name)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_snapshots_5m_asg_ts
  ON asg_snapshots_5m (region, asg_name, ts);

CREATE TABLE IF NOT EXISTS asg_snapshots_1h (
  region            TEXT NOT NULL,
  ts                INTEGER NOT NULL,
  asg_name          TEXT NOT NULL,
  in_service_avg    REAL,
  in_service_max    INTEGER,
  desired_avg       REAL,
  alb_rcpt_avg      REAL,
  alb_rcpt_max      REAL,
  ps_predicted_avg  REAL,
  cost_hourly_usd   REAL,
  PRIMARY KEY (region, ts, asg_name)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_snapshots_1h_asg_ts
  ON asg_snapshots_1h (region, asg_name, ts);

CREATE TABLE IF NOT EXISTS asg_monthly_rollup (
  region             TEXT NOT NULL,
  month              TEXT NOT NULL,
  asg_name           TEXT NOT NULL,
  avg_in_service     REAL,
  peak_in_service    INTEGER,
  p95_utilization    REAL,
  over_prov_hours    REAL,
  sla_risk_minutes   INTEGER,
  estimated_cost_usd REAL,
  prior_month_delta  REAL,
  computed_at        INTEGER NOT NULL,
  PRIMARY KEY (region, month, asg_name)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS asg_recommendations (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  region                TEXT NOT NULL,
  asg_name              TEXT NOT NULL,
  category              TEXT NOT NULL,
  headline              TEXT NOT NULL,
  detail                TEXT,
  estimated_savings_usd REAL,
  confidence            TEXT,
  generated_at          INTEGER NOT NULL,
  status                TEXT DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS ix_recs_asg
  ON asg_recommendations (region, asg_name, status);

-- Target groups attached to each ASG, resolved from elbv2 DescribeTargetGroups.
CREATE TABLE IF NOT EXISTS asg_target_groups (
  region            TEXT NOT NULL,
  asg_name          TEXT NOT NULL,
  tg_arn            TEXT NOT NULL,
  tg_name           TEXT,
  protocol          TEXT,
  port              INTEGER,
  target_type       TEXT,
  lb_arns           TEXT,              -- JSON list of attached load balancer ARNs
  updated_at        INTEGER NOT NULL,
  PRIMARY KEY (region, asg_name, tg_arn)
) WITHOUT ROWID;

-- On-demand hourly price per (region, instance_type), cached from the AWS
-- Pricing API by the poller so the read-only UI never calls AWS directly.
CREATE TABLE IF NOT EXISTS pricing_cache (
  region            TEXT NOT NULL,
  instance_type     TEXT NOT NULL,
  hourly_rate_usd   REAL NOT NULL,
  source            TEXT,              -- 'pricing_api' | 'fallback'
  updated_at        INTEGER NOT NULL,
  PRIMARY KEY (region, instance_type)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS ingestion_status (
  region                TEXT NOT NULL,
  asg_name              TEXT NOT NULL,
  source                TEXT NOT NULL,
  last_success_ts       INTEGER,
  last_success_data_ts  INTEGER,
  last_error_ts         INTEGER,
  last_error_msg        TEXT,
  PRIMARY KEY (region, asg_name, source)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_ingest_source ON ingestion_status (source);
