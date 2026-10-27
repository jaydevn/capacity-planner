"""Collect ASG configuration and CloudWatch metrics across configured regions.

The systemd job reads ``ASG_SCAN_REGIONS`` as a comma-separated allowlist.
Each region gets its own boto3 session while credentials continue to come from
this EC2 instance's IAM role. Results are keyed by region and ASG name.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import boto3

from . import db

log = logging.getLogger("pull_metrics")

DISCOVERY_TAG_KEY = os.environ.get("ASG_DISCOVERY_TAG_KEY", "Environment")
DISCOVERY_TAG_VAL = os.environ.get("ASG_DISCOVERY_TAG_VAL", "prod")
POLL_WINDOW_MIN = int(os.environ.get("ASG_POLL_WINDOW_MIN", "60"))
DEFAULT_METRIC_PERIOD_SEC = int(os.environ.get("ASG_METRIC_PERIOD_SEC", "300"))
RESOLUTION_TAG_KEY = os.environ.get("ASG_RESOLUTION_TAG_KEY", "AsgInsightsResolution")
RESOLUTION_TAG_HIGH_RES_VAL = os.environ.get("ASG_RESOLUTION_TAG_HIGH_VAL", "1min")

# Fallback on-demand rates used only when the AWS Pricing API is unreachable
# or returns no price for a (region, instance_type). The poller caches real
# Pricing API rates into the pricing_cache table on each run; these are the
# last resort so cost/savings math never silently becomes $0.
HOURLY_RATE_USD = {
    "m5.large": 0.096, "m5.xlarge": 0.192, "m5.2xlarge": 0.384,
    "m5.4xlarge": 0.768, "m5.8xlarge": 1.536, "m5.12xlarge": 2.304,
    "c5.large": 0.085, "c5.xlarge": 0.170, "c5.2xlarge": 0.340,
    "r5.large": 0.126, "r5.xlarge": 0.252, "r5.2xlarge": 0.504,
    "t3.medium": 0.0416, "t3.large": 0.0832,
}

# The AWS Pricing API is only served from a few regions; us-east-1 carries the
# global on-demand price list. Region names must be mapped to the "location"
# long names the Pricing API filters on.
PRICING_API_REGION = os.environ.get("ASG_PRICING_API_REGION", "us-east-1")
_REGION_LOCATION = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "ca-central-1": "Canada (Central)",
    "eu-west-1": "EU (Ireland)",
    "eu-west-2": "EU (London)",
    "eu-west-3": "EU (Paris)",
    "eu-central-1": "EU (Frankfurt)",
    "eu-north-1": "EU (Stockholm)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "sa-east-1": "South America (Sao Paulo)",
}


def fetch_on_demand_rate(region: str, instance_type: str) -> tuple[float, str]:
    """Return (hourly_usd, source) for a Linux on-demand instance.

    Queries the AWS Pricing API (served from us-east-1). Falls back to the
    static HOURLY_RATE_USD table when the API is unreachable or has no match.
    """
    location = _REGION_LOCATION.get(region)
    if location:
        try:
            client = boto3.Session(region_name=PRICING_API_REGION).client("pricing")
            response = client.get_products(
                ServiceCode="AmazonEC2",
                Filters=[
                    {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
                    {"Type": "TERM_MATCH", "Field": "location", "Value": location},
                    {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
                    {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
                    {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
                    {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
                ],
                MaxResults=1,
            )
            for raw in response.get("PriceList", []):
                price = _extract_on_demand_price(raw)
                if price is not None:
                    return price, "pricing_api"
        except Exception as error:  # pragma: no cover - network/permission dependent
            log.warning("Pricing API lookup failed for %s/%s: %s", region, instance_type, error)
    return HOURLY_RATE_USD.get(instance_type, 0.0), "fallback"


def _extract_on_demand_price(raw: str) -> float | None:
    """Pull the USD on-demand per-hour price out of a Pricing API product JSON."""
    product = json.loads(raw)
    on_demand = product.get("terms", {}).get("OnDemand", {})
    for term in on_demand.values():
        for dim in term.get("priceDimensions", {}).values():
            usd = dim.get("pricePerUnit", {}).get("USD")
            try:
                value = float(usd)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
    return None


def cache_price(region: str, instance_type: str, rate: float, source: str) -> None:
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO pricing_cache (region, instance_type, hourly_rate_usd, source, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(region, instance_type) DO UPDATE SET
              hourly_rate_usd=excluded.hourly_rate_usd,
              source=excluded.source,
              updated_at=excluded.updated_at
            """,
            (region, instance_type, rate, source, int(time.time())),
        )


