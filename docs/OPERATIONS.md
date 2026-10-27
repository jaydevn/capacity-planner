# asg-insights — operations runbook

Everything you need to run, monitor, change, back up, and rebuild this app.

All commands below use two placeholders — set them once in your shell and
paste-run the rest. Everything is generic; nothing about a specific customer
environment is baked in.

```bash
# One-time in each shell (or add to ~/.zshrc / ~/.bashrc):
export AWS_PROFILE=default
export AWS_REGION=us-east-1
export STACK=asg-insights
export ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export BUCKET=${STACK}-src-${ACCOUNT}
```

## 1. What runs where

```
Laptop                            EC2 (t3.medium, private subnet)
------                            ---------------------------------
aws cli + deploy.sh          →    /opt/asg-insights/        (source, updated by s3 sync)
                                  /var/lib/asg-insights/    (SQLite DB, WAL files)
                                  /var/log/asg-insights-bootstrap.log

                                  Services:
                                  - asg-insights.service    (gunicorn on 127.0.0.1:8000)
                                  - nginx                   (proxy :80 → :8000)
                                  - asg-puller.timer        (hourly CW + PS pull)
                                  - asg-rollup.timer        (03:15 UTC nightly)
```

Everything is read-only from AWS's perspective. The instance never modifies
ASGs, ALBs, or policies.

## 2. First-time setup

Prerequisites on the laptop:

```bash
aws --version                          # v2 recommended
aws sts get-caller-identity   # confirm you're on the right account
brew install --cask session-manager-plugin      # for SSM port-forward
```

Deploy:

```bash
cd /path/to/claude-output
./scripts/deploy.sh
```

This will:

1. Create the S3 source bucket if missing (encryption + PAB + versioning).
2. `s3 sync` current directory into the bucket.
3. Discover the default VPC + a subnet in `us-east-1a`.
4. Validate the CloudFormation template.
5. Deploy the stack with `InstanceType=t3.medium`.
6. Print stack outputs on success; on failure, dump the last 20
   `describe-events` rows automatically.

Env-var overrides:
- `INSTANCE_TYPE=m5.large` — bigger box
- `PREFERRED_AZ=us-east-1b` — pick a different AZ
- `ALLOWED_CIDR=1.2.3.4/32` — restrict who can reach :80 inside the VPC
- `AWS_REGION=us-west-2` — deploy elsewhere

Post-deploy: wait ~5 min for UserData to finish (it apt-installs, pip-installs,
enables services). Verify:

```bash
INST=$(aws cloudformation describe-stack-resource \
  --stack-name asg-insights --logical-resource-id Dashboard \
  --query 'StackResourceDetail.PhysicalResourceId' --output text)

aws ssm start-session --target "$INST"
# then:  sudo tail -20 /var/log/asg-insights-bootstrap.log
#        systemctl status asg-insights.service --no-pager
```

## 3. Accessing the dashboard

The dashboard is served by an internet-facing ALB. Get its URL and browse to it:

```bash
aws cloudformation describe-stacks --stack-name asg-insights \
  --query "Stacks[0].Outputs[?OutputKey=='DashboardUrl'].OutputValue" --output text
# open the printed http://<alb-dns-name>/ URL
```

Who can reach it is governed by `ALLOWED_CIDR` on the ALB security group (set
at deploy time). The app itself has no authentication — keep `ALLOWED_CIDR`
tight and/or add OIDC/Cognito on the ALB listener before exposing real data.

Prefer to keep access internal? SSM port-forward still works:

```bash
INST=$(aws cloudformation describe-stack-resource \
  --stack-name asg-insights --logical-resource-id Dashboard \
  --query 'StackResourceDetail.PhysicalResourceId' --output text)

aws ssm start-session --target "$INST" \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["80"],"localPortNumber":["8080"]}'
```

Then open http://127.0.0.1:8080/. Leave the SSM session running while you use
the dashboard; Ctrl-C closes the tunnel.

To open a shell instead of a port-forward:

```bash
aws ssm start-session --target "$INST"
sudo -i    # become root once — no password prompts
```

## 4. Monitoring the poller

**In the browser** — every page has a "Data freshness" strip. Each source
(ASG config, CloudWatch metrics, PS forecast, TT policy) shows how long since
its last successful pull. Green means < 30 min; red means older or never.

The ASG detail page shows the same per-ASG in a 4-card grid.

**On the instance** — check the timer and recent runs:

```bash
sudo systemctl list-timers asg-puller.timer --no-pager
sudo journalctl -u asg-puller.service -n 100 --no-pager
sudo journalctl -u asg-puller.service --since '24 hours ago' --no-pager | grep -i error
```

**In SQLite** — full freshness snapshot:

