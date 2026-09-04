from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from psycopg import Connection
from psycopg.types.json import Jsonb

from .contracts import CheckStatus, DatasetRunIn, RuleIn, ScopeIn


def target_key(row: dict[str, Any]) -> str:
    return "|".join(
        str(row[key]) for key in ("source", "store_id", "marketplace", "dataset")
    )


def register_scope(conn: Connection, scope: ScopeIn) -> None:
    conn.execute(
        """INSERT INTO data_scopes(source, store_id, marketplace, dataset)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (source, store_id, marketplace, dataset)
           DO UPDATE SET enabled = TRUE""",
        (scope.source, scope.store_id, scope.marketplace, scope.dataset),
    )


def upsert_rule(conn: Connection, rule: RuleIn) -> None:
    conn.execute(
        """INSERT INTO quality_rules(
               rule_code, check_type, dataset, source, store_id, marketplace,
               severity, config, enabled
           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (rule_code) DO UPDATE SET
               check_type = EXCLUDED.check_type,
               dataset = EXCLUDED.dataset,
               source = EXCLUDED.source,
               store_id = EXCLUDED.store_id,
               marketplace = EXCLUDED.marketplace,
               severity = EXCLUDED.severity,
               config = EXCLUDED.config,
               enabled = EXCLUDED.enabled,
               updated_at = NOW()""",
        (
            rule.rule_code,
            rule.check_type,
            rule.dataset,
            rule.source,
            rule.store_id,
            rule.marketplace,
            rule.severity,
            Jsonb(rule.config),
            rule.enabled,
        ),
    )


def ingest_run(conn: Connection, run: DatasetRunIn) -> dict[str, Any]:
    scope = ScopeIn(
        source=run.source,
        store_id=run.store_id,
        marketplace=run.marketplace,
        dataset=run.dataset,
    )
    register_scope(conn, scope)
    if run.external_run_id:
        existing = conn.execute(
            """SELECT id, accepted, rejection_reason FROM dataset_runs
               WHERE source=%s AND external_run_id=%s""",
            (run.source, run.external_run_id),
        ).fetchone()
        if existing:
            return {
                "run_id": str(existing["id"]),
                "accepted": existing["accepted"],
                "rejection_reason": existing["rejection_reason"],
                "duplicate": True,
            }
    version_at = run.source_updated_at or run.fetched_at
    current = conn.execute(
        """SELECT version_at FROM current_dataset_state
           WHERE source=%s AND store_id=%s AND marketplace=%s AND dataset=%s
           FOR UPDATE""",
        (run.source, run.store_id, run.marketplace, run.dataset),
    ).fetchone()
    accepted = current is None or version_at >= current["version_at"]
    rejection_reason = None if accepted else "older_source_version"
    run_id = uuid4()
    conn.execute(
        """INSERT INTO dataset_runs(
               id, external_run_id, source, store_id, marketplace, dataset, business_date,
               fetched_at, source_updated_at, version_at, ingestion_status,
               source_count, normalized_count, duplicate_count, error_count, checksum,
               timezone, currency, raw_reference, schema_version,
               formula_version, is_provisional, correction_of_run_id,
               metadata, accepted, rejection_reason
           ) VALUES (
               %(id)s, %(external_run_id)s, %(source)s, %(store_id)s,
               %(marketplace)s, %(dataset)s, %(business_date)s,
               %(fetched_at)s, %(source_updated_at)s, %(version_at)s,
               %(ingestion_status)s, %(source_count)s, %(normalized_count)s,
               %(duplicate_count)s, %(error_count)s, %(checksum)s,
               %(timezone)s, %(currency)s, %(raw_reference)s, %(schema_version)s,
               %(formula_version)s, %(is_provisional)s, %(correction_of_run_id)s,
               %(metadata)s, %(accepted)s, %(rejection_reason)s
           )""",
        {
            "id": run_id,
            "external_run_id": run.external_run_id,
            "source": run.source,
            "store_id": run.store_id,
            "marketplace": run.marketplace,
            "dataset": run.dataset,
            "business_date": run.business_date,
            "fetched_at": run.fetched_at,
            "source_updated_at": run.source_updated_at,
            "version_at": version_at,
            "ingestion_status": run.ingestion_status,
            "source_count": run.source_count,
            "normalized_count": run.normalized_count,
            "duplicate_count": run.duplicate_count,
            "error_count": run.error_count,
            "checksum": run.checksum,
            "timezone": run.timezone,
            "currency": run.currency,
            "raw_reference": run.raw_reference,
            "schema_version": run.schema_version,
            "formula_version": run.formula_version,
            "is_provisional": run.is_provisional,
            "correction_of_run_id": run.correction_of_run_id,
            "metadata": Jsonb(run.metadata),
            "accepted": accepted,
            "rejection_reason": rejection_reason,
        },
    )
    if accepted:
        conn.execute(
            """INSERT INTO current_dataset_state(
                   source, store_id, marketplace, dataset, run_id,
                   business_date, fetched_at, source_updated_at, version_at,
                   ingestion_status, source_count, normalized_count,
                   duplicate_count, error_count, checksum
               ) VALUES (
                   %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
               )
               ON CONFLICT (source, store_id, marketplace, dataset) DO UPDATE SET
                   run_id=EXCLUDED.run_id,
                   business_date=EXCLUDED.business_date,
                   fetched_at=EXCLUDED.fetched_at,
                   source_updated_at=EXCLUDED.source_updated_at,
                   version_at=EXCLUDED.version_at,
                   ingestion_status=EXCLUDED.ingestion_status,
                   source_count=EXCLUDED.source_count,
                   normalized_count=EXCLUDED.normalized_count,
                   duplicate_count=EXCLUDED.duplicate_count,
                   error_count=EXCLUDED.error_count,
                   checksum=EXCLUDED.checksum,
                   updated_at=NOW()""",
            (
                run.source,
                run.store_id,
                run.marketplace,
                run.dataset,
                run_id,
                run.business_date,
                run.fetched_at,
                run.source_updated_at,
                version_at,
                run.ingestion_status,
                run.source_count,
                run.normalized_count,
                run.duplicate_count,
                run.error_count,
                run.checksum,
            ),
        )
    return {"run_id": str(run_id), "accepted": accepted, "rejection_reason": rejection_reason}