def resolve_rate(session: boto3.Session, region: str, instance_type: str | None) -> float:
    """Resolve the hourly rate for an instance type, caching the Pricing result."""
    if not instance_type:
        return 0.0
    rate, source = fetch_on_demand_rate(region, instance_type)
    if rate > 0:
        cache_price(region, instance_type, rate, source)
    return rate


def _all_account_regions() -> list[str]:
    """Enumerate every region enabled for this account via EC2 DescribeRegions."""
    anchor = os.environ.get("AWS_REGION") or boto3.Session().region_name or "us-east-1"
    client = boto3.Session(region_name=anchor).client("ec2")
    response = client.describe_regions(AllRegions=False)
    regions = sorted(item["RegionName"] for item in response.get("Regions", []))
    if not regions:
        raise ValueError("DescribeRegions returned no enabled regions for this account")
    return regions


def configured_regions() -> list[str]:
    """Return the regions to scan.

    Default behavior is to scan *every* region enabled in the account, which
    satisfies the "all ASGs across all regions" requirement. Set
    ``ASG_SCAN_REGIONS`` to a comma-separated allowlist to restrict scanning
    to specific regions; the sentinel value ``all`` (the default) means
    "discover every enabled region at runtime".
    """
    raw = (os.environ.get("ASG_SCAN_REGIONS") or "all").strip()
    if raw.lower() == "all":
        return _all_account_regions()

    regions: list[str] = []
    seen: set[str] = set()
    for item in raw.split(","):
        region = item.strip()
        if region and region not in seen:
            regions.append(region)
            seen.add(region)
    if not regions:
        raise ValueError("ASG_SCAN_REGIONS must be 'all' or a comma-separated region list")
    return regions


def _scaling_policies(session: boto3.Session, asg_name: str) -> list[dict]:
    client = session.client("autoscaling")
    policies: list[dict] = []
    paginator = client.get_paginator("describe_policies")
    for page in paginator.paginate(AutoScalingGroupName=asg_name):
        policies.extend(page.get("ScalingPolicies", []))
    return policies


def discover_asgs(session: boto3.Session) -> list[dict]:
    """List ASGs in one region that match the configured discovery tag."""
    client = session.client("autoscaling")
    out: list[dict] = []
    for page in client.get_paginator("describe_auto_scaling_groups").paginate():
        for group in page.get("AutoScalingGroups", []):
            tags = {tag["Key"]: tag["Value"] for tag in group.get("Tags", [])}
            if tags.get(DISCOVERY_TAG_KEY) != DISCOVERY_TAG_VAL:
                continue
            instance_type = tags.get("InstanceType") if group.get("LaunchTemplate") else None
            period = (
                60
                if tags.get(RESOLUTION_TAG_KEY) == RESOLUTION_TAG_HIGH_RES_VAL
                else DEFAULT_METRIC_PERIOD_SEC
            )
            out.append({
                "asg_name": group["AutoScalingGroupName"],
                "region": session.region_name,
                "instance_type": instance_type,
                "min_size": group["MinSize"],
                "max_size": group["MaxSize"],
                # Defaults; overwritten by load_scaling_policies() in scan_region().
                # Use None so a policy-load failure is distinguishable from a
                # genuine "no predictive scaling" state rather than silently
                # recording ps_enabled=0.
                "tt_target": None,
                "ps_enabled": None,
                "scale_in_enabled": None,
                "tg_arns": json.dumps(group.get("TargetGroupARNs", [])),
                "tags": json.dumps(tags),
                "hourly_rate_usd": resolve_rate(session, session.region_name, instance_type),
                "metric_period_sec": period,
            })
    return out