```bash
sudo -u asginsights sqlite3 /var/lib/asg-insights/asg_insights.db \
  "SELECT asg_name, source, datetime(last_success_ts,'unixepoch') AS last_ok, \
          last_error_msg FROM ingestion_status ORDER BY asg_name, source;"
```

## 5. Running the poller manually

Force a pull now (as root on the instance):

```bash
systemctl start asg-puller.service       # runs once, exits
journalctl -u asg-puller.service -n 40 --no-pager
```

Run with a wider window (e.g. backfill 6 hours):

```bash
cd /opt/asg-insights
sudo -u asginsights ASG_POLL_WINDOW_MIN=360 \
  ASG_INSIGHTS_DB=/var/lib/asg-insights/asg_insights.db \
  /opt/asg-insights/.venv/bin/python -m app.pull_metrics
```

## 6. Config changes

Everything the puller cares about is env vars declared in the service unit.
Edit them without touching the code.

### Change discovery tag (e.g. `env=production` instead of `Environment=prod`)

On the instance:

```bash
sudo systemctl edit asg-puller.service
# in the editor, add:
[Service]
Environment=ASG_DISCOVERY_TAG_KEY=env
Environment=ASG_DISCOVERY_TAG_VAL=production

sudo systemctl daemon-reload
sudo systemctl start asg-puller.service
```

This creates `/etc/systemd/system/asg-puller.service.d/override.conf` — it
survives package updates.

### Change poll frequency

Edit `/etc/systemd/system/asg-puller.timer` (or push a new version to S3 and
re-sync). Then:

```bash
sudo systemctl daemon-reload
sudo systemctl restart asg-puller.timer
sudo systemctl list-timers asg-puller.timer --no-pager
```

Or override just the value:

```bash
sudo systemctl edit asg-puller.timer
# in the editor:
[Timer]
OnUnitActiveSec=            # clear the base value
OnUnitActiveSec=30min       # your new cadence

sudo systemctl daemon-reload
sudo systemctl restart asg-puller.timer
```

### Change discovery/poll knobs without a restart

```bash
cd /opt/asg-insights
sudo -u asginsights \
  ASG_DISCOVERY_TAG_KEY=env ASG_DISCOVERY_TAG_VAL=stage \
  /opt/asg-insights/.venv/bin/python -m app.pull_metrics
```

### Change retention

Edit `RETENTION_5M` in [app/rollup.py](../app/rollup.py) (default 30 days),
sync to S3, then re-sync on the instance. Next nightly run picks up the change.

### Change hourly pricing table

[app/pull_metrics.py](../app/pull_metrics.py) has a hardcoded
`HOURLY_RATE_USD` dict. Add rows for any new instance families you run.
For accurate rates use the AWS Pricing API — TODO in the code.

## 7. Pushing code updates

Full workflow from laptop to running instance:

```bash
# 1. Sync source to S3
cd /path/to/claude-output
aws s3 sync . \
  s3://${BUCKET}/ \
  --exclude ".venv/*" --exclude "__pycache__/*" --exclude "*.db*" \
  --exclude ".claude/*" --exclude ".git/*" --exclude ".DS_Store"

# 2. Pull on the instance (SSM shell as root)
cd /opt/asg-insights
aws s3 sync s3://${BUCKET}/ /opt/asg-insights/ \
  --delete --exclude "*.db*"
chown -R asginsights:asginsights /opt/asg-insights

# 3. If Python code changed:
systemctl restart asg-insights.service

# 4. If systemd unit files changed:
install -m 0644 /opt/asg-insights/systemd/*.service /etc/systemd/system/
install -m 0644 /opt/asg-insights/systemd/*.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl restart asg-puller.timer asg-rollup.timer asg-insights.service

# 5. If requirements.txt changed:
sudo -u asginsights /opt/asg-insights/.venv/bin/pip install -r requirements.txt
systemctl restart asg-insights.service
```

For schema migrations, add DDL to [app/schema.sql](../app/schema.sql) —
`db.init_db()` runs at Flask startup and executes the file idempotently
(`CREATE TABLE IF NOT EXISTS`). Restart `asg-insights.service` to trigger.

## 8. Backup and restore

The whole app state is one SQLite file:
`/var/lib/asg-insights/asg_insights.db` (plus its WAL companions
`asg_insights.db-wal`, `asg_insights.db-shm`).

### Ad-hoc snapshot

```bash
DATE=$(date -u +%Y%m%d-%H%M%S)
sudo -u asginsights sqlite3 /var/lib/asg-insights/asg_insights.db \
  ".backup /tmp/asg_insights.$DATE.db"
gzip /tmp/asg_insights.$DATE.db
aws s3 cp /tmp/asg_insights.$DATE.db.gz \
  s3://${BUCKET}/backups/asg_insights.$DATE.db.gz
rm /tmp/asg_insights.$DATE.db.gz
```

