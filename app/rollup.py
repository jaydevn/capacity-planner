"""Nightly retention, regional monthly rollups, and recommendations."""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta, timezone

from . import db

log = logging.getLogger("rollup")

RETENTION_1M = 180 * 86400
RETENTION_5M = 365 * 86400


def rollup_5m_from_1m() -> int:
    cutoff = int(time.time()) - RETENTION_1M
    with db.get_conn() as conn:
        conn.execute("BEGIN")
        conn.execute(
            """
            INSERT INTO asg_snapshots_5m
              (region, ts, asg_name, in_service_avg, in_service_max, desired_avg,
               alb_rcpt_avg, alb_rcpt_max, ps_predicted_avg, cost_hourly_usd)
            SELECT region, (ts / 300) * 300 AS bucket, asg_name,
                   AVG(in_service), MAX(in_service), AVG(desired),
                   AVG(alb_rcpt), MAX(alb_rcpt), AVG(ps_predicted),
                   AVG(cost_hourly_usd)
            FROM asg_snapshots
            WHERE ts < ?
            GROUP BY region, bucket, asg_name
            ON CONFLICT(region, ts, asg_name) DO NOTHING
            """,
            (cutoff,),
        )
        deleted = conn.execute("DELETE FROM asg_snapshots WHERE ts < ?", (cutoff,)).rowcount
        conn.execute("COMMIT")
    return deleted


def rollup_1h() -> int:
    cutoff = int(time.time()) - RETENTION_5M
    with db.get_conn() as conn:
        conn.execute("BEGIN")
        conn.execute(
            """
            INSERT INTO asg_snapshots_1h
              (region, ts, asg_name, in_service_avg, in_service_max, desired_avg,
               alb_rcpt_avg, alb_rcpt_max, ps_predicted_avg, cost_hourly_usd)
            SELECT region, (ts / 3600) * 3600 AS bucket, asg_name,
                   AVG(in_service_avg), MAX(in_service_max), AVG(desired_avg),
                   AVG(alb_rcpt_avg), MAX(alb_rcpt_max), AVG(ps_predicted_avg),
                   AVG(cost_hourly_usd)
            FROM asg_snapshots_5m
            WHERE ts < ?
            GROUP BY region, bucket, asg_name
            ON CONFLICT(region, ts, asg_name) DO NOTHING
            """,
            (cutoff,),
        )
        deleted = conn.execute("DELETE FROM asg_snapshots_5m WHERE ts < ?", (cutoff,)).rowcount
        conn.execute("COMMIT")
    return deleted


