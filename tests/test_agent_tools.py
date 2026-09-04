from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

from amazon_data_core.agent_tools import json_safe


def test_json_safe_converts_database_values_for_agent_hosts():
    value = {
        "date": date(2026, 9, 4),
        "time": datetime(2026, 9, 4, 1, 2, tzinfo=timezone.utc),
        "id": UUID("11111111-1111-1111-1111-111111111111"),
        "amount": Decimal("12.30"),
        "nested": [date(2026, 9, 3)],
    }

    assert json_safe(value) == {
        "date": "2026-09-04",
        "time": "2026-09-04T01:02:00+00:00",
        "id": "11111111-1111-1111-1111-111111111111",
        "amount": "12.30",
        "nested": ["2026-09-03"],
    }