`.backup` is atomic and locks the DB only briefly — safe to run while the app
is serving traffic.

### Scheduled daily backup (recommended)

Drop this on the instance:

```bash
sudo tee /usr/local/bin/asg-insights-backup.sh >/dev/null <<'SH'
#!/bin/bash
set -euo pipefail
DATE=$(date -u +%Y%m%d)
DB=/var/lib/asg-insights/asg_insights.db
OUT=/tmp/asg_insights.$DATE.db.gz
sudo -u asginsights sqlite3 "$DB" ".backup /tmp/asg_insights.$DATE.db"
gzip -f /tmp/asg_insights.$DATE.db
aws s3 cp "$OUT" s3://${BUCKET}/backups/ \
  --storage-class STANDARD_IA
rm -f "$OUT"
SH
sudo chmod +x /usr/local/bin/asg-insights-backup.sh

sudo tee /etc/systemd/system/asg-insights-backup.service >/dev/null <<'UNIT'
[Unit]
Description=Daily SQLite snapshot to S3
[Service]
Type=oneshot
ExecStart=/usr/local/bin/asg-insights-backup.sh
UNIT

sudo tee /etc/systemd/system/asg-insights-backup.timer >/dev/null <<'UNIT'
[Unit]
Description=Run asg-insights-backup daily at 02:00 UTC
[Timer]
OnCalendar=*-*-* 02:00:00 UTC
Persistent=true
Unit=asg-insights-backup.service
[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now asg-insights-backup.timer
```

Bucket versioning is already enabled by `deploy.sh`, so overwrites and
accidental deletes are recoverable for 30 days (see the noncurrent-version
lifecycle rule).

### Restore

Replace the current DB atomically. Stop writers first:

```bash
sudo systemctl stop asg-insights.service asg-puller.timer asg-rollup.timer

sudo aws s3 cp s3://${BUCKET}/backups/asg_insights.YYYYMMDD.db.gz /tmp/
sudo gunzip /tmp/asg_insights.YYYYMMDD.db.gz
sudo -u asginsights cp /tmp/asg_insights.YYYYMMDD.db /var/lib/asg-insights/asg_insights.db
sudo rm -f /var/lib/asg-insights/asg_insights.db-wal \
           /var/lib/asg-insights/asg_insights.db-shm

sudo systemctl start asg-insights.service asg-puller.timer asg-rollup.timer
```

## 9. Troubleshooting playbook

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| Dashboard shows 404 | nginx not reloaded after config drop | `curl -v http://127.0.0.1/`; `ls /etc/nginx/sites-enabled/` | `ln -sf /etc/nginx/sites-available/asg-insights /etc/nginx/sites-enabled/; systemctl reload nginx` |
| Dashboard shows 502 | gunicorn down | `systemctl status asg-insights.service`; `journalctl -u asg-insights.service -n 50` | Fix Python error, `systemctl restart asg-insights.service` |
| All freshness badges red | puller stopped | `systemctl list-timers asg-puller.timer`; `journalctl -u asg-puller.service` | `systemctl start asg-puller.timer asg-puller.service` |
| One source red | permission/API error | Look at `last_error_msg` in `ingestion_status` | Fix IAM policy or the specific ASG's config |
| "discovered 0 ASGs" | Tag mismatch | `aws autoscaling describe-auto-scaling-groups --query '...Tags'` | See section 6 – change discovery tag |
| Bootstrap hangs on apt | dpkg lock held by cloud-init | `sudo tail -f /var/log/asg-insights-bootstrap.log` | Wait for the 60x5s retry loop (fixed in current `user_data.sh`); or restart the instance |
| Disk filling up | SQLite growing, rollup not running | `df -h /var/lib`; `du -h /var/lib/asg-insights/` | `systemctl start asg-rollup.service` — should reclaim after retention cutoff |
| Freshness "never" for cw_metrics | first pull hasn't run | Wait; or `systemctl start asg-puller.service` | — |
| SSM session asks for password | ssm-user sudoers file missing | `cat /etc/sudoers.d/ssm-agent-users` | Reboot the instance so SSM agent re-drops the file |
| Stack update fails with S3 collision | Trying to create bucket that exists | — | Bucket is now managed by `deploy.sh`, not CFN — verify the template doesn't declare an S3 bucket resource |

## 10. Rotating the instance (patching, AMI refresh)

The DB lives on EBS. Terminating the instance destroys it unless you snapshot
first. Two options.

### Simple: back up, delete, redeploy

```bash
# from the instance
/usr/local/bin/asg-insights-backup.sh   # or run the ad-hoc block above

# from your laptop
aws cloudformation delete-stack --stack-name asg-insights
aws cloudformation wait stack-delete-complete --stack-name asg-insights
./scripts/deploy.sh

# then SSM in and restore from S3 (section 8)
```

### Zero-downtime-ish: swap EBS volume