def compute_monthly(month: str) -> None:
    start = int(datetime.strptime(month + "-01", "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    start_dt = datetime.fromtimestamp(start, tz=timezone.utc)
    if start_dt.month == 12:
        end = int(datetime(start_dt.year + 1, 1, 1, tzinfo=timezone.utc).timestamp())
    else:
        end = int(datetime(start_dt.year, start_dt.month + 1, 1, tzinfo=timezone.utc).timestamp())

    with db.get_conn() as conn:
        asgs = conn.execute("SELECT region, asg_name FROM asg_config").fetchall()
        for item in asgs:
            region = item["region"]
            asg_name = item["asg_name"]
            cfg = conn.execute(
                """SELECT tt_target, hourly_rate_usd FROM asg_config
                   WHERE region=? AND asg_name=?""",
                (region, asg_name),
            ).fetchone()
            rows = conn.execute(
                """
                SELECT AVG(in_service) AS avg_inservice, MAX(in_service) AS peak,
                       AVG(alb_rcpt) AS avg_rcpt,
                       SUM(CASE WHEN alb_rcpt > ? THEN 1 ELSE 0 END) AS sla_risk_min,
                       SUM(cost_hourly_usd) / 60.0 AS cost
                FROM asg_snapshots
                WHERE region=? AND asg_name=? AND ts >= ? AND ts < ?
                UNION ALL
                SELECT AVG(in_service_avg), MAX(in_service_max), AVG(alb_rcpt_avg),
                       SUM(CASE WHEN alb_rcpt_max > ? THEN 5 ELSE 0 END),
                       SUM(cost_hourly_usd) / 12.0
                FROM asg_snapshots_5m
                WHERE region=? AND asg_name=? AND ts >= ? AND ts < ?
                UNION ALL
                SELECT AVG(in_service_avg), MAX(in_service_max), AVG(alb_rcpt_avg),
                       SUM(CASE WHEN alb_rcpt_max > ? THEN 60 ELSE 0 END),
                       SUM(cost_hourly_usd)
                FROM asg_snapshots_1h
                WHERE region=? AND asg_name=? AND ts >= ? AND ts < ?
                """,
                (
                    cfg["tt_target"] or 1e18, region, asg_name, start, end,
                    cfg["tt_target"] or 1e18, region, asg_name, start, end,
                    cfg["tt_target"] or 1e18, region, asg_name, start, end,
                ),
            ).fetchall()
            avg_inservice = next(
                (row["avg_inservice"] for row in rows if row["avg_inservice"] is not None),
                None,
            )
            if avg_inservice is None:
                continue
            peak = next((row["peak"] for row in rows if row["peak"] is not None), None)
            avg_rcpt = next((row["avg_rcpt"] for row in rows if row["avg_rcpt"] is not None), None)
            sla = sum((row["sla_risk_min"] or 0) for row in rows)
            cost = sum((row["cost"] or 0) for row in rows)
            utilization = (
                avg_rcpt / cfg["tt_target"]
                if avg_rcpt is not None and cfg["tt_target"]
                else None
            )
            prior = conn.execute(
                """SELECT estimated_cost_usd FROM asg_monthly_rollup
                   WHERE region=? AND asg_name=? AND month<?
                   ORDER BY month DESC LIMIT 1""",
                (region, asg_name, month),
            ).fetchone()
            delta = cost - prior["estimated_cost_usd"] if prior else None
            conn.execute(
                """
                INSERT INTO asg_monthly_rollup
                  (region, month, asg_name, avg_in_service, peak_in_service,
                   p95_utilization, over_prov_hours, sla_risk_minutes,
                   estimated_cost_usd, prior_month_delta, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(region, month, asg_name) DO UPDATE SET
                  avg_in_service=excluded.avg_in_service,
                  peak_in_service=excluded.peak_in_service,
                  p95_utilization=excluded.p95_utilization,
                  over_prov_hours=excluded.over_prov_hours,
                  sla_risk_minutes=excluded.sla_risk_minutes,
                  estimated_cost_usd=excluded.estimated_cost_usd,
                  prior_month_delta=excluded.prior_month_delta,
                  computed_at=excluded.computed_at
                """,
                (
                    region, month, asg_name, avg_inservice, int(peak or 0),
                    utilization, None, int(sla or 0), cost, delta, int(time.time()),
                ),
            )


def regenerate_recommendations() -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM asg_recommendations WHERE status='open'")
        rows = conn.execute(
            """
            SELECT c.region, c.asg_name, c.instance_type, c.min_size, c.max_size,
                   c.tt_target, c.ps_enabled, c.scale_in_enabled, c.hourly_rate_usd,
                   m.avg_in_service, m.peak_in_service, m.p95_utilization,
                   m.estimated_cost_usd
            FROM asg_config c
            LEFT JOIN asg_monthly_rollup m
              ON m.region=c.region AND m.asg_name=c.asg_name
             AND m.month = (
               SELECT MAX(month) FROM asg_monthly_rollup
               WHERE region=c.region AND asg_name=c.asg_name
             )
            """
        ).fetchall()
        now = int(time.time())
        for row in rows:
            region = row["region"]
            asg_name = row["asg_name"]

            # --- Close the utilization gap (the headline cost recommendation) ---
            # For a target-tracking ASG, p95_utilization = avg_rcpt / tt_target,
            # i.e. how close actual load runs to the configured ideal. When
            # utilization sits well below target, the group is carrying more
            # instances than demand needs. The "ideal" instance count scales
            # linearly with utilization: ideal = current * (actual / target).
            # The instances between current average and ideal are wasted, and
            # their on-demand cost (from the Pricing-API-backed rate) is the
            # estimated monthly saving.
            util = row["p95_utilization"]
            rate = row["hourly_rate_usd"] or 0.0
            if (
                row["tt_target"]
                and util is not None
                and 0 < util < 0.85
                and (row["avg_in_service"] or 0) > 0
            ):
                current = row["avg_in_service"]
                ideal = max(row["min_size"] or 1, math.ceil(current * util / 0.85))
                shed = max(0.0, current - ideal)
                savings = shed * rate * 730  # 730 h/month
                if savings > 1 and shed >= 1:
                    gap_pct = (0.85 - util)
                    confidence = "high" if gap_pct > 0.3 else "medium"
                    conn.execute(
                        """INSERT INTO asg_recommendations
                           (region, asg_name, category, headline, detail,
                            estimated_savings_usd, confidence, generated_at)
                           VALUES (?, ?, 'reduce_util_gap', ?, ?, ?, ?, ?)""",
                        (
                            region,
                            asg_name,
                            f"Right-size: utilization {util:.0%} vs 85% ideal",
                            (
                                f"Averaging {current:.1f} instances at {util:.0%} of the "
                                f"TT target ({row['tt_target']:g} req/inst/min). Closing the "
                                f"gap to an 85% ideal implies ~{ideal} instances, shedding "
                                f"~{shed:.1f}. At ${rate:.4f}/h (AWS Pricing API) that is the "
                                f"estimated monthly saving."
                            ),
                            savings,
                            confidence,
                            now,
                        ),
                    )

            if (
                row["p95_utilization"] is not None
                and row["p95_utilization"] < 0.4
                and (row["min_size"] or 0) > 2
            ):
                slack = max(
                    0,
                    (row["min_size"] or 0) - math.ceil((row["avg_in_service"] or 0) * 1.2),
                )
                savings = slack * (row["hourly_rate_usd"] or 0) * 730
                if savings > 5:
                    conn.execute(
                        """INSERT INTO asg_recommendations
                           (region, asg_name, category, headline, detail,
                            estimated_savings_usd, confidence, generated_at)
                           VALUES (?, ?, 'lower_min', ?, ?, ?, 'high', ?)""",
                        (
                            region,
                            asg_name,
                            f"Lower MIN from {row['min_size']} to ~{row['min_size'] - slack}",
                            f"Avg utilization {row['p95_utilization']:.0%}; floor is over-provisioned by ~{slack} instances.",
                            savings,
                            now,
                        ),
                    )
            if not row["ps_enabled"]:
                conn.execute(
                    """INSERT INTO asg_recommendations
                       (region, asg_name, category, headline, detail,
                        estimated_savings_usd, confidence, generated_at)
                       VALUES (?, ?, 'enable_ps', ?, ?, ?, 'medium', ?)""",
                    (
                        region,
                        asg_name,
                        "Enable Predictive Scaling (ForecastAndScale)",
                        "Predictive Scaling can pre-warm capacity before forecast demand.",
                        0,
                        now,
                    ),
                )
            if not row["scale_in_enabled"]:
                slack = max(
                    0,
                    (row["peak_in_service"] or 0)
                    - math.ceil((row["avg_in_service"] or 0) * 1.2),
                )
                savings = slack * (row["hourly_rate_usd"] or 0) * 730 * 0.5
                conn.execute(
                    """INSERT INTO asg_recommendations
                       (region, asg_name, category, headline, detail,
                        estimated_savings_usd, confidence, generated_at)
                       VALUES (?, ?, 'enable_scale_in', ?, ?, ?, 'high', ?)""",
                    (
                        region,
                        asg_name,
                        "Enable scale-in on target-tracking policy",
                        "Capacity can remain above demand when scale-in is disabled.",
                        savings,
                        now,
                    ),
                )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.init_db()
    log.info("1m→5m rollup: purged %d rows", rollup_5m_from_1m())
    log.info("5m→1h rollup: purged %d rows", rollup_1h())
    now = datetime.now(tz=timezone.utc)
    compute_monthly(now.strftime("%Y-%m"))
    compute_monthly((now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m"))
    regenerate_recommendations()
    log.info("done")


if __name__ == "__main__":
    main()
