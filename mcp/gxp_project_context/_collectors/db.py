from __future__ import annotations

import contextlib
import queue
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import pymysql
from pymysql.connections import Connection
from pymysql.cursors import DictCursor

from .config import DatabaseConfig, default_config_path, load_config, load_password
from .sql_guard import GuardResult, validate_readonly_sql


class DatabaseExecutionError(RuntimeError):
    pass


def _redacted_connection_error(exc: Exception, config: DatabaseConfig) -> str:
    message = str(exc)
    for value in (config.host, config.user, config.database):
        if value:
            message = message.replace(value, "<configured>")
    return message or type(exc).__name__


class ConnectionPool:
    def __init__(self, config: DatabaseConfig):
        self.config = config
        self._idle: queue.LifoQueue[Connection] = queue.LifoQueue(config.pool_size)
        self._created = 0
        self._lock = threading.Lock()

    def _create(self) -> Connection:
        password = load_password(self.config)
        try:
            return pymysql.connect(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=password,
                database=self.config.database,
                charset="utf8mb4",
                autocommit=False,
                cursorclass=DictCursor,
                connect_timeout=self.config.connect_timeout_seconds,
                read_timeout=self.config.read_timeout_seconds,
                write_timeout=self.config.read_timeout_seconds,
            )
        except Exception as exc:
            raise DatabaseExecutionError(
                "Database connection failed: "
                + _redacted_connection_error(exc, self.config)
            ) from exc

    def acquire(self, timeout: float = 15.0) -> Connection:
        try:
            connection = self._idle.get_nowait()
        except queue.Empty:
            create_new = False
            with self._lock:
                if self._created < self.config.pool_size:
                    self._created += 1
                    create_new = True
            if create_new:
                try:
                    connection = self._create()
                except Exception:
                    with self._lock:
                        self._created = max(0, self._created - 1)
                    raise
            else:
                try:
                    connection = self._idle.get(timeout=timeout)
                except queue.Empty as exc:
                    raise DatabaseExecutionError(
                        "Timed out waiting for a read-only database connection"
                    ) from exc
        try:
            connection.ping(reconnect=True)
            return connection
        except Exception:
            self.discard(connection)
            with self._lock:
                self._created += 1
            try:
                return self._create()
            except Exception:
                with self._lock:
                    self._created = max(0, self._created - 1)
                raise

    def release(self, connection: Connection) -> None:
        try:
            connection.rollback()
            self._idle.put_nowait(connection)
        except Exception:
            self.discard(connection)

    def discard(self, connection: Connection) -> None:
        with contextlib.suppress(Exception):
            connection.close()
        with self._lock:
            self._created = max(0, self._created - 1)


class ReadOnlySession:
    def __init__(
        self,
        pool: ConnectionPool,
        *,
        timeout_ms: int | None = None,
    ):
        self.pool = pool
        self.timeout_ms = max(
            100,
            min(timeout_ms or pool.config.max_execution_time_ms, 10000),
        )
        self.connection: Connection | None = None
        self.cursor: DictCursor | None = None

    def __enter__(self) -> "ReadOnlySession":
        self.connection = self.pool.acquire()
        try:
            self.cursor = self.connection.cursor()
            self.set_query_timeout(self.timeout_ms)
            self.cursor.execute("START TRANSACTION READ ONLY")
            return self
        except Exception:
            self.pool.discard(self.connection)
            self.connection = None
            self.cursor = None
            raise

    def set_query_timeout(self, timeout_ms: int) -> None:
        """Tighten the per-SELECT limit while preserving the current transaction."""
        if self.cursor is None:
            raise DatabaseExecutionError("Read-only session is not active")
        self.timeout_ms = max(100, min(int(timeout_ms), 10000))
        self.cursor.execute(f"SET SESSION MAX_EXECUTION_TIME = {self.timeout_ms:d}")

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.cursor is not None:
            with contextlib.suppress(Exception):
                self.cursor.close()
        if self.connection is not None:
            self.pool.release(self.connection)
        self.cursor = None
        self.connection = None

    def query(
        self,
        sql: str,
        params: Mapping[str, Any] | Sequence[Any] | None = None,
        *,
        max_rows: int = 200,
    ) -> tuple[list[dict[str, Any]], bool]:
        if self.cursor is None:
            raise DatabaseExecutionError("Read-only session is not active")
        capped_rows = max(1, min(int(max_rows), 500))
        try:
            from .telemetry import stage
            with stage("database.read", count=capped_rows):
                self.cursor.execute(sql, params)
                rows = list(self.cursor.fetchmany(capped_rows + 1))
        except Exception as exc:
            raise DatabaseExecutionError(str(exc)) from exc
        truncated = len(rows) > capped_rows
        return rows[:capped_rows], truncated

    def scalar(
        self,
        sql: str,
        params: Mapping[str, Any] | Sequence[Any] | None = None,
    ) -> Any:
        rows, _ = self.query(sql, params, max_rows=1)
        if not rows:
            return None
        return next(iter(rows[0].values()))


class ReadOnlyDatabase:
    def __init__(self, config: DatabaseConfig | None = None):
        self.config = config or load_config()
        self.pool = ConnectionPool(self.config)

    @contextlib.contextmanager
    def session(self, *, timeout_ms: int | None = None) -> Iterator[ReadOnlySession]:
        session = ReadOnlySession(self.pool, timeout_ms=timeout_ms)
        with session:
            yield session

    def raw_query(
        self,
        *,
        reason: str,
        sql: str,
        params: Mapping[str, Any] | None = None,
        max_rows: int = 200,
        timeout_ms: int = 5000,
    ) -> dict[str, Any]:
        if not reason or not reason.strip():
            raise ValueError("A reason is required for readonly_sql")
        guard: GuardResult = validate_readonly_sql(sql, self.config.database)
        started = time.perf_counter()
        with self.session(timeout_ms=timeout_ms) as session:
            rows, truncated = session.query(sql, params or {}, max_rows=max_rows)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        columns = list(rows[0].keys()) if rows else []
        return {
            "reason": reason.strip(),
            "statement_type": guard.statement_type,
            "sql_hash": guard.sql_hash,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "elapsed_ms": elapsed_ms,
        }

    def status(self) -> dict[str, Any]:
        started = time.perf_counter()
        with self.session(timeout_ms=3000) as session:
            rows, _ = session.query(
                "SELECT VERSION() AS server_version",
                max_rows=1,
            )
        return {
            "configured": True,
            "config_path": str(default_config_path()),
            "server_version": rows[0].get("server_version") if rows else None,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        }