Take an EBS snapshot, launch a new instance in the same AZ, detach & re-attach
the volume, update DNS or the SSM target. Overkill for a leadership dashboard;
listed for completeness.

## 11. Teardown

```bash
# 1. Final backup (optional but recommended)
aws ssm start-session --target "$INST"
sudo /usr/local/bin/asg-insights-backup.sh
exit

# 2. Delete the stack (EC2, IAM, SG go away; S3 bucket is Retained by CFN, so preserved)
aws cloudformation delete-stack --stack-name asg-insights
aws cloudformation wait stack-delete-complete --stack-name asg-insights

# 3. If you also want to delete the source bucket + backups
aws s3 rm s3://${BUCKET} --recursive
aws s3api delete-bucket --bucket ${BUCKET}
```

## 12. Capacity planning — how much data before SQLite gets weird

Row sizes with WAL + our indexes:

| Table | Bytes/row | Rows/day per ASG |
|---|---|---|
| `asg_snapshots` (1-min) | ~100 | 1,440 |
| `asg_snapshots_5m`      | ~120 |   288 |
| `asg_snapshots_1h`      | ~120 |    24 |

Retentions (defaults in [app/rollup.py](../app/rollup.py)):

| Table | Retention |
|---|---|
| `asg_snapshots` (1-min) | **180 days (6 months)** then rolls into 5-min |
| `asg_snapshots_5m`      | **365 days (1 year)** then rolls into 1-hour |
| `asg_snapshots_1h`      | forever |

### Storage at 140 ASGs

**Case A — all 140 ASGs tagged high-res (1-min for everyone):**

| Window (steady state) | Rows | Storage |
|---|---|---|
| 180 d of 1-min (140 ASGs) | 140 × 1,440 × 180 = **36.3 M** | ~3.6 GB |
| 365 d of 5-min (140 ASGs) | 140 × 288 × 365 = **14.7 M** | ~1.8 GB |
| 1 y of 1-hour (140 ASGs, grows 150 MB/yr) | 1.2 M | ~150 MB |
| Indexes + WAL overhead (~25 %) | — | ~1.4 GB |
| **Total steady state** | — | **~7 GB** |
| **After 5 years** | — | **~7.6 GB** |

**Case B — only ~30 % of ASGs tagged high-res (recommended default):**

| Window (steady state) | Rows | Storage |
|---|---|---|
| 180 d of 1-min (42 ASGs)  | 10.9 M | ~1.1 GB |
| 365 d of 5-min (140 ASGs) | 14.7 M | ~1.8 GB |
| 1 y of 1-hour + overhead | — | ~700 MB |
| **Total** | — | **~3.6 GB** |

Either case fits comfortably on the default **20 GiB gp3** volume for years.
Bump to 30 GiB in `infra/cloudformation.yaml` (`VolumeSize: 30`) if you want
extra margin.

### Practical thresholds

- **DB > 5 GB**: `VACUUM` takes minutes. Schedule weekly off-hours instead
  of relying on `PRAGMA auto_vacuum`:
  ```bash
  sudo tee /etc/systemd/system/asg-insights-vacuum.service >/dev/null <<'UNIT'
  [Unit]
  Description=Weekly SQLite VACUUM
  [Service]
  Type=oneshot
  ExecStart=/usr/bin/sudo -u asginsights /usr/bin/sqlite3 \
    /var/lib/asg-insights/asg_insights.db "PRAGMA wal_checkpoint(TRUNCATE); VACUUM;"
  UNIT
  sudo tee /etc/systemd/system/asg-insights-vacuum.timer >/dev/null <<'UNIT'
  [Timer]
  OnCalendar=Sun *-*-* 04:00:00 UTC
  Persistent=true
  [Install]
  WantedBy=timers.target
  UNIT
  sudo systemctl daemon-reload
  sudo systemctl enable --now asg-insights-vacuum.timer
  ```
- **DB > 20 GB**: consider PostgreSQL, or partition by year.
- **WAL file > 500 MB**: means auto-checkpoint isn't firing — check for a
  long-running reader (`journalctl -u asg-insights.service`).
- **Disk > 85 % full**: alert on `du -h /var/lib/asg-insights/`, or resize
  the EBS volume (`aws ec2 modify-volume`, then `sudo resize2fs` on the
  instance) — no downtime.
- **`.backup` writes to /tmp**: at 7 GB, `/tmp` needs headroom. If tight,
  stream directly: `sqlite3 $DB .dump | gzip > /tmp/dump.sql.gz` instead of
  `.backup`.

### Instance sizing

RAM matters more than CPU for SQLite query speed:

