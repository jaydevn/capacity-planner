# asg-insights — prerequisite setup guide (for customers)

Follow this once, before your first deployment. Takes ~20 minutes end to end.
After you finish, `./scripts/deploy.sh` will run cleanly. For day-2
operations (updates, backup, troubleshooting), see
[OPERATIONS.md](OPERATIONS.md).

---

## Step 1 — Decide the target account, region, and identity

Pick the AWS account this dashboard will live in. It should be the **same
account that owns the ASGs you want to monitor** — this app uses the EC2
instance's IAM role to read CloudWatch, ASG, and ALB APIs in its own account
only. Cross-account requires additional IAM work not covered here.

Write these down; you'll paste them into commands below:

- **Account ID**: `_____________`  (12 digits)
- **Region**: `_____________`      (e.g. `us-east-1`)
- **Stack name**: `asg-insights`   (fine to keep default)
- **Source S3 bucket name**: `asg-insights-src-<ACCOUNT_ID>`

You need an IAM identity (SSO role or IAM user) in that account with
permission to create EC2, IAM roles, S3, CloudFormation, and Route53 records.
Roughly `AdministratorAccess` or a scoped equivalent.

## Step 2 — Install the AWS tools on your workstation

macOS:

```bash
brew install awscli
brew install --cask session-manager-plugin
```

Linux:

```bash
# AWS CLI v2
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscli.zip
unzip awscli.zip && sudo ./aws/install
# Session Manager plugin
curl "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/ubuntu_64bit/session-manager-plugin.deb" -o smp.deb
sudo dpkg -i smp.deb
```

Verify:

```bash
aws --version                 # aws-cli/2.x
session-manager-plugin        # "The Session Manager plugin was installed successfully"
```

## Step 3 — Configure your AWS profile

If you use SSO:

```bash
aws configure sso
# follow the prompts; profile name: default (recommended so scripts work as-is)
```

If you use IAM access keys:

```bash
aws configure
# AWS Access Key ID:  ...
# AWS Secret Access Key:  ...
# Default region name:  <your region>
# Default output format: json
```

Confirm you're pointing at the right account:

```bash
aws sts get-caller-identity
```

The `Account` field must match the account ID you picked in Step 1.

## Step 4 — Prepare your VPC

The dashboard EC2 needs a VPC and a subnet. `deploy.sh` uses the **default
VPC** in the target region. Verify one exists:

```bash
aws ec2 describe-vpcs --filters "Name=isDefault,Values=true" \
  --query 'Vpcs[0].VpcId' --output text
```

If it returns `None`, either:
- Recreate the default VPC: `aws ec2 create-default-vpc`, or
- Edit `deploy.sh` to pass explicit `VpcId` and `SubnetId` parameters (a line
  is marked for override in the script).

Also verify your preferred AZ (`us-east-1a` by default) has a subnet:

```bash
aws ec2 describe-subnets --filters \
  "Name=vpc-id,Values=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)" \
  "Name=availability-zone,Values=us-east-1a"
```

If `us-east-1a` returns empty, override with `PREFERRED_AZ=us-east-1b` when
you run the deploy.

## Step 5 — Tag the ASGs you want to monitor

The puller discovers ASGs by an AWS tag (defaults: **`Environment=prod`**).
Every ASG the dashboard should track needs this tag.

Check what's already tagged:

```bash
aws autoscaling describe-auto-scaling-groups \
  --query 'AutoScalingGroups[?Tags[?Key==`Environment` && Value==`prod`]].AutoScalingGroupName'
```

To add the tag to an ASG:

```bash
aws autoscaling create-or-update-tags --tags \
  "ResourceId=my-asg-name,ResourceType=auto-scaling-group,Key=Environment,Value=prod,PropagateAtLaunch=false"
```

**Using a different tag key/value** (e.g. your team uses `env=production`)?
That's supported — you'll set two environment variables on the puller after
deploy. Skip the tag-check for now; see the "post-deploy config" note at the
end.

## Step 6 — Check the service quotas

The dashboard needs one small EC2 instance and some cheap AWS quota. Confirm
you're not blocked:

```bash
# Running On-Demand vCPUs (t3.medium = 2 vCPUs)
aws service-quotas get-service-quota \
  --service-code ec2 --quota-code L-1216C47A --region us-east-1 \
  --query 'Quota.Value'

# Should be >= 2. If it's 0, you're on a new account — request a small quota
# increase through the AWS console before proceeding.
```

Rough monthly cost (see [README](../README.md#cost-tuning-already-applied)):
- EC2 t3.medium: ~$30
- EBS 20 GiB gp3: ~$1.60
- CloudWatch GetMetricData: ~$1.80 at 50 ASGs, hourly polling
- S3 (source + backups): ~$0.10

**Total: ~$33/month** for a 50-ASG fleet. Roughly $8/month at ~10 ASGs.

## Step 7 — Get the source

Clone or copy the `claude-output` directory to your workstation. It contains
`scripts/deploy.sh`, the CloudFormation template, and the app source.

Verify structure:

```bash
cd path/to/claude-output
ls scripts/deploy.sh app/main.py infra/cloudformation.yaml
```

All three should exist.

## Step 8 — Adjust the deploy configuration (optional)

Most defaults are fine. Override only if needed. All overrides are env vars —
no file edits required.

| Env var | Default | When to change |
|---|---|---|
| `AWS_REGION` | `us-east-1` | Deploying to a different region |
| `AWS_PROFILE_OVERRIDE` | `default` | Your named profile is different |
| `INSTANCE_TYPE` | `t3.medium` | Want smaller/bigger box |
| `PREFERRED_AZ` | `${REGION}a` | The `-a` AZ has no default subnet |
| `ALLOWED_CIDR` | `172.31.0.0/16` | Non-default VPC or restricted access |

Example:

```bash
INSTANCE_TYPE=t3.small PREFERRED_AZ=us-east-1b ./scripts/deploy.sh
```

## Step 9 — Deploy

```bash
./scripts/deploy.sh
```

The script:
1. Verifies you're on the right account.
2. Creates the S3 source bucket (encryption + public-access-block + versioning).
3. Syncs source to S3.
4. Discovers VPC + subnet.
5. Validates the CloudFormation template.
6. Deploys the stack (~2 minutes).
7. Prints the stack outputs.

If deploy fails, it dumps the last 20 CloudFormation failure events
automatically — send them to your dev contact and they can pinpoint the fix.

## Step 10 — Wait for bootstrap, then verify

After the stack finishes, the EC2 UserData script takes another **~5 minutes**
to `apt install` Python + nginx, create the venv, and enable the services.

Grab the instance ID and watch the bootstrap log:

```bash
INST=$(aws cloudformation describe-stack-resource \
  --stack-name asg-insights --logical-resource-id Dashboard \
  --query 'StackResourceDetail.PhysicalResourceId' --output text)

aws ssm start-session --target "$INST"
# in the session:
sudo tail -f /var/log/asg-insights-bootstrap.log
# wait for "systemctl enable --now ..." lines, then Ctrl-C, then:
systemctl status asg-insights.service --no-pager
curl -s http://127.0.0.1/healthz    # expect: {"ok": true}
exit
```

## Step 11 — Open the dashboard

Still on your workstation (leave this session running while you browse):

```bash
aws ssm start-session --target "$INST" \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["80"],"localPortNumber":["8080"]}'
```

Open **http://127.0.0.1:8080/** in your browser.

The freshness strip at the top says "No ingestion recorded yet" until the
first hourly poll runs (up to ~1 hour after boot, unless you kick it manually
— see next step).

## Step 12 — Trigger the first data pull

By default, the puller runs 2 minutes after boot, then hourly. To see data
immediately without waiting, kick it manually:

```bash
aws ssm start-session --target "$INST"
sudo systemctl start asg-puller.service
sudo journalctl -u asg-puller.service -n 30 --no-pager
```

Look for `discovered N ASGs matching Environment=prod`. Refresh the browser
— the ASGs appear on the fleet dashboard within seconds.

**If it says `discovered 0 ASGs`**, the discovery tag doesn't match. Fix
either by tagging your ASGs (Step 5) or by overriding the tag on the puller:

```bash
# still in SSM shell as root
systemctl edit asg-puller.service
# in the editor, add:
[Service]
Environment=ASG_DISCOVERY_TAG_KEY=env
Environment=ASG_DISCOVERY_TAG_VAL=production

systemctl daemon-reload
systemctl start asg-puller.service
```

Adjust the `env=production` to whatever tag your team actually uses.

## Step 13 — (Optional) Enable daily backups

The whole app state is one SQLite file. Section 8 of
[OPERATIONS.md](OPERATIONS.md#8-backup-and-restore) has a copy-pasteable
systemd timer that snapshots the DB to S3 nightly. Do this before your
customer starts relying on the numbers.

## Step 14 — Hand off

You're done. Give your customer:

1. This document (or `README.md` for a shorter intro).
2. [OPERATIONS.md](OPERATIONS.md) for ongoing maintenance.
3. The SSM port-forward command they'll use to reach the dashboard.
4. The stack name (`asg-insights`) and source bucket
   (`asg-insights-src-<account>`), so they can find things later.

If they'll be updating ASG tags or asking about numbers, point them at the
"Data freshness" strip at the top of every dashboard page — it shows when
each source last synced.

---

## Quick reference — what got created

After Step 9 succeeds, the following exist in your AWS account:

| Resource | Purpose | Deletion |
|---|---|---|
| CloudFormation stack `asg-insights` | Manages everything below except the bucket | `delete-stack` removes it |
| EC2 instance (`t3.medium`) | Runs the Flask dashboard + puller | Stack-managed |
| IAM role + instance profile | Read-only access to CW, ASG, ALB, SSM | Stack-managed |
| Security group | :80 from `ALLOWED_CIDR` only | Stack-managed |
| S3 bucket `asg-insights-src-<account>` | App source + backups | **Retained** — outlives stack teardown |

## Troubleshooting quick reference

Symptom → see:
- Deploy fails with early-validation error → [OPERATIONS.md §9](OPERATIONS.md#9-troubleshooting-playbook)
- Dashboard shows 404/502 → OPERATIONS §9
- Freshness badges red → OPERATIONS §4
- `TargetNotConnected` on SSM → wait 60–90s after instance state = running
- `SessionManagerPlugin not found` → re-do Step 2
- `sudo` asks for a password inside SSM → you're an `ssm-user` whose passwordless
  sudoers file needs a fresh session. Type `exit`, reconnect via
  `aws ssm start-session --target "$INST"`, retry.

## Gotchas already handled by this codebase

Six real problems came up during the first end-to-end deploy. All are fixed in
the code you have — listed here so you understand what's protecting you
(and know what to check if they somehow resurface in a different environment):

1. **S3 bucket already exists** during `cloudformation deploy`.
   The bucket is created by `deploy.sh` (with encryption / PAB / versioning),
   not by the CFN template. Prevents `AWS::EarlyValidation::ResourceExistenceCheck`
   collisions on re-deploys.

2. **Em-dash characters in Security Group rule descriptions.**
   EC2 rejects rule descriptions containing anything outside
   `a-zA-Z0-9._-:/()#,@[]+=&;{}!$*`. All SG descriptions in
   `infra/cloudformation.yaml` use ASCII only.

3. **`t3.micro` not available in `us-east-1e`.**
   Default instance type is `t3.medium` and `deploy.sh` prefers `us-east-1a`
   for its subnet — the AZ with the broadest instance-family support.
   Override with `PREFERRED_AZ` if that AZ doesn't have a default subnet.

4. **dpkg lock held by `unattended-upgrades` at first boot.**
   `scripts/user_data.sh` waits up to 5 minutes for the apt lock before
   installing anything, and passes `-o DPkg::Lock::Timeout=300` to apt itself.
   Prevents the classic Ubuntu first-boot race.

5. **nginx not reloaded after our site config is dropped.**
   nginx is installed and auto-started by apt with a default config; the
   bootstrap adds a new site file and now explicitly runs
   `nginx -t && systemctl reload nginx`, so port 80 serves the app on the
   first boot (not just after a reboot).

6. **AZ-specific service quota surprises.**
   The template requests `t3` family in every AZ; Step 6 has you verify vCPU
   quota is >= 2 before deploying so it doesn't fail late.

If a customer still hits one of these, it usually means a change was made
outside this repo (custom AMI, non-default VPC, service control policy).
Point them at [OPERATIONS.md §9](OPERATIONS.md#9-troubleshooting-playbook)
which has the same table with fix commands.