def load_scaling_policies(session: boto3.Session, asg_name: str) -> dict:
    tt_target = None
    ps_enabled = False
    scale_in_enabled = True
    for policy in _scaling_policies(session, asg_name):
        if policy["PolicyType"] == "TargetTrackingScaling":
            cfg = policy.get("TargetTrackingConfiguration") or {}
            tt_target = cfg.get("TargetValue")
            if cfg.get("DisableScaleIn"):
                scale_in_enabled = False
        elif policy["PolicyType"] == "PredictiveScaling":
            ps_enabled = True
    return {
        "tt_target": tt_target,
        "ps_enabled": int(ps_enabled),
        "scale_in_enabled": int(scale_in_enabled),
    }


def fetch_target_groups(session: boto3.Session, tg_arns: list[str]) -> list[dict]:
    """Resolve target-group metadata (name, proto/port, type, LB ARNs)."""
    if not tg_arns:
        return []
    client = session.client("elbv2")
    out: list[dict] = []
    # DescribeTargetGroups accepts up to 20 ARNs per call.
    for i in range(0, len(tg_arns), 20):
        batch = tg_arns[i : i + 20]
        try:
            response = client.describe_target_groups(TargetGroupArns=batch)
        except Exception as error:
            log.warning("DescribeTargetGroups failed for %s: %s", batch, error)
            continue
        for tg in response.get("TargetGroups", []):
            out.append({
                "tg_arn": tg["TargetGroupArn"],
                "tg_name": tg.get("TargetGroupName"),
                "protocol": tg.get("Protocol"),
                "port": tg.get("Port"),
                "target_type": tg.get("TargetType"),
                "lb_arns": json.dumps(tg.get("LoadBalancerArns", [])),
            })
    return out


def upsert_target_groups(region: str, asg_name: str, rows: list[dict]) -> None:
    now = int(time.time())
    with db.get_conn() as conn:
        conn.execute(
            "DELETE FROM asg_target_groups WHERE region=? AND asg_name=?",
            (region, asg_name),
        )
        for row in rows:
            conn.execute(
                """
                INSERT INTO asg_target_groups
                  (region, asg_name, tg_arn, tg_name, protocol, port,
                   target_type, lb_arns, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(region, asg_name, tg_arn) DO UPDATE SET
                  tg_name=excluded.tg_name, protocol=excluded.protocol,
                  port=excluded.port, target_type=excluded.target_type,
                  lb_arns=excluded.lb_arns, updated_at=excluded.updated_at
                """,
                (
                    region, asg_name, row["tg_arn"], row["tg_name"], row["protocol"],
                    row["port"], row["target_type"], row["lb_arns"], now,
                ),
            )


def _prior_policy_state(region: str, asg_name: str) -> dict:
    """Return the last stored policy state for an ASG, or safe defaults.

    Used when a live DescribePolicies call fails so we preserve a known PS/TT
    configuration instead of overwriting it with nulls.
    """
    row = db.query_one(
        "SELECT tt_target, ps_enabled, scale_in_enabled FROM asg_config "
        "WHERE region=? AND asg_name=?",
        (region, asg_name),
    )
    if row is None:
        return {"tt_target": None, "ps_enabled": 0, "scale_in_enabled": 1}
    return {
        "tt_target": row["tt_target"],
        "ps_enabled": row["ps_enabled"] if row["ps_enabled"] is not None else 0,
        "scale_in_enabled": (
            row["scale_in_enabled"] if row["scale_in_enabled"] is not None else 1
        ),
    }


