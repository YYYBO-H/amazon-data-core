from __future__ import annotations

import os


def database_url() -> str:
    return os.getenv(
        "DATABASE_URL",
        "postgresql://data_core:data_core@localhost:55432/data_core",
    )