| Fleet size | Recommended instance | Reason |
|---|---|---|
| ≤ 50 ASGs | `t3.small` (2 GiB) | tiny |
| 50–150 ASGs | `t3.medium` (4 GiB) — **current default** | fits SQLite page cache for the hot working set |
| 150–500 ASGs | `t3.large` (8 GiB) | 1-year 5-min table starts to exceed cache on t3.medium |
| > 500 ASGs | `m5.large` or reconsider PostgreSQL | at this scale a bigger DB engine pays off |

## 13. Upgrading the instance without losing data

The DB lives on the EC2's root EBS volume. Changing `InstanceType` on an
`AWS::EC2::Instance` is a **"Some interruptions"** update in CloudFormation
— the instance is stopped, the type is modified, and the instance is
started. Volume, private IP, and IAM role all persist. Total downtime
~2 minutes.

### Preferred: update via CloudFormation

```bash
INSTANCE_TYPE=t3.large ./scripts/deploy.sh
```

`deploy.sh` re-runs `cloudformation deploy` with the new parameter. CFN
sees only the instance type has changed and does the stop/modify/start.

### Alternative: raw EC2 API (bypass CFN — introduces drift)

Only if the stack is otherwise broken:

```bash
INST=$(aws cloudformation describe-stack-resource \
  --stack-name "$STACK" --logical-resource-id Dashboard \
  --query 'StackResourceDetail.PhysicalResourceId' --output text)

aws ec2 stop-instances --instance-ids "$INST"
aws ec2 wait instance-stopped --instance-ids "$INST"
aws ec2 modify-instance-attribute --instance-id "$INST" \
  --instance-type '{"Value":"t3.large"}'
aws ec2 start-instances --instance-ids "$INST"
```

CFN will show drift on the next `describe-stack-drift` call — re-run
`deploy.sh` with the new type to sync the template.

### Verifying nothing was lost

```bash
aws ssm start-session --target "$INST"
sudo -i
sudo -u asginsights sqlite3 /var/lib/asg-insights/asg_insights.db \
  "SELECT COUNT(*) FROM asg_snapshots_1h;"    # should match the pre-upgrade count
systemctl status asg-insights.service asg-puller.timer nginx --no-pager
```

## 14. Resilience — automated backup and restore

Section 8 has the manual snapshot + restore procedure. This section adds
the **automated** side: daily backups on the instance, plus a boot-time
auto-restore for when you rebuild the instance from scratch.

### 14a. Daily backup (install once)

Uses the systemd timer from §8 — recap in one block for convenience.
Run on the instance as root:

```bash
sudo tee /usr/local/bin/asg-insights-backup.sh >/dev/null <<'SH'
#!/bin/bash
set -euo pipefail
DATE=$(date -u +%Y%m%d)
BUCKET="$(cat /etc/asg-insights/bucket)"   # written by user_data.sh
DB=/var/lib/asg-insights/asg_insights.db
OUT=/tmp/asg_insights.$DATE.db.gz
sudo -u asginsights sqlite3 "$DB" ".backup /tmp/asg_insights.$DATE.db"
gzip -f /tmp/asg_insights.$DATE.db
aws s3 cp "$OUT" "s3://$BUCKET/backups/latest.db.gz" \
  --storage-class STANDARD
aws s3 cp "$OUT" "s3://$BUCKET/backups/asg_insights.$DATE.db.gz" \
  --storage-class STANDARD_IA
rm -f "$OUT"
SH
sudo chmod +x /usr/local/bin/asg-insights-backup.sh
sudo mkdir -p /etc/asg-insights
echo "${STACK}-src-$(aws sts get-caller-identity --query Account --output text)" \
  | sudo tee /etc/asg-insights/bucket >/dev/null

sudo tee /etc/systemd/system/asg-insights-backup.service >/dev/null <<'UNIT'
[Unit]
Description=Daily SQLite snapshot to S3
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/bin/asg-insights-backup.sh
UNIT

sudo tee /etc/systemd/system/asg-insights-backup.timer >/dev/null <<'UNIT'
[Unit]
Description=Run asg-insights-backup daily at 02:00 UTC
[Timer]
OnCalendar=*-*-* 02:00:00 UTC
Persistent=true
Unit=asg-insights-backup.service
[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now asg-insights-backup.timer
```

Two S3 objects per day: a rotating `latest.db.gz` (STANDARD, fast to grab
on rebuild) and a dated `asg_insights.YYYYMMDD.db.gz` (STANDARD_IA, ~$0.0125/GB/mo).
Bucket versioning is already on, so accidental deletes are recoverable for
30 days.

Add a **retention lifecycle** so the dated backups don't grow forever:

```bash
aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" \
  --lifecycle-configuration '{"Rules":[{
    "ID":"expire-old-backups",
    "Prefix":"backups/asg_insights.",
    "Status":"Enabled",
    "Expiration":{"Days":90}
  }]}'
```

### 14b. Restore on a fresh instance (opt-in)

