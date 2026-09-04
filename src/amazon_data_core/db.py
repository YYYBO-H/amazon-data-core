from __future__ import annotations

from contextlib import contextmanager
from importlib.resources import files
from typing import Iterator

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from .config import database_url


@contextmanager
def connect() -> Iterator[Connection]:
    with psycopg.connect(database_url(), row_factory=dict_row) as conn:
        yield conn


def migrate() -> None:
    schema = files("amazon_data_core").joinpath("schema.sql").read_text()
    with connect() as conn:
        conn.execute(schema)
