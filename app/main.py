"""Read-only Flask dashboard backed entirely by regional SQLite data."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from flask import Flask, abort, jsonify, render_template, request

from . import db

app = Flask(__name__)


@app.template_filter("from_json")
def _from_json(value):
    """Parse a JSON string stored in SQLite into a Python object for templates."""
    import json as _json

    if not value:
        return []
    try:
        return _json.loads(value)
    except (ValueError, TypeError):
        return []

SOURCE_LABELS = {
    "asg_config": "ASG config",
    "cw_metrics": "CloudWatch metrics",
    "ps_policy": "PS policy",
    "ps_forecast": "PS forecast",
    "tt_policy": "Target Tracking",
}
STALE_AFTER_SEC = 30 * 60


def _month_key(ts: int | None = None) -> str:
    dt = datetime.fromtimestamp(ts or time.time(), tz=timezone.utc)
    return dt.strftime("%Y-%m")


@app.route("/")
def dashboard():
    month = request.args.get("month", _month_key())
    fleet = db.query(
        """
        SELECT m.*, c.instance_type, c.min_size, c.max_size, c.tt_target,
               c.ps_enabled, c.scale_in_enabled
        FROM asg_monthly_rollup m
        JOIN asg_config c USING (region, asg_name)
        WHERE m.month = ?
        ORDER BY m.estimated_cost_usd DESC
        """,
        (month,),
    )
    totals = db.query_one(
        """
        SELECT SUM(m.peak_in_service) AS peak,
               AVG(m.p95_utilization) AS util,
               COUNT(*) AS asg_count
        FROM asg_monthly_rollup m WHERE m.month = ?
        """,
        (month,),
    )
    # ASGs that have BOTH predictive scaling and target tracking enabled.
    ps_tt = db.query_one(
        """
        SELECT COUNT(*) AS n
        FROM asg_config
        WHERE ps_enabled = 1 AND tt_target IS NOT NULL
        """
    )
    # Count of ASGs that currently have at least one open recommendation,
    # so the fleet KPI can link to where the opportunities are.
    opps = db.query_one(
        "SELECT COUNT(DISTINCT region || '/' || asg_name) AS n "
        "FROM asg_recommendations WHERE status='open'"
    )
    months = [
        row["month"]
        for row in db.query(
            "SELECT DISTINCT month FROM asg_monthly_rollup ORDER BY month DESC LIMIT 24"
        )
    ]
    return render_template(
        "dashboard.html",
        month=month,
        months=months,
        fleet=fleet,
        totals=totals,
        ps_tt_count=ps_tt["n"] if ps_tt else 0,
        opportunity_count=opps["n"] if opps else 0,
    )


@app.route("/asg/<region>/<path:name>")
def asg_detail(region: str, name: str):
    cfg = db.query_one(
        "SELECT * FROM asg_config WHERE region=? AND asg_name=?",
        (region, name),
    )
    if not cfg:
        abort(404)
    monthly = db.query(
        """SELECT * FROM asg_monthly_rollup
           WHERE region=? AND asg_name=? ORDER BY month DESC LIMIT 12""",
        (region, name),
    )
    target_groups = db.query(
        """SELECT tg_name, protocol, port, target_type, lb_arns, tg_arn
           FROM asg_target_groups WHERE region=? AND asg_name=?
           ORDER BY tg_name""",
        (region, name),
    )
    recs = db.query(
        """SELECT * FROM asg_recommendations
           WHERE region=? AND asg_name=? AND status='open'
           ORDER BY estimated_savings_usd DESC""",
        (region, name),
    )
    util = _utilization_stats(region, name, cfg["tt_target"])
    return render_template(
        "asg_detail.html",
        cfg=cfg,
        monthly=monthly,
        target_groups=target_groups,
        recs=recs,
        rec_savings_total=sum(r["estimated_savings_usd"] or 0 for r in recs),
        util=util,
        per_asg_freshness=_freshness_rows(region, name),
    )


def _utilization_stats(region: str, name: str, tt_target: float | None) -> dict:
    """Avg/peak utilization (actual req-per-target vs the TT target) over 30d.

    Utilization is expressed as a fraction of the configured target-tracking
    target: 1.0 means running exactly at target, <1.0 means over-provisioned.
    """
    since = int(time.time()) - 30 * 86400
    row = db.query_one(
        """
        SELECT AVG(alb_rcpt_avg) AS avg_rcpt, MAX(alb_rcpt_max) AS peak_rcpt,
               AVG(in_service_avg) AS avg_inst, MAX(in_service_max) AS peak_inst
        FROM asg_snapshots_1h
        WHERE region=? AND asg_name=? AND ts>=?
        """,
        (region, name, since),
    )
    avg_rcpt = row["avg_rcpt"] if row else None
    peak_rcpt = row["peak_rcpt"] if row else None
    avg_util = (avg_rcpt / tt_target) if (avg_rcpt is not None and tt_target) else None
    peak_util = (peak_rcpt / tt_target) if (peak_rcpt is not None and tt_target) else None
    return {
        "avg_rcpt": avg_rcpt,
        "peak_rcpt": peak_rcpt,
        "avg_util": avg_util,
        "peak_util": peak_util,
        "avg_inst": row["avg_inst"] if row else None,
        "peak_inst": row["peak_inst"] if row else None,
        "tt_target": tt_target,
    }


@app.route("/recommendations")
def recommendations():
    recs = db.query(
        """
        SELECT r.*, c.instance_type
        FROM asg_recommendations r
        JOIN asg_config c USING (region, asg_name)
        WHERE r.status = 'open'
        ORDER BY r.estimated_savings_usd DESC
        """
    )
    total_savings = sum(row["estimated_savings_usd"] or 0 for row in recs)
    return render_template("recommendations.html", recs=recs, total_savings=total_savings)


@app.route("/scenarios")
def scenarios():
    return render_template("scenarios.html")


@app.route("/api/asg/<region>/<path:name>/timeseries")
def api_timeseries(region: str, name: str):
    window = request.args.get("range", "7d")
    now = int(time.time())
    seconds = {
        "1d": 86400,
        "7d": 7 * 86400,
        "30d": 30 * 86400,
        "90d": 90 * 86400,
        "1y": 365 * 86400,
    }.get(window, 7 * 86400)
    since = now - seconds
    cols_rollup = (
        "in_service_avg AS in_service, in_service_max, "
        "desired_avg AS desired, alb_rcpt_avg AS alb_rcpt, "
        "ps_predicted_avg AS ps_predicted, cost_hourly_usd"
    )
    cols_1m = (
        "in_service, in_service AS in_service_max, desired, alb_rcpt, "
        "ps_predicted, cost_hourly_usd"
    )
    if seconds <= 7 * 86400:
        preference = [
            ("asg_snapshots", cols_1m),
            ("asg_snapshots_5m", cols_rollup),
            ("asg_snapshots_1h", cols_rollup),
        ]
    elif seconds <= 30 * 86400:
        preference = [
            ("asg_snapshots_5m", cols_rollup),
            ("asg_snapshots_1h", cols_rollup),
        ]
    else:
        preference = [
            ("asg_snapshots_1h", cols_rollup),
            ("asg_snapshots_5m", cols_rollup),
        ]

    def query_table(table: str, columns: str) -> list[dict]:
        rows = db.query(
            f"""SELECT ts, {columns} FROM {table}
                WHERE region=? AND asg_name=? AND ts>=? ORDER BY ts""",
            (region, name, since),
        )
        return [dict(row) for row in rows]

    used_table = preference[0][0]
    points: list[dict] = []
    for table, columns in preference:
        points = query_table(table, columns)
        if points:
            used_table = table
            break
    return jsonify({
        "region": region,
        "asg_name": name,
        "range": window,
        "table": used_table,
        "points": points,
    })


@app.route("/api/fleet/summary")
def api_fleet_summary():
    since = int(time.time()) - 90 * 86400
    rows = db.query(
        """
        SELECT ts, SUM(in_service_avg) AS instances,
               SUM(cost_hourly_usd) AS cost_hourly
        FROM asg_snapshots_1h
        WHERE ts >= ?
        GROUP BY ts
        ORDER BY ts
        """,
        (since,),
    )
    return jsonify({"points": [dict(row) for row in rows]})


def _freshness_rows(region: str | None = None, asg_name: str | None = None) -> list[dict]:
    now = int(time.time())
    if region and asg_name:
        rows = db.query(
            """
            SELECT source, MAX(last_success_ts) AS success_ts,
                   MAX(last_success_data_ts) AS data_ts,
                   MAX(last_error_ts) AS error_ts, last_error_msg
            FROM ingestion_status
            WHERE region=? AND asg_name=?
            GROUP BY source
            """,
            (region, asg_name),
        )
    else:
        rows = db.query(
            """
            SELECT source, MIN(last_success_ts) AS success_ts,
                   MIN(last_success_data_ts) AS data_ts,
                   MAX(last_error_ts) AS error_ts, last_error_msg
            FROM ingestion_status
            GROUP BY source
            """
        )
    output = []
    for row in rows:
        item = dict(row)
        item["label"] = SOURCE_LABELS.get(item["source"], item["source"])
        item["age_sec"] = now - item["success_ts"] if item["success_ts"] else None
        item["stale"] = item["age_sec"] is None or item["age_sec"] > STALE_AFTER_SEC
        output.append(item)
    return output


@app.context_processor
def inject_freshness():
    try:
        rows = _freshness_rows()
    except Exception:
        rows = []
    any_stale = any(row["stale"] for row in rows) if rows else True
    return {
        "freshness": rows,
        "freshness_stale": any_stale,
        "fmt_age": _fmt_age,
    }


def _fmt_age(sec: int | None) -> str:
    if sec is None:
        return "never"
    if sec < 90:
        return f"{sec}s ago"
    if sec < 60 * 90:
        return f"{sec // 60}m ago"
    if sec < 86400:
        return f"{sec // 3600}h {(sec % 3600) // 60}m ago"
    return f"{sec // 86400}d ago"


@app.route("/api/freshness")
def api_freshness():
    return jsonify({
        "sources": _freshness_rows(
            request.args.get("region"),
            request.args.get("asg"),
        )
    })


@app.route("/healthz")
def healthz():
    try:
        db.query_one("SELECT 1")
        return jsonify({"ok": True})
    except Exception as error:  # pragma: no cover
        return jsonify({"ok": False, "error": str(error)}), 500


if __name__ == "__main__":
    db.init_db()
    app.run(host="0.0.0.0", port=8080, debug=True)