Add this to `scripts/user_data.sh` **or** run once after a fresh deploy.
It restores `latest.db.gz` from S3 **only if the local DB doesn't already
exist** — safe to re-run:

```bash
DB=/var/lib/asg-insights/asg_insights.db
BUCKET="$(cat /etc/asg-insights/bucket 2>/dev/null || true)"
if [[ -n "$BUCKET" && ! -s "$DB" ]] && \
   aws s3api head-object --bucket "$BUCKET" --key backups/latest.db.gz 2>/dev/null; then
  echo "restoring DB from s3://$BUCKET/backups/latest.db.gz"
  aws s3 cp "s3://$BUCKET/backups/latest.db.gz" /tmp/latest.db.gz
  gunzip /tmp/latest.db.gz
  sudo -u asginsights mv /tmp/latest.db "$DB"
  systemctl restart asg-insights.service
fi
```

Wire it into `user_data.sh` right before `systemctl enable --now
asg-insights.service ...` so first boot picks up yesterday's backup.

### 14c. Alerting on backup failure

Simplest — CloudWatch Logs subscription on the systemd journal, filter for
`asg-insights-backup.service` + `FAILED`. Or a nightly cron that checks
S3 for a fresh object:

```bash
sudo tee /usr/local/bin/asg-insights-backup-check.sh >/dev/null <<'SH'
#!/bin/bash
BUCKET="$(cat /etc/asg-insights/bucket)"
last=$(aws s3api head-object --bucket "$BUCKET" --key backups/latest.db.gz \
       --query 'LastModified' --output text 2>/dev/null || echo "MISSING")
now=$(date -u +%s)
last_ts=$(date -u -d "$last" +%s 2>/dev/null || echo 0)
if (( now - last_ts > 90000 )); then    # 25 h
  echo "asg-insights backup stale: $last" | \
    aws sns publish --topic-arn "$SNS_TOPIC" --subject "asg-insights backup stale" --message file:///dev/stdin
fi
SH
```

Bind to an SNS topic that emails your ops list.

### 14d. Full recovery drill (do this before you need it)

Once a quarter:

1. Note the row counts and freshness on the running dashboard.
2. Deploy a second stack (`STACK_NAME=asg-insights-drill ./scripts/deploy.sh`).
3. Copy the source bucket's `backups/latest.db.gz` into the new stack's
   bucket, or configure the new stack to read from the primary bucket.
4. SSH in, run the auto-restore block from §14b.
5. Verify row counts and freshness match.
6. Tear down (`aws cloudformation delete-stack --stack-name asg-insights-drill`).

If step 5 fails you learn it in a drill, not in an incident.

## 15. What-if: EC2 fails a status check and gets replaced

### What happens to the SQLite data

Depends on **which** kind of failure and **who** replaces the instance.
Three scenarios, in order of severity:

| Scenario | Volume survives? | Data lost |
|---|---|---|
| **A. System status check fails (hardware)** — AWS auto-recovery migrates the same instance to new hardware | ✅ yes — same EBS attached | none |
| **B. Instance status check fails (OS-level)** — you stop, fix, start | ✅ yes — EBS is persistent by design | none unless you also terminate |
| **C. Instance terminated & replaced** (CFN update-replace, manual, ASG replacement) | ❌ **NO** — our template sets `DeleteOnTermination: true` on the root volume | everything in SQLite since the last backup |

The current template ([infra/cloudformation.yaml](../infra/cloudformation.yaml))
uses `DeleteOnTermination: true`. That's the AWS default and keeps the stack
clean, but it means **Scenario C loses data unless there's a backup**.

### RPO (recovery point objective)

Determined by how recent your S3 backup is:

| With daily backup only (§14a default) | **RPO ≤ 24 h** |
| With daily + hourly backup (add the timer below) | **RPO ≤ 1 h** |
| With EBS snapshots via Data Lifecycle Manager | **RPO = snapshot cadence** |

For a monthly-review dashboard, losing up to 24 h is usually fine — the
puller refills the current window in one poll. If you also want the last
24 h of high-res 1-min drilldown to survive an instance replacement, add
hourly backups.

### Improve RPO to 1 hour — hourly backup timer

Add on the instance (runs alongside the nightly `asg-insights-backup.timer`):

```bash
sudo tee /etc/systemd/system/asg-insights-backup-hourly.timer >/dev/null <<'UNIT'
[Unit]
Description=Hourly SQLite snapshot to S3 (light — overwrites latest.db.gz only)
[Timer]
OnCalendar=hourly
Persistent=true
Unit=asg-insights-backup.service
[Install]
WantedBy=timers.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now asg-insights-backup-hourly.timer
```

Only `backups/latest.db.gz` is refreshed hourly — the dated
`asg_insights.YYYYMMDD.db.gz` stays daily. Cost: 24 × 1.4 GB S3 PUT/day ≈
$0.10/mo.