def upsert_config(rows: list[dict]) -> None:
    now = int(time.time())
    with db.get_conn() as conn:
        for row in rows:
            conn.execute(
                """
                INSERT INTO asg_config (region, asg_name, instance_type, min_size, max_size,
                                        tt_target, ps_enabled, scale_in_enabled,
                                        tg_arns, tags, first_seen, last_seen, hourly_rate_usd,
                                        metric_period_sec)
                VALUES (:region, :asg_name, :instance_type, :min_size, :max_size,
                        :tt_target, :ps_enabled, :scale_in_enabled,
                        :tg_arns, :tags, :now, :now, :hourly_rate_usd, :metric_period_sec)
                ON CONFLICT(region, asg_name) DO UPDATE SET
                  instance_type=excluded.instance_type,
                  min_size=excluded.min_size,
                  max_size=excluded.max_size,
                  tt_target=excluded.tt_target,
                  ps_enabled=excluded.ps_enabled,
                  scale_in_enabled=excluded.scale_in_enabled,
                  tg_arns=excluded.tg_arns,
                  tags=excluded.tags,
                  last_seen=excluded.last_seen,
                  hourly_rate_usd=excluded.hourly_rate_usd,
                  metric_period_sec=excluded.metric_period_sec
                """,
                {**row, "now": now},
            )


