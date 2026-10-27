"""Generate synthetic regional data so the dashboard renders without AWS."""
from __future__ import annotations

import math
import random
import time
from datetime import datetime, timezone

from . import db
from .rollup import compute_monthly, regenerate_recommendations

FAKE_REGION = "us-east-1"
FAKE_ASGS = [
    ("prod_ums_cy_t2", "m5.4xlarge", 10, 80, 3500, False, False, 0.768),
    ("prod_nv_ums_cy_r_common", "m5.4xlarge", 15, 64, 535, False, True, 0.768),
    ("prod_api_gateway", "c5.2xlarge", 6, 40, 1200, True, True, 0.340),
    ("prod_billing_worker", "m5.2xlarge", 4, 20, 800, True, True, 0.384),
    ("prod_search_indexer", "r5.2xlarge", 2, 12, 600, False, True, 0.504),
    ("prod_media_transcoder", "c5.2xlarge", 3, 30, 400, False, False, 0.340),
]


def seed_config() -> None:
    now = int(time.time())
    with db.get_conn() as conn:
        for name, instance_type, minimum, maximum, target, predictive, scale_in, rate in FAKE_ASGS:
            conn.execute(
                """
                INSERT OR REPLACE INTO asg_config
                  (region, asg_name, instance_type, min_size, max_size,
                   tt_target, ps_enabled, scale_in_enabled, tg_arns, tags,
                   first_seen, last_seen, hourly_rate_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]',
                        '{"Environment":"prod"}', ?, ?, ?)
                """,
                (
                    FAKE_REGION, name, instance_type, minimum, maximum, target,
                    int(predictive), int(scale_in), now - 30 * 86400, now, rate,
                ),
            )


def seed_target_groups() -> None:
    """Attach 1-2 synthetic target groups per ASG so the detail page renders."""
    now = int(time.time())
    with db.get_conn() as conn:
        conn.execute("DELETE FROM asg_target_groups")
        for name, *_ in FAKE_ASGS:
            short = name.replace("prod_", "")[:20]
            tgs = [(f"tg-{short}-http", "HTTP", 80)]
            if name.endswith(("gateway", "worker")):
                tgs.append((f"tg-{short}-grpc", "HTTP", 8080))
            for idx, (tg_name, proto, port) in enumerate(tgs):
                arn = (
                    f"arn:aws:elasticloadbalancing:{FAKE_REGION}:000000000000:"
                    f"targetgroup/{tg_name}/{idx:016x}"
                )
                lb = [
                    f"arn:aws:elasticloadbalancing:{FAKE_REGION}:000000000000:"
                    f"loadbalancer/app/alb-{short}/{idx:016x}"
                ]
                conn.execute(
                    """INSERT OR REPLACE INTO asg_target_groups
                       (region, asg_name, tg_arn, tg_name, protocol, port,
                        target_type, lb_arns, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'instance', ?, ?)""",
                    (FAKE_REGION, name, arn, tg_name, proto, port, __import__("json").dumps(lb), now),
                )


def seed_pricing() -> None:
    """Populate the pricing cache from the fake rates so savings math is non-zero."""
    now = int(time.time())
    with db.get_conn() as conn:
        seen: set[str] = set()
        for _name, instance_type, _mn, _mx, _t, _ps, _si, rate in FAKE_ASGS:
            if instance_type in seen:
                continue
            seen.add(instance_type)
            conn.execute(
                """INSERT OR REPLACE INTO pricing_cache
                   (region, instance_type, hourly_rate_usd, source, updated_at)
                   VALUES (?, ?, ?, 'fallback', ?)""",
                (FAKE_REGION, instance_type, rate, now),
            )


def diurnal(minute_of_day: int) -> float:
    value = (minute_of_day - 14 * 60) / (24 * 60)
    return 0.20 + 0.75 * max(0.0, math.cos(value * math.pi * 2) * 0.5 + 0.5)


def seed_series(days: int = 45) -> None:
    now = datetime.now(tz=timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = int(now.timestamp() - days * 86400)
    end = int(now.timestamp())
    with db.get_conn() as conn:
        for name, _instance_type, minimum, maximum, target, predictive, scale_in, rate in FAKE_ASGS:
            phase = random.uniform(0, 6.28)
            for ts in range(start, end, 3600):
                point = datetime.fromtimestamp(ts, tz=timezone.utc)
                weekend = 0.7 if point.weekday() >= 5 else 1.0
                shape = diurnal(point.hour * 60)
                demand = (minimum + (maximum - minimum) * shape * weekend) * (
                    0.9 + 0.2 * math.sin(ts / 43200 + phase)
                )
                if not scale_in:
                    # Scale-in disabled: instances stay high while real demand
                    # drops — the classic over-provisioned shape. Per-instance
                    # utilization runs well below target so reduce_util_gap fires.
                    demand = max(demand, minimum + (maximum - minimum) * 0.6)
                    util_frac = 0.45 + 0.20 * shape  # ~45-65% of target
                else:
                    util_frac = 0.60 + 0.50 * shape  # ~60-110% of target
                in_service = int(min(maximum, max(minimum, round(demand))))
                request_count = target * util_frac * random.uniform(0.9, 1.1)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO asg_snapshots_1h
                      (region, ts, asg_name, in_service_avg, in_service_max,
                       desired_avg, alb_rcpt_avg, alb_rcpt_max,
                       ps_predicted_avg, cost_hourly_usd)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        FAKE_REGION, ts, name, in_service, in_service, in_service,
                        request_count, request_count * 1.15,
                        in_service if predictive else None, in_service * rate,
                    ),
                )


def seed_freshness() -> None:
    now = int(time.time())
    with db.get_conn() as conn:
        for name, *_ in FAKE_ASGS:
            for source, offset in (
                ("asg_config", 90),
                ("cw_metrics", 300),
                ("ps_policy", 90),
                ("tt_policy", 90),
            ):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO ingestion_status
                      (region, asg_name, source, last_success_ts, last_success_data_ts)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (FAKE_REGION, name, source, now - offset, now - offset),
                )


def main() -> None:
    db.init_db()
    seed_config()
    seed_target_groups()
    seed_pricing()
    seed_series()
    seed_freshness()
    now = datetime.now(tz=timezone.utc)
    for month_offset in range(3):
        year, month = now.year, now.month - month_offset
        while month <= 0:
            month += 12
            year -= 1
        compute_monthly(f"{year:04d}-{month:02d}")
    regenerate_recommendations()
    print("seed complete — try `flask --app app.main run --port 8080`")


if __name__ == "__main__":
    main()