### Enable EC2 auto-recovery (Scenario A)

Recovery-on-status-check is a free CloudWatch alarm. Add on the instance
or wire into the CloudFormation template:

```bash
INST=$(aws cloudformation describe-stack-resource \
  --stack-name "$STACK" --logical-resource-id Dashboard \
  --query 'StackResourceDetail.PhysicalResourceId' --output text)

aws cloudwatch put-metric-alarm \
  --alarm-name "asg-insights-status-check-recover" \
  --alarm-description "Auto-recover on system status check failure" \
  --metric-name StatusCheckFailed_System --namespace AWS/EC2 \
  --statistic Maximum --period 60 --evaluation-periods 2 \
  --comparison-operator GreaterThanOrEqualToThreshold --threshold 1 \
  --dimensions "Name=InstanceId,Value=$INST" \
  --alarm-actions "arn:aws:automate:$AWS_REGION:ec2:recover"
```

Now AWS auto-migrates the instance if the physical host fails — no manual
intervention, EBS + private IP preserved.

### Recovery drill

Simulate Scenario C:

```bash
# 1. Note current row counts on the running dashboard.
sudo -u asginsights sqlite3 /var/lib/asg-insights/asg_insights.db \
  "SELECT 'snap_5m',COUNT(*) FROM asg_snapshots_5m UNION ALL SELECT 'snap_1h',COUNT(*) FROM asg_snapshots_1h;"

# 2. Trigger a stack update-replace by intentionally terminating and letting
#    CFN recreate. WARNING: destructive — do in a non-prod stack.
aws ec2 terminate-instances --instance-ids "$INST"
aws cloudformation deploy --stack-name "$STACK" ...    # UserData auto-restore kicks in

# 3. Wait for bootstrap; verify row counts:
sudo -u asginsights sqlite3 /var/lib/asg-insights/asg_insights.db \
  "SELECT MAX(ts),datetime(MAX(ts),'unixepoch') FROM asg_snapshots_5m;"
# newest_ts should be close to the last hourly backup timestamp
```

If it works, your RPO is what the auto-restore recovers. If it doesn't,
check §14b's auto-restore hook in `user_data.sh`.

### Preventing Scenario C entirely (optional)

Two ways to make the volume outlive the instance so replacements don't
destroy data:

1. **Change the template**: set `DeleteOnTermination: false` on the root
   volume. Next CFN drift/replace preserves the volume — you can detach it
   from the old instance and attach to the new one. Adds ops complexity
   (orphaned volumes on stack delete).

2. **Detach the DB to a separate EBS volume**: mount `/var/lib/asg-insights`
   on its own `AWS::EC2::Volume` with `DeletionPolicy: Retain`. Volume
   survives instance replacement, teardown, everything. Requires editing
   `user_data.sh` to `mkfs.ext4` and mount, one-time. Best long-term shape
   for a durable single-node app.

For a leadership dashboard where losing 1 h of data is acceptable, hourly
backups + auto-restore is enough. For anything customer-facing, do option 2.

## 16. Disk hygiene — keeping /tmp and /var from filling

At 140 ASGs with 6-month 1-min retention the DB reaches ~7 GB — big enough
that a careless backup can fill `/tmp`. Two things to add.

### 16a. Redirect backups off /tmp

`/tmp` is only ~2 GB on Ubuntu by default. Update the backup script to use
`/var/tmp/asg-insights` on the main EBS (which has room):

```bash
sudo mkdir -p /var/tmp/asg-insights
sudo chown asginsights:asginsights /var/tmp/asg-insights

sudo tee /usr/local/bin/asg-insights-backup.sh >/dev/null <<'SH'
#!/bin/bash
set -euo pipefail
DATE=$(date -u +%Y%m%d)
BUCKET="$(cat /etc/asg-insights/bucket)"
DB=/var/lib/asg-insights/asg_insights.db
WORK=/var/tmp/asg-insights
mkdir -p "$WORK"
# Stream .backup → gzip directly. No 7 GB uncompressed file on disk.
sudo -u asginsights sqlite3 "$DB" ".backup $WORK/asg_insights.db"
gzip -f "$WORK/asg_insights.db"
aws s3 cp "$WORK/asg_insights.db.gz" "s3://$BUCKET/backups/latest.db.gz"
aws s3 cp "$WORK/asg_insights.db.gz" \
  "s3://$BUCKET/backups/asg_insights.$DATE.db.gz" \
  --storage-class STANDARD_IA
rm -f "$WORK/asg_insights.db.gz"
SH
sudo chmod +x /usr/local/bin/asg-insights-backup.sh
```

Result: the backup uses `/var/tmp` (same filesystem as the DB itself, with
plenty of headroom) and cleans up after itself. Peak extra disk usage
during backup: ~1× DB size + gzip'd copy (~35 % of DB).

