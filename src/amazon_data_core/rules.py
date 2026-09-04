from __future__ import annotations

import hashlib

from psycopg import Connection

from .contracts import RuleIn
from .engine import upsert_rule

DEFAULT_RULES = (
    ("FRESH", "freshness", "critical"),
    ("COMPLETE", "completeness", "critical"),
    ("RECON", "reconciliation", "warning"),
    ("ORDER", "ordering", "warning"),
)


def rule_suffix(dataset: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in dataset.upper())


def scoped_rule_code(prefix: str, dataset: str, selector: str) -> str:
    readable = rule_suffix(f"{prefix}_{dataset}_{selector}")
    if len(readable) <= 60:
        return readable
    digest = hashlib.sha256(readable.encode()).hexdigest()[:8].upper()
    return f"{readable[:51]}_{digest}"


def ensure_default_rules(
    conn: Connection,
    dataset: str,
    *,
    max_age_minutes: int = 180,
    source: str | None = None,
    store_id: str | None = None,
    marketplace: str | None = None,
) -> None:
    selector = "_".join(value or "ALL" for value in (source, store_id, marketplace))
    for prefix, check_type, severity in DEFAULT_RULES:
        config = {"max_age_minutes": max_age_minutes} if check_type == "freshness" else {}
        upsert_rule(
            conn,
            RuleIn(
                rule_code=scoped_rule_code(prefix, dataset, selector),
                check_type=check_type,
                dataset=dataset,
                source=source,
                store_id=store_id,
                marketplace=marketplace,
                severity=severity,
                config=config,
            ),
        )
