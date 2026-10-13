from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import pymysql
from pymysql.constants import CLIENT
from pymysql.connections import Connection
from pymysql.cursors import DictCursor

from .config import DatabaseConfig, load_secret


class DatabaseError(RuntimeError):
    pass


class DatabaseSession:
    def __init__(self, connection: Connection):
        self.connection = connection
        self.cursor: DictCursor = connection.cursor()
        self.rollback_confirmed = False

    def query(
        self,
        sql: str,
        params: Mapping[str, Any] | Sequence[Any] | None = None,
        *,
        max_rows: int = 500,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(max_rows, 1000))
        self.cursor.execute(sql, params)
        rows = list(self.cursor.fetchmany(limit + 1))
        if len(rows) > limit:
            raise DatabaseError(f"Query exceeded the bounded result limit of {limit} rows")
        return rows

    def one(
        self,
        sql: str,
        params: Mapping[str, Any] | Sequence[Any] | None = None,
    ) -> dict[str, Any] | None:
        rows = self.query(sql, params, max_rows=1)
        return rows[0] if rows else None

    def execute(
        self,
        sql: str,
        params: Mapping[str, Any] | Sequence[Any] | None = None,
    ) -> int:
        return int(self.cursor.execute(sql, params))

    def close(self) -> None:
        self.cursor.close()


class EditorDatabase:
    def __init__(self, config: DatabaseConfig):
        self.config = config

    def _connect(self) -> Connection:
        try:
            return pymysql.connect(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=load_secret(self.config.credential_target, self.config.user),
                database=self.config.database,
                charset="utf8mb4",
                cursorclass=DictCursor,
                autocommit=False,
                connect_timeout=self.config.connect_timeout_seconds,
                read_timeout=self.config.read_timeout_seconds,
                write_timeout=self.config.write_timeout_seconds,
                client_flag=CLIENT.FOUND_ROWS,
            )
        except Exception as exc:
            message = str(exc)
            for sensitive in (self.config.host, self.config.user, self.config.database):
                if sensitive:
                    message = message.replace(sensitive, "<configured>")
            raise DatabaseError("Database connection failed: " + (message or type(exc).__name__)) from exc

    @contextlib.contextmanager
    def read_session(self) -> Iterator[DatabaseSession]:
        connection = self._connect()
        session = DatabaseSession(connection)
        try:
            session.execute("SET SESSION MAX_EXECUTION_TIME = 5000")
            session.execute("START TRANSACTION READ ONLY")
            yield session
        finally:
            with contextlib.suppress(Exception):
                connection.rollback()
            with contextlib.suppress(Exception):
                session.close()
            connection.close()

    @contextlib.contextmanager
    def write_transaction(self) -> Iterator[DatabaseSession]:
        connection = self._connect()
        session = DatabaseSession(connection)
        commit_attempted = False
        try:
            session.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
            session.execute("SET SESSION innodb_lock_wait_timeout = 3")
            session.execute("START TRANSACTION")
            yield session
            commit_attempted = True
            connection.commit()
        except Exception:
            connection.rollback()
            # A failed COMMIT acknowledgement can hide a successful commit.
            session.rollback_confirmed = not commit_attempted
            raise
        finally:
            with contextlib.suppress(Exception):
                session.close()
            connection.close()

    def status(self) -> dict[str, Any]:
        with self.read_session() as session:
            row = session.one("SELECT VERSION() AS server_version")
        return {"connected": True, "server_version": (row or {}).get("server_version")}