### 16b. Automated disk hygiene job

Runs daily. Cleans stale artifacts, apt cache, and old journal logs; alerts
if anything is filling up:

```bash
sudo tee /usr/local/bin/asg-insights-disk-hygiene.sh >/dev/null <<'SH'
#!/bin/bash
set -uo pipefail

WARN_PCT=80
CRIT_PCT=90

# 1. Purge stale temp files (nothing older than 24 h in /tmp, 3 d in /var/tmp)
find /tmp -mindepth 1 -mtime +1 -not -path '/tmp/systemd-private*' -delete 2>/dev/null || true
find /var/tmp/asg-insights -mindepth 1 -mtime +3 -delete 2>/dev/null || true

# 2. Trim apt package cache (frees ~200-500 MB after upgrades)
apt-get -y clean

# 3. Cap systemd-journal to 500 MB (unlimited by default; grows forever)
journalctl --vacuum-size=500M >/dev/null 2>&1 || true

# 4. Report + fail loud if a mounted FS is over WARN_PCT
df -P --output=target,pcent | tail -n +2 | while read -r mount pct; do
  n=${pct%%%*}
  if (( n >= CRIT_PCT )); then
    echo "CRITICAL: $mount is $pct full"     >&2
  elif (( n >= WARN_PCT )); then
    echo "WARN: $mount is $pct full"         >&2
  fi
done

# 5. Report SQLite DB size for tracking
du -sh /var/lib/asg-insights/ 2>/dev/null | awk '{print "asg-insights DB size: " $1}'
SH
sudo chmod +x /usr/local/bin/asg-insights-disk-hygiene.sh

sudo tee /etc/systemd/system/asg-insights-disk-hygiene.service >/dev/null <<'UNIT'
[Unit]
Description=Daily disk cleanup + capacity check
[Service]
Type=oneshot
StandardOutput=journal
StandardError=journal
ExecStart=/usr/local/bin/asg-insights-disk-hygiene.sh
UNIT

sudo tee /etc/systemd/system/asg-insights-disk-hygiene.timer >/dev/null <<'UNIT'
[Unit]
Description=Run disk hygiene daily at 04:30 UTC
[Timer]
OnCalendar=*-*-* 04:30:00 UTC
Persistent=true
Unit=asg-insights-disk-hygiene.service
[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now asg-insights-disk-hygiene.timer
```

CRITICAL/WARN lines go to journald. Grep them into a CloudWatch metric
filter or SNS alert:

```bash
journalctl -u asg-insights-disk-hygiene.service --since '7 days ago' | grep -E 'WARN|CRITICAL'
```

### 16c. What consumes disk on this instance

| Location | Grows because | Auto-managed? |
|---|---|---|
| `/var/lib/asg-insights/asg_insights.db` | Puller + rollup writes | Yes — retention deletes old rows nightly |
| `/var/lib/asg-insights/asg_insights.db-wal` | Active writes | Yes — auto-checkpointed; capped by `wal_autocheckpoint` |
| `/var/tmp/asg-insights/` | Backup working area | Yes (§16b cleans > 3 days old) |
| `/var/log/asg-insights-bootstrap.log` | UserData appends on every re-run | No — rotate manually or with `logrotate` |
| `/var/log/journal/` | systemd journal | Yes (§16b caps to 500 MB) |
| `/var/cache/apt/` | apt downloads | Yes (§16b runs `apt-get clean`) |
| `/tmp/` | anything ad-hoc | Yes (§16b deletes >24 h old) |

If `du -sh /var/lib/asg-insights/` unexpectedly reports > 10 GB, look for:
- WAL file that failed to checkpoint (long-running reader — probably a hung `sqlite3` shell)
- Retention job not running (`journalctl -u asg-rollup.service`)
- Manual `.dump` output left in-place from an operator

## 17. Where to look for things

| Thing | Location |
|---|---|
| Flask routes | [app/main.py](../app/main.py) |
| SQLite DDL | [app/schema.sql](../app/schema.sql) |
| Metric queries + PS forecast fetch | [app/pull_metrics.py](../app/pull_metrics.py) |
| Rollup + monthly aggregation + recommendation rules | [app/rollup.py](../app/rollup.py) |
| systemd units | [systemd/](../systemd/) |
| EC2 bootstrap | [scripts/user_data.sh](../scripts/user_data.sh) |
| One-shot deploy | [scripts/deploy.sh](../scripts/deploy.sh) |
| CloudFormation template | [infra/cloudformation.yaml](../infra/cloudformation.yaml) |
| Runtime DB | `/var/lib/asg-insights/asg_insights.db` on the instance |
| Bootstrap log | `/var/log/asg-insights-bootstrap.log` on the instance |
| Puller history | `journalctl -u asg-puller.service` on the instance |
