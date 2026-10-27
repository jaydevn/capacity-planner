import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = os.environ.get(
    "ASG_INSIGHTS_DB",
    str(Path(__file__).resolve().parent.parent / "asg_insights.db"),
)
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

_REGION_TABLES = (
    "asg_config",
    "asg_snapshots",
    "asg_snapshots_5m",
    "asg_snapshots_1h",
    "asg_monthly_rollup",
    "asg_recommendations",
    "ingestion_status",
)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _primary_key_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    columns = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [row["name"] for row in sorted(columns, key=lambda row: row["pk"]) if row["pk"]]


def _without_pragmas(ddl: str) -> str:
    return "\n".join(
        line for line in ddl.splitlines() if not line.lstrip().upper().startswith("PRAGMA ")
    )


def _migrate_region_keys(conn: sqlite3.Connection, ddl: str) -> None:
    """Rebuild the original single-region schema with region composite keys."""
    existing = [table for table in _REGION_TABLES if _table_exists(conn, table)]
    indexes = conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type='index' AND sql IS NOT NULL
          AND tbl_name IN ({})
        """.format(",".join("?" for _ in existing)),
        existing,
    ).fetchall()
    config_columns = {
        row["name"] for row in conn.execute('PRAGMA table_info("asg_config")').fetchall()
    }
    metric_period = "metric_period_sec" if "metric_period_sec" in config_columns else "300"

    statements = ["PRAGMA foreign_keys=OFF;", "BEGIN IMMEDIATE;"]
    statements.extend(f'DROP INDEX IF EXISTS "{row["name"]}";' for row in indexes)
    statements.extend(f'ALTER TABLE "{table}" RENAME TO "{table}_legacy";' for table in existing)
    statements.append(_without_pragmas(ddl))
    statements.append(
        f"""
        INSERT INTO asg_config
          (region, asg_name, instance_type, min_size, max_size, tt_target,
           ps_enabled, scale_in_enabled, tg_arns, tags, first_seen, last_seen,
           hourly_rate_usd, metric_period_sec)
        SELECT COALESCE(NULLIF(region, ''), 'us-east-1'), asg_name, instance_type,
               min_size, max_size, tt_target, ps_enabled, scale_in_enabled,
               tg_arns, tags, first_seen, last_seen, hourly_rate_usd, {metric_period}
        FROM asg_config_legacy;
        """
    )

    copy_specs = {
        "asg_snapshots": """
            INSERT INTO asg_snapshots
              (region, ts, asg_name, in_service, desired, alb_rcpt, ps_predicted,
               tt_high_state, tt_low_state, cost_hourly_usd)
            SELECT c.region, s.ts, s.asg_name, s.in_service, s.desired, s.alb_rcpt,
                   s.ps_predicted, s.tt_high_state, s.tt_low_state, s.cost_hourly_usd
            FROM asg_snapshots_legacy s
            JOIN asg_config_legacy c ON c.asg_name=s.asg_name;
        """,
        "asg_snapshots_5m": """
            INSERT INTO asg_snapshots_5m
              (region, ts, asg_name, in_service_avg, in_service_max, desired_avg,
               alb_rcpt_avg, alb_rcpt_max, ps_predicted_avg, cost_hourly_usd)
            SELECT c.region, s.ts, s.asg_name, s.in_service_avg, s.in_service_max,
                   s.desired_avg, s.alb_rcpt_avg, s.alb_rcpt_max,
                   s.ps_predicted_avg, s.cost_hourly_usd
            FROM asg_snapshots_5m_legacy s
            JOIN asg_config_legacy c ON c.asg_name=s.asg_name;
        """,
        "asg_snapshots_1h": """
            INSERT INTO asg_snapshots_1h
              (region, ts, asg_name, in_service_avg, in_service_max, desired_avg,
               alb_rcpt_avg, alb_rcpt_max, ps_predicted_avg, cost_hourly_usd)
            SELECT c.region, s.ts, s.asg_name, s.in_service_avg, s.in_service_max,
                   s.desired_avg, s.alb_rcpt_avg, s.alb_rcpt_max,
                   s.ps_predicted_avg, s.cost_hourly_usd
            FROM asg_snapshots_1h_legacy s
            JOIN asg_config_legacy c ON c.asg_name=s.asg_name;
        """,
        "asg_monthly_rollup": """
            INSERT INTO asg_monthly_rollup
              (region, month, asg_name, avg_in_service, peak_in_service,
               p95_utilization, over_prov_hours, sla_risk_minutes,
               estimated_cost_usd, prior_month_delta, computed_at)
            SELECT c.region, m.month, m.asg_name, m.avg_in_service,
                   m.peak_in_service, m.p95_utilization, m.over_prov_hours,
                   m.sla_risk_minutes, m.estimated_cost_usd,
                   m.prior_month_delta, m.computed_at
            FROM asg_monthly_rollup_legacy m
            JOIN asg_config_legacy c ON c.asg_name=m.asg_name;
        """,
        "asg_recommendations": """
            INSERT INTO asg_recommendations
              (id, region, asg_name, category, headline, detail,
               estimated_savings_usd, confidence, generated_at, status)
            SELECT r.id, c.region, r.asg_name, r.category, r.headline, r.detail,
                   r.estimated_savings_usd, r.confidence, r.generated_at, r.status
            FROM asg_recommendations_legacy r
            JOIN asg_config_legacy c ON c.asg_name=r.asg_name;
        """,
        "ingestion_status": """
            INSERT INTO ingestion_status
              (region, asg_name, source, last_success_ts, last_success_data_ts,
               last_error_ts, last_error_msg)
            SELECT c.region, i.asg_name, i.source, i.last_success_ts,
                   i.last_success_data_ts, i.last_error_ts, i.last_error_msg
            FROM ingestion_status_legacy i
            JOIN asg_config_legacy c ON c.asg_name=i.asg_name;
        """,
    }
    statements.extend(copy_specs[table] for table in existing if table in copy_specs)
    statements.extend(f'DROP TABLE "{table}_legacy";' for table in reversed(existing))
    statements.extend(["COMMIT;", "PRAGMA foreign_keys=ON;"])

    try:
        conn.executescript("\n".join(statements))
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.execute("PRAGMA foreign_keys=ON;")
        raise


def init_db() -> None:
    ddl = SCHEMA_PATH.read_text()
    with _connect() as conn:
        if _table_exists(conn, "asg_config") and _primary_key_columns(conn, "asg_config") == ["asg_name"]:
            _migrate_region_keys(conn, ddl)
        else:
            conn.executescript(ddl)


@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return list(conn.execute(sql, params))


def query_one(sql: str, params: tuple = ()):
    rows = query(sql, params)
    return rows[0] if rows else None