def fetch_ps_forecast(
    session: boto3.Session,
    asg_name: str,
    start: datetime,
    end: datetime,
) -> list[tuple[int, float]]:
    policies = _scaling_policies(session, asg_name)
    predictive = next((p for p in policies if p["PolicyType"] == "PredictiveScaling"), None)
    if not predictive:
        return []
    response = session.client("autoscaling").get_predictive_scaling_forecast(
        AutoScalingGroupName=asg_name,
        PolicyName=predictive["PolicyName"],
        StartTime=start,
        EndTime=end,
    )
    capacity = response.get("CapacityForecast", {}) or {}
    rows: list[tuple[int, float]] = []
    for ts, value in zip(capacity.get("Timestamps", []), capacity.get("Values", [])):
        bucket = (int(ts.timestamp()) // DEFAULT_METRIC_PERIOD_SEC) * DEFAULT_METRIC_PERIOD_SEC
        rows.append((bucket, float(value)))
    return rows


def upsert_ps_forecast(
    rows: list[tuple[int, float]],
    region: str,
    asg_name: str,
) -> None:
    with db.get_conn() as conn:
        for ts, value in rows:
            conn.execute(
                """
                INSERT INTO asg_snapshots_5m
                  (region, ts, asg_name, in_service_avg, in_service_max, desired_avg,
                   alb_rcpt_avg, alb_rcpt_max, ps_predicted_avg, cost_hourly_usd)
                VALUES (?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, NULL)
                ON CONFLICT(region, ts, asg_name) DO UPDATE SET
                  ps_predicted_avg=excluded.ps_predicted_avg
                """,
                (region, ts, asg_name, value),
            )


def fetch_metrics(
    session: boto3.Session,
    asg_name: str,
    tg_arns: list[str],
    start: datetime,
    end: datetime,
    period_sec: int,
) -> list[dict]:
    queries = [
        {
            "Id": "insvc",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/AutoScaling",
                    "MetricName": "GroupInServiceInstances",
                    "Dimensions": [{"Name": "AutoScalingGroupName", "Value": asg_name}],
                },
                "Period": period_sec,
                "Stat": "Average",
            },
            "ReturnData": True,
        },
        {
            "Id": "desired",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/AutoScaling",
                    "MetricName": "GroupDesiredCapacity",
                    "Dimensions": [{"Name": "AutoScalingGroupName", "Value": asg_name}],
                },
                "Period": period_sec,
                "Stat": "Average",
            },
            "ReturnData": True,
        },
    ]
    for index, arn in enumerate(tg_arns):
        target_group_dimension = arn.split(":targetgroup/")[-1]
        queries.append({
            "Id": f"rcpt{index}",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/ApplicationELB",
                    "MetricName": "RequestCountPerTarget",
                    "Dimensions": [
                        {"Name": "TargetGroup", "Value": f"targetgroup/{target_group_dimension}"}
                    ],
                },
                "Period": period_sec,
                "Stat": "Sum",
            },
            "ReturnData": True,
        })

    client = session.client("cloudwatch")
    request = {
        "MetricDataQueries": queries,
        "StartTime": start,
        "EndTime": end,
        "ScanBy": "TimestampAscending",
    }
    results: list[dict] = []
    while True:
        response = client.get_metric_data(**request)
        results.extend(response.get("MetricDataResults", []))
        next_token = response.get("NextToken")
        if not next_token:
            break
        request["NextToken"] = next_token

    period_min = max(1, period_sec // 60)
    by_ts: dict[int, dict] = {}
    for series in results:
        for ts, value in zip(series.get("Timestamps", []), series.get("Values", [])):
            key = int(ts.timestamp())
            row = by_ts.setdefault(
                key,
                {"region": session.region_name, "ts": key, "asg_name": asg_name},
            )
            if series["Id"] == "insvc":
                row["in_service"] = int(value)
            elif series["Id"] == "desired":
                row["desired"] = int(value)
            elif series["Id"].startswith("rcpt"):
                row["alb_rcpt"] = (row.get("alb_rcpt") or 0) + value / period_min
    return list(by_ts.values())


def upsert_snapshots(rows: list[dict], hourly_rate: float, period_sec: int) -> None:
    if period_sec == 60:
        table = "asg_snapshots"
        statement = """
            INSERT INTO asg_snapshots
              (region, ts, asg_name, in_service, desired, alb_rcpt, ps_predicted, cost_hourly_usd)
            VALUES (:region, :ts, :asg_name, :in_service, :desired, :alb_rcpt, NULL, :cost_hourly)
            ON CONFLICT(region, ts, asg_name) DO UPDATE SET
              in_service=excluded.in_service,
              desired=excluded.desired,
              alb_rcpt=excluded.alb_rcpt,
              cost_hourly_usd=excluded.cost_hourly_usd
        """
    else:
        table = "asg_snapshots_5m"
        statement = """
            INSERT INTO asg_snapshots_5m
              (region, ts, asg_name, in_service_avg, in_service_max, desired_avg,
               alb_rcpt_avg, alb_rcpt_max, ps_predicted_avg, cost_hourly_usd)
            VALUES (:region, :ts, :asg_name, :in_service, :in_service, :desired,
                    :alb_rcpt, :alb_rcpt, NULL, :cost_hourly)
            ON CONFLICT(region, ts, asg_name) DO UPDATE SET
              in_service_avg=excluded.in_service_avg,
              in_service_max=excluded.in_service_max,
              desired_avg=excluded.desired_avg,
              alb_rcpt_avg=excluded.alb_rcpt_avg,
              alb_rcpt_max=excluded.alb_rcpt_max,
              cost_hourly_usd=excluded.cost_hourly_usd
        """
    with db.get_conn() as conn:
        for row in rows:
            row.setdefault("in_service", None)
            row.setdefault("desired", None)
            row.setdefault("alb_rcpt", None)
            cost_hourly = (row.get("in_service") or 0) * hourly_rate
            conn.execute(statement, {**row, "cost_hourly": cost_hourly})
    log.debug("wrote %d rows to %s", len(rows), table)


def mark_freshness(
    region: str,
    asg_name: str,
    source: str,
    *,
    ok: bool,
    data_ts: int | None = None,
    err: str | None = None,
) -> None:
    now = int(time.time())
    with db.get_conn() as conn:
        if ok:
            conn.execute(
                """
                INSERT INTO ingestion_status
                  (region, asg_name, source, last_success_ts, last_success_data_ts)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(region, asg_name, source) DO UPDATE SET
                  last_success_ts=excluded.last_success_ts,
                  last_success_data_ts=COALESCE(
                    excluded.last_success_data_ts,
                    ingestion_status.last_success_data_ts
                  )
                """,
                (region, asg_name, source, now, data_ts),
            )
        else:
            conn.execute(
                """
                INSERT INTO ingestion_status
                  (region, asg_name, source, last_error_ts, last_error_msg)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(region, asg_name, source) DO UPDATE SET
                  last_error_ts=excluded.last_error_ts,
                  last_error_msg=excluded.last_error_msg
                """,
                (region, asg_name, source, now, (err or "")[:500]),
            )


def scan_region(region: str, start: datetime, end: datetime) -> int:
    session = boto3.Session(region_name=region)
    configs = discover_asgs(session)
    log.info(
        "[%s] discovered %d ASGs matching %s=%s",
        region,
        len(configs),
        DISCOVERY_TAG_KEY,
        DISCOVERY_TAG_VAL,
    )

    for cfg in configs:
        name = cfg["asg_name"]
        try:
            cfg.update(load_scaling_policies(session, name))
            mark_freshness(region, name, "tt_policy", ok=True, data_ts=int(time.time()))
            mark_freshness(region, name, "ps_policy", ok=True, data_ts=int(time.time()))
        except Exception as error:
            # Don't clobber known-good policy state with nulls on a transient
            # DescribePolicies failure. Fall back to whatever we last stored so
            # an ASG that genuinely has PS+TT isn't mislabeled as having neither.
            prior = _prior_policy_state(region, name)
            cfg.update(prior)
            mark_freshness(region, name, "tt_policy", ok=False, err=str(error))
            mark_freshness(region, name, "ps_policy", ok=False, err=str(error))
            log.exception("[%s] failed to load policies for %s", region, name)

    upsert_config(configs)
    for cfg in configs:
        mark_freshness(region, cfg["asg_name"], "asg_config", ok=True, data_ts=int(time.time()))

    for cfg in configs:
        name = cfg["asg_name"]
        target_groups = json.loads(cfg["tg_arns"])
        period_sec = cfg.get("metric_period_sec") or DEFAULT_METRIC_PERIOD_SEC
        try:
            upsert_target_groups(region, name, fetch_target_groups(session, target_groups))
        except Exception:
            log.exception("[%s] failed to resolve target groups for %s", region, name)
        try:
            rows = fetch_metrics(session, name, target_groups, start, end, period_sec)
            upsert_snapshots(rows, cfg.get("hourly_rate_usd") or 0.0, period_sec)
            newest = max((row["ts"] for row in rows), default=None)
            mark_freshness(region, name, "cw_metrics", ok=True, data_ts=newest)
            log.info(
                "[%s] %s: %d CloudWatch rows at Period=%ds (newest=%s)",
                region,
                name,
                len(rows),
                period_sec,
                newest,
            )
        except Exception as error:
            mark_freshness(region, name, "cw_metrics", ok=False, err=str(error))
            log.exception("[%s] failed to pull CloudWatch metrics for %s", region, name)

        if cfg.get("ps_enabled"):
            try:
                forecast_rows = fetch_ps_forecast(
                    session,
                    name,
                    start,
                    end + timedelta(hours=48),
                )
                upsert_ps_forecast(forecast_rows, region, name)
                newest_forecast = max((ts for ts, _ in forecast_rows), default=None)
                mark_freshness(
                    region,
                    name,
                    "ps_forecast",
                    ok=True,
                    data_ts=newest_forecast,
                )
                log.info(
                    "[%s] %s: %d predictive forecast points (newest=%s)",
                    region,
                    name,
                    len(forecast_rows),
                    newest_forecast,
                )
            except Exception as error:
                mark_freshness(region, name, "ps_forecast", ok=False, err=str(error))
                log.exception("[%s] failed to pull predictive forecast for %s", region, name)
    return len(configs)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.init_db()
    regions = configured_regions()
    log.info("scanning ASGs in regions: %s", ", ".join(regions))
    end = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(minutes=POLL_WINDOW_MIN)

    failures: list[str] = []
    total = 0
    for region in regions:
        try:
            total += scan_region(region, start, end)
        except Exception:
            failures.append(region)
            log.exception("[%s] regional ASG scan failed", region)

    log.info("multi-region scan completed: %d ASGs across %d regions", total, len(regions))
    if failures:
        raise RuntimeError(f"ASG scan failed in regions: {', '.join(failures)}")


if __name__ == "__main__":
    main()
