#!/usr/bin/env bash
# One-shot deploy for asg-insights.
#
# The account, region, and bucket name are derived from your AWS profile —
# nothing about the target environment is hardcoded here. Override any of
# the env vars below to change destination.
#
# Cost knobs already applied elsewhere:
#   * t3.medium EC2 (~$30/mo, supported in every us-east-1 AZ)
#   * 20 GiB gp3 (~$1.60/mo)
#   * Puller runs hourly at Period=300 (5-min buckets)
#   * Dashboard queries SQLite only — no per-request CloudWatch bill
#
set -euo pipefail

PROFILE="${AWS_PROFILE_OVERRIDE:-default}"
REGION="${AWS_REGION:-us-east-1}"
STACK="${STACK_NAME:-asg-insights}"
SCAN_REGIONS="${ASG_SCAN_REGIONS:-$REGION}"
SCAN_REGIONS="${SCAN_REGIONS//[[:space:]]/}"

if [[ ! "$SCAN_REGIONS" =~ ^[a-z0-9-]+(,[a-z0-9-]+)*$ ]]; then
  echo "ERROR: ASG_SCAN_REGIONS must be a comma-separated region list; got: $SCAN_REGIONS" >&2
  exit 2
fi

# Discover the account from the profile so nothing about the target account
# is baked into the script.
ACCOUNT=$(aws --profile "$PROFILE" sts get-caller-identity --query Account --output text)
BUCKET="${SOURCE_BUCKET:-${STACK}-src-${ACCOUNT}}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "  account $ACCOUNT  deploy-region $REGION  scan-regions $SCAN_REGIONS  profile $PROFILE  stack $STACK  bucket $BUCKET"

echo "== Ensuring source bucket $BUCKET =="
if ! aws --profile "$PROFILE" --region "$REGION" s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  aws --profile "$PROFILE" --region "$REGION" s3 mb "s3://$BUCKET"
fi
# These are idempotent — safe to re-apply on every deploy.
aws --profile "$PROFILE" --region "$REGION" s3api put-bucket-encryption \
  --bucket "$BUCKET" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws --profile "$PROFILE" --region "$REGION" s3api put-public-access-block \
  --bucket "$BUCKET" --public-access-block-configuration \
  'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'
aws --profile "$PROFILE" --region "$REGION" s3api put-bucket-versioning \
  --bucket "$BUCKET" --versioning-configuration Status=Enabled

echo "== Syncing source to s3://$BUCKET/ =="
aws --profile "$PROFILE" --region "$REGION" s3 sync . "s3://$BUCKET/" \
  --exclude ".venv/*" --exclude "__pycache__/*" --exclude "*.db" \
  --exclude "*.db-journal" --exclude "*.db-wal" --exclude "*.db-shm" \
  --exclude ".git/*" --exclude ".DS_Store" --exclude ".claude/*" \
  --delete

echo "== Discovering default VPC + subnet =="
VPC_ID=$(aws --profile "$PROFILE" --region "$REGION" ec2 describe-vpcs \
  --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
# Prefer us-east-1a; some AZs (e.g. us-east-1e) don't offer the newer instance
# families, so picking a well-supported AZ avoids capacity-error retries.
PREFERRED_AZ="${PREFERRED_AZ:-${REGION}a}"
SUBNET_ID=$(aws --profile "$PROFILE" --region "$REGION" ec2 describe-subnets \
  --filters "Name=vpc-id,Values=$VPC_ID" "Name=availability-zone,Values=$PREFERRED_AZ" \
  --query 'Subnets[0].SubnetId' --output text)
if [[ "$SUBNET_ID" == "None" || -z "$SUBNET_ID" ]]; then
  # Fall back to any subnet in the default VPC.
  SUBNET_ID=$(aws --profile "$PROFILE" --region "$REGION" ec2 describe-subnets \
    --filters "Name=vpc-id,Values=$VPC_ID" --query 'Subnets[0].SubnetId' --output text)
fi
echo "  VPC=$VPC_ID  Subnet=$SUBNET_ID  AZ=$PREFERRED_AZ"

if [[ "$VPC_ID" == "None" || -z "$VPC_ID" ]]; then
  echo "ERROR: no default VPC in $REGION. Create one or pass --parameter-overrides VpcId=... SubnetId=..." >&2
  exit 1
fi

# Allow the internal 172.31/16 that default VPCs use, unless caller overrides.
ALLOWED_CIDR="${ALLOWED_CIDR:-172.31.0.0/16}"

echo "== Validating template =="
aws --profile "$PROFILE" --region "$REGION" cloudformation validate-template \
  --template-body file://infra/cloudformation.yaml >/dev/null
echo "  template OK"

echo "== Deploying stack $STACK =="
aws --profile "$PROFILE" --region "$REGION" cloudformation deploy \
  --stack-name "$STACK" \
  --template-file infra/cloudformation.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
      "VpcId=$VPC_ID" \
      "SubnetId=$SUBNET_ID" \
      "AllowedCidr=$ALLOWED_CIDR" \
      "InstanceType=${INSTANCE_TYPE:-t3.medium}" \
      "SourceBucketName=$BUCKET" \
      "ScanRegions=$SCAN_REGIONS" \
      "DnsHostedZoneId=${DNS_ZONE_ID:-}" \
      "DnsRecordName=${DNS_RECORD:-}" \
      || {
        echo
        echo "== Deploy failed — printing recent failure events =="
        aws --profile "$PROFILE" --region "$REGION" cloudformation describe-events \
          --stack-name "$STACK" --filters FailedEvents=true --max-items 20 \
          --query 'StackEvents[*].[Timestamp,ResourceType,LogicalResourceId,ResourceStatus,ResourceStatusReason]' \
          --output table || true
        exit 1
      }

echo "== Outputs =="
aws --profile "$PROFILE" --region "$REGION" cloudformation describe-stacks \
  --stack-name "$STACK" --query 'Stacks[0].Outputs' --output table

echo
echo "The instance is in a private subnet with no public IP. To reach the"
echo "dashboard: SSM session-manager port-forward or attach an internet gateway."
echo "  aws --profile $PROFILE --region $REGION ssm start-session \\"
echo "    --target <INSTANCE_ID> --document-name AWS-StartPortForwardingSession \\"
echo "    --parameters portNumber=80,localPortNumber=8080"
echo "then open http://127.0.0.1:8080/"
