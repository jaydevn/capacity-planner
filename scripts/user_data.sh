#!/bin/bash
# EC2 user-data — bootstraps the asg-insights app. Run once on instance launch.
set -euxo pipefail

APP_DIR=/opt/asg-insights
DATA_DIR=/var/lib/asg-insights
CONFIG_DIR=/etc/asg-insights
SRC_S3="${SRC_S3:?SRC_S3 must be set by CloudFormation (s3://bucket/prefix)}"
AWS_REGION="${AWS_REGION:-us-east-1}"
# 'all' scans every enabled region in the account (requirement: all ASGs,
# all regions). Override by passing an explicit comma-separated list.
ASG_SCAN_REGIONS="${ASG_SCAN_REGIONS:-all}"
ASG_DISCOVERY_TAG_KEY="${ASG_DISCOVERY_TAG_KEY:-Environment}"
ASG_DISCOVERY_TAG_VAL="${ASG_DISCOVERY_TAG_VAL:-prod}"

export DEBIAN_FRONTEND=noninteractive

wait_for_apt() {
  local i
  for i in $(seq 1 60); do
    if ! fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 \
       && ! fuser /var/lib/apt/lists/lock >/dev/null 2>&1; then
      return 0
    fi
    echo "waiting for apt lock ($i/60)..."
    sleep 5
  done
  echo "apt lock still held after 5 min — giving up" >&2
  return 1
}
wait_for_apt

APT_OPTS='-o DPkg::Lock::Timeout=300 -o Dpkg::Options::=--force-confold'
apt-get $APT_OPTS update -y
apt-get $APT_OPTS install -y python3.11 python3.11-venv python3-pip nginx awscli sqlite3

useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin asginsights || true
install -d -o asginsights -g asginsights "$APP_DIR" "$DATA_DIR"
install -d -m 0755 "$CONFIG_DIR"

aws s3 sync "$SRC_S3" "$APP_DIR" --delete
chown -R asginsights:asginsights "$APP_DIR"

sudo -u asginsights python3.11 -m venv "$APP_DIR/.venv"
sudo -u asginsights "$APP_DIR/.venv/bin/pip" install --upgrade pip
sudo -u asginsights "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# Preserve customer edits: only seed the env file if it doesn't already exist.
# Re-runs of user_data.sh (e.g. instance replacement with a re-mounted DB
# volume) will find the file present and skip the write, keeping any tuning
# the customer applied via `systemctl edit` or direct edit intact.
if [[ ! -f "$CONFIG_DIR/asg-insights.env" ]]; then
  cat >"$CONFIG_DIR/asg-insights.env" <<EOF
AWS_REGION=$AWS_REGION
ASG_SCAN_REGIONS=$ASG_SCAN_REGIONS
ASG_DISCOVERY_TAG_KEY=$ASG_DISCOVERY_TAG_KEY
ASG_DISCOVERY_TAG_VAL=$ASG_DISCOVERY_TAG_VAL
ASG_INSIGHTS_DB=$DATA_DIR/asg_insights.db
EOF
  chmod 0644 "$CONFIG_DIR/asg-insights.env"
  echo "seeded $CONFIG_DIR/asg-insights.env"
else
  echo "$CONFIG_DIR/asg-insights.env already present — leaving as-is"
fi

# Always drop the example alongside it, so operators have a reference.
install -m 0644 "$APP_DIR/config/asg-insights.env.example" \
  "$CONFIG_DIR/asg-insights.env.example" 2>/dev/null || true

install -m 0644 "$APP_DIR/systemd/asg-insights.service" /etc/systemd/system/
install -m 0644 "$APP_DIR/systemd/asg-puller.service"   /etc/systemd/system/
install -m 0644 "$APP_DIR/systemd/asg-puller.timer"     /etc/systemd/system/
install -m 0644 "$APP_DIR/systemd/asg-rollup.service"   /etc/systemd/system/
install -m 0644 "$APP_DIR/systemd/asg-rollup.timer"     /etc/systemd/system/

cat >/etc/nginx/sites-available/asg-insights <<'NGINX'
server {
  listen 80 default_server;
  server_name _;
  client_max_body_size 4m;
  location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 30s;
  }
}
NGINX
ln -sf /etc/nginx/sites-available/asg-insights /etc/nginx/sites-enabled/asg-insights
rm -f /etc/nginx/sites-enabled/default

systemctl daemon-reload

DB="$DATA_DIR/asg_insights.db"
echo "${SRC_S3#s3://}" | sed 's|/$||' > "$CONFIG_DIR/bucket"
BUCKET=$(cat "$CONFIG_DIR/bucket")
if [[ ! -s "$DB" ]] && \
   aws s3api head-object --bucket "$BUCKET" --key backups/latest.db.gz >/dev/null 2>&1; then
  echo "restoring DB from s3://$BUCKET/backups/latest.db.gz"
  aws s3 cp "s3://$BUCKET/backups/latest.db.gz" /tmp/latest.db.gz
  gunzip -f /tmp/latest.db.gz
  install -m 0644 -o asginsights -g asginsights /tmp/latest.db "$DB"
  rm -f /tmp/latest.db
fi

systemctl enable --now asg-insights.service asg-puller.timer asg-rollup.timer nginx
nginx -t
systemctl reload nginx