def _evaluate(
    rule: dict[str, Any], state: dict[str, Any] | None, latest_run: dict[str, Any] | None
) -> tuple[CheckStatus, dict[str, Any]]:
    check_type = rule["check_type"]
    config = rule["config"] or {}
    if state is None:
        if check_type == "freshness":
            return CheckStatus.FAILED, {"reason": "no_data_received"}
        return CheckStatus.SKIPPED, {"reason": "no_current_data"}

    if check_type == "freshness":
        max_age_minutes = int(config.get("max_age_minutes", 180))
        now = datetime.now(timezone.utc)
        age_minutes = max(0, int((now - state["fetched_at"]).total_seconds() // 60))
        status = CheckStatus.PASSED if age_minutes <= max_age_minutes else CheckStatus.FAILED
        return status, {"age_minutes": age_minutes, "max_age_minutes": max_age_minutes}

    if check_type == "completeness":
        complete = state["ingestion_status"] == "complete" and state["error_count"] == 0
        status = CheckStatus.PASSED if complete else CheckStatus.FAILED
        return status, {
            "ingestion_status": state["ingestion_status"],
            "error_count": state["error_count"],
        }

    if check_type == "reconciliation":
        accounted = (
            state["normalized_count"]
            + state["duplicate_count"]
            + state["error_count"]
        )
        delta = state["source_count"] - accounted
        status = CheckStatus.PASSED if delta == 0 else CheckStatus.FAILED
        return status, {
            "source_count": state["source_count"],
            "normalized_count": state["normalized_count"],
            "duplicate_count": state["duplicate_count"],
            "error_count": state["error_count"],
            "unaccounted_count": delta,
        }

    if check_type == "ordering":
        if latest_run is None:
            return CheckStatus.SKIPPED, {"reason": "no_ingestion_attempt"}
        status = CheckStatus.PASSED if latest_run["accepted"] else CheckStatus.FAILED
        return status, {
            "latest_run_id": str(latest_run["id"]),
            "accepted": latest_run["accepted"],
            "rejection_reason": latest_run["rejection_reason"],
        }

    raise ValueError(f"unsupported check type: {check_type}")


def run_checks(conn: Connection) -> dict[str, Any]:
    rows = conn.execute(
        """SELECT
               r.rule_code, r.check_type, r.severity, r.config,
               s.source, s.store_id, s.marketplace, s.dataset,
               c.run_id, c.business_date, c.fetched_at, c.source_updated_at,
               c.version_at, c.ingestion_status, c.source_count,
               c.normalized_count, c.duplicate_count, c.error_count
           FROM quality_rules r
           JOIN data_scopes s
             ON s.dataset = r.dataset AND s.enabled
            AND (r.source IS NULL OR r.source=s.source)
            AND (r.store_id IS NULL OR r.store_id=s.store_id)
            AND (r.marketplace IS NULL OR r.marketplace=s.marketplace)
           LEFT JOIN current_dataset_state c
             ON c.source=s.source AND c.store_id=s.store_id
            AND c.marketplace=s.marketplace AND c.dataset=s.dataset
           WHERE r.enabled
           ORDER BY r.rule_code, s.store_id, s.marketplace"""
    ).fetchall()
    counts = {status.value: 0 for status in CheckStatus}
    for row in rows:
        state = dict(row) if row["run_id"] is not None else None
        latest_run = conn.execute(
            """SELECT id, accepted, rejection_reason FROM dataset_runs
               WHERE source=%s AND store_id=%s AND marketplace=%s AND dataset=%s
               ORDER BY arrival_sequence DESC LIMIT 1""",
            (row["source"], row["store_id"], row["marketplace"], row["dataset"]),
        ).fetchone()
        try:
            status, details = _evaluate(dict(row), state, latest_run)
        except Exception as exc:  # checker failures are data, not false business failures
            status = CheckStatus.ERROR
            details = {"error_type": type(exc).__name__, "message": str(exc)}
        conn.execute(
            """INSERT INTO quality_check_events(
                   rule_code, target_key, source, store_id, marketplace,
                   dataset, target_date, check_status, severity, details
               ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                row["rule_code"],
                target_key(dict(row)),
                row["source"],
                row["store_id"],
                row["marketplace"],
                row["dataset"],
                row["business_date"] if state else None,
                status.value,
                row["severity"],
                Jsonb(details),
            ),
        )
        counts[status.value] += 1
    return {"evaluated": len(rows), **counts}


def health_summary(conn: Connection) -> dict[str, Any]:
    status_rows = conn.execute(
        "SELECT check_status, COUNT(*) AS count FROM v_latest_checks GROUP BY check_status"
    ).fetchall()
    counts = {status.value: 0 for status in CheckStatus}
    counts.update({row["check_status"]: row["count"] for row in status_rows})
    open_count = conn.execute("SELECT COUNT(*) AS n FROM v_open_issues").fetchone()["n"]
    recovered = conn.execute("SELECT COUNT(*) AS n FROM v_recovered_issues").fetchone()["n"]
    last_checked = conn.execute(
        "SELECT MAX(evaluated_at) AS value FROM quality_check_events"
    ).fetchone()["value"]
    expected = conn.execute(
        """SELECT COUNT(*) AS n FROM quality_rules r
           JOIN data_scopes s
             ON s.dataset=r.dataset
            AND (r.source IS NULL OR r.source=s.source)
            AND (r.store_id IS NULL OR r.store_id=s.store_id)
            AND (r.marketplace IS NULL OR r.marketplace=s.marketplace)
           WHERE r.enabled AND s.enabled"""
    ).fetchone()["n"]
    checked = sum(counts.values())

    if checked == 0:
        tone, headline = "unknown", "还没有数据健康结果"
    elif counts["error"]:
        tone, headline = "critical", f"{counts['error']} 项检查执行出错"
    elif counts["failed"] or open_count:
        tone, headline = "critical", f"有 {open_count} 个数据问题待处理"
    elif counts["skipped"] or checked < expected:
        tone, headline = "warning", "部分数据暂时无法判断"
    else:
        tone, headline = "clear", "经营数据可以正常使用"
    return {
        "tone": tone,
        "headline": headline,
        "expected_checks": expected,
        "checked": checked,
        **counts,
        "open_issues": open_count,
        "recovered_historical": recovered,
        "last_checked_at": last_checked.isoformat() if last_checked else None,
    }
