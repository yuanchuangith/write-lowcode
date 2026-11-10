from __future__ import annotations

import re
import time
from collections import defaultdict
from typing import Any, Iterable

from .db import DatabaseExecutionError, ReadOnlyDatabase
from .sql_guard import validate_identifier


GENERIC_COLUMNS = {"id", "code", "name", "type", "status", "state", "key", "value"}


def _is_generic_column(value: str) -> bool:
    return value.lower().strip("_") in GENERIC_COLUMNS


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _row(row: dict[str, Any]) -> dict[str, Any]:
    return {str(key).lower(): _json_value(value) for key, value in row.items()}


def _quote(identifier: str) -> str:
    return f"`{validate_identifier(identifier, 'identifier')}`"


def _type_family(column: dict[str, Any]) -> tuple[str, str | None, str | None]:
    data_type = str(column.get("data_type") or "").lower()
    if data_type in {"tinyint", "smallint", "mediumint", "int", "integer", "bigint", "decimal", "numeric"}:
        family = "number"
    elif data_type in {"char", "varchar", "text", "tinytext", "mediumtext", "longtext", "enum", "set"}:
        family = "string"
    elif data_type in {"binary", "varbinary", "blob", "tinyblob", "mediumblob", "longblob"}:
        family = "binary"
    elif data_type in {"date", "datetime", "timestamp", "time", "year"}:
        family = "temporal"
    else:
        family = data_type
    return family, column.get("character_set_name"), column.get("collation_name")


def columns_compatible(source: dict[str, Any], target: dict[str, Any]) -> bool:
    sf, sc, scol = _type_family(source)
    tf, tc, tcol = _type_family(target)
    if sf != tf:
        return False
    if sf == "string" and sc and tc and sc != tc:
        return False
    if sf == "string" and scol and tcol and scol != tcol:
        return False
    source_len = source.get("character_maximum_length")
    target_len = target.get("character_maximum_length")
    if source_len and target_len and int(source_len) > int(target_len):
        return False
    return True


def _words(value: str) -> set[str]:
    lowered = value.lower()
    words = {part for part in re.split(r"[^\w]+", lowered, flags=re.UNICODE) if part}
    chinese = "".join(char for char in lowered if "\u4e00" <= char <= "\u9fff")
    words.update(chinese[index:index + 2] for index in range(max(0, len(chinese) - 1)))
    return words


def _candidate_score(source_table: dict[str, Any], source_column: dict[str, Any], target_table: dict[str, Any], target_column: dict[str, Any]) -> int:
    source_name = str(source_column["column_name"]).lower()
    target_name = str(target_column["column_name"]).lower()
    table_name = str(target_table["table_name"]).lower()
    singular = table_name[:-1] if table_name.endswith("s") else table_name
    compact_source = re.sub(r"[^a-z0-9]", "", source_name)
    compact_expected = re.sub(r"[^a-z0-9]", "", f"{singular}_{target_name}")
    score = 0
    if source_name in {f"{table_name}_{target_name}", f"{singular}_{target_name}"}:
        score += 5
    elif compact_source == compact_expected:
        score += 5
    if target_name == "id" and (source_name.endswith(f"_{table_name}_id") or source_name.endswith(f"_{singular}_id")):
        score += 5
    if source_name == target_name and not _is_generic_column(source_name):
        score += 3
    if source_name.endswith(f"_{target_name}") and not _is_generic_column(target_name):
        score += 2
    source_comment = str(source_column.get("column_comment") or "").lower()
    target_comment = " ".join(
        str(value or "").lower()
        for value in (target_table.get("table_comment"), target_column.get("column_comment"))
    )
    if source_comment and target_comment and _words(source_comment) & _words(target_comment):
        score += 1
    return score


class SchemaMetadataRepository:
    def __init__(self, database: ReadOnlyDatabase, *, query_timeout_ms: int = 10000):
        self.database = database
        self.query_timeout_ms = max(100, min(int(query_timeout_ms), 10000))

    def _prepare_query(self, session: Any, deadline: float | None) -> None:
        timeout_ms = self.query_timeout_ms
        if deadline is not None:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms < 100:
                raise TimeoutError("schema refresh deadline exceeded")
            timeout_ms = min(timeout_ms, remaining_ms)
        setter = getattr(session, "set_query_timeout", None)
        if setter is not None:
            setter(timeout_ms)

    def load_table_metadata(self, table: str, *, deadline: float | None = None) -> dict[str, Any] | None:
        """Read exactly one table, sharing a deadline across all metadata queries."""
        validate_identifier(table, "table")
        with self.database.session(timeout_ms=self.query_timeout_ms) as session:
            self._prepare_query(session, deadline)
            rows, truncated = session.query(
                "SELECT TABLE_NAME, TABLE_TYPE, ENGINE, TABLE_COLLATION, TABLE_COMMENT "
                "FROM information_schema.TABLES WHERE TABLE_SCHEMA=%(schema)s AND TABLE_NAME=%(table)s",
                {"schema": self.database.config.database, "table": table}, max_rows=2,
            )
        if truncated or len(rows) > 1:
            raise ValueError("Ambiguous table metadata")
        if not rows:
            return None
        result = _row(rows[0])
        result["columns"] = self._columns(table, deadline=deadline)
        result["indexes"] = self._indexes(table, deadline=deadline)
        return result

    def load_schema(self, *, deadline: float | None = None) -> dict[str, Any]:
        database_name = self.database.config.database
        tables: list[dict[str, Any]] = []
        after = ""
        with self.database.session(timeout_ms=self.query_timeout_ms) as session:
            while True:
                self._prepare_query(session, deadline)
                rows, truncated = session.query(
                    """
SELECT TABLE_NAME, TABLE_TYPE, ENGINE, TABLE_COLLATION, TABLE_COMMENT
FROM information_schema.TABLES
WHERE TABLE_SCHEMA=%(schema)s AND TABLE_NAME > %(after)s
ORDER BY TABLE_NAME
LIMIT 501
""",
                    {"schema": database_name, "after": after},
                    max_rows=500,
                )
                normalized = [_row(item) for item in rows]
                tables.extend(normalized)
                if not normalized or not truncated:
                    break
                after = str(normalized[-1]["table_name"])

        try:
            columns = self._bulk_columns(deadline=deadline)
        except DatabaseExecutionError as error:
            cause = error.__cause__
            code = cause.args[0] if cause is not None and cause.args else None
            if code not in {2013, 3024}:
                raise
            # The server may time out when paging COLUMNS for the whole schema.
            # Exact table predicates use the metadata index and keep the scan bounded.
            columns = {str(table["table_name"]): self._columns(str(table["table_name"]), deadline=deadline)
                       for table in tables}
        try:
            indexes = self._bulk_indexes(deadline=deadline)
        except DatabaseExecutionError as error:
            cause = error.__cause__
            code = cause.args[0] if cause is not None and cause.args else None
            if code not in {2013, 3024}:
                raise
            # Some servers spend longer than the bounded query budget opening
            # the whole STATISTICS view. Exact table predicates avoid that scan.
            indexes = {str(table["table_name"]): self._indexes(str(table["table_name"]), deadline=deadline)
                       for table in tables}
        constraints = self._bulk_constraints(deadline=deadline)
        foreign_keys = self._bulk_foreign_keys(deadline=deadline)
        checks = self._bulk_checks(deadline=deadline)
        for table in tables:
            name = str(table["table_name"])
            table["columns"] = columns.get(name, [])
            table["indexes"] = indexes.get(name, [])
            table["constraints"] = constraints.get(name, [])
            table["foreign_keys"] = foreign_keys.get(name, [])
            table["checks"] = checks.get(name, [])
        return {"database": database_name, "tables": tables}

    def _bulk_columns(self, *, deadline: float | None = None) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = defaultdict(list)
        after_table = ""
        after_position = 0
        with self.database.session(timeout_ms=self.query_timeout_ms) as session:
            while True:
                self._prepare_query(session, deadline)
                rows, truncated = session.query(
                    """
SELECT TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, COLUMN_DEFAULT, IS_NULLABLE, DATA_TYPE,
       COLUMN_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE,
       CHARACTER_SET_NAME, COLLATION_NAME, COLUMN_KEY, EXTRA, COLUMN_COMMENT,
       GENERATION_EXPRESSION
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA=%(schema)s
  AND (TABLE_NAME > %(after_table)s OR
       (TABLE_NAME=%(after_table)s AND ORDINAL_POSITION > %(after_position)s))
ORDER BY TABLE_NAME, ORDINAL_POSITION
LIMIT 501
""",
                    {
                        "schema": self.database.config.database,
                        "after_table": after_table,
                        "after_position": after_position,
                    },
                    max_rows=500,
                )
                normalized = [_row(item) for item in rows]
                for item in normalized:
                    result[str(item.pop("table_name"))].append(item)
                if not normalized or not truncated:
                    break
                last = _row(rows[-1])
                after_table = str(last["table_name"])
                after_position = int(last["ordinal_position"])
        return dict(result)

    def _bulk_indexes(self, *, deadline: float | None = None) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = defaultdict(list)
        after_table = after_name = ""
        after_seq = 0
        with self.database.session(timeout_ms=self.query_timeout_ms) as session:
            while True:
                self._prepare_query(session, deadline)
                rows, truncated = session.query(
                    """
SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, COLLATION,
       SUB_PART, NULLABLE, INDEX_TYPE
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA=%(schema)s
  AND (TABLE_NAME > %(after_table)s OR
       (TABLE_NAME=%(after_table)s AND
        (INDEX_NAME > %(after_name)s OR
         (INDEX_NAME=%(after_name)s AND SEQ_IN_INDEX > %(after_seq)s))))
ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
LIMIT 501
""",
                    {
                        "schema": self.database.config.database,
                        "after_table": after_table,
                        "after_name": after_name,
                        "after_seq": after_seq,
                    },
                    max_rows=500,
                )
                normalized = [_row(item) for item in rows]
                for item in normalized:
                    result[str(item.pop("table_name"))].append(item)
                if not normalized or not truncated:
                    break
                last = _row(rows[-1])
                after_table = str(last["table_name"])
                after_name = str(last["index_name"])
                after_seq = int(last["seq_in_index"])
        return dict(result)

    def _bulk_constraints(self, *, deadline: float | None = None) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = defaultdict(list)
        after_table = after_name = ""
        with self.database.session(timeout_ms=self.query_timeout_ms) as session:
            while True:
                self._prepare_query(session, deadline)
                rows, truncated = session.query(
                    """
SELECT TABLE_NAME, CONSTRAINT_NAME, CONSTRAINT_TYPE
FROM information_schema.TABLE_CONSTRAINTS
WHERE TABLE_SCHEMA=%(schema)s
  AND (TABLE_NAME > %(after_table)s OR
       (TABLE_NAME=%(after_table)s AND CONSTRAINT_NAME > %(after_name)s))
ORDER BY TABLE_NAME, CONSTRAINT_NAME
LIMIT 501
""",
                    {
                        "schema": self.database.config.database,
                        "after_table": after_table,
                        "after_name": after_name,
                    },
                    max_rows=500,
                )
                normalized = [_row(item) for item in rows]
                for item in normalized:
                    result[str(item.pop("table_name"))].append(item)
                if not normalized or not truncated:
                    break
                last = _row(rows[-1])
                after_table = str(last["table_name"])
                after_name = str(last["constraint_name"])
        return dict(result)

    def _bulk_foreign_keys(self, *, deadline: float | None = None) -> dict[str, list[dict[str, Any]]]:
        rows_by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
        after_table = after_name = ""
        after_position = 0
        with self.database.session(timeout_ms=self.query_timeout_ms) as session:
            while True:
                self._prepare_query(session, deadline)
                rows, truncated = session.query(
                    """
SELECT k.TABLE_NAME, k.CONSTRAINT_NAME, k.COLUMN_NAME, k.ORDINAL_POSITION,
       k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME,
       r.UPDATE_RULE, r.DELETE_RULE
FROM information_schema.KEY_COLUMN_USAGE k
LEFT JOIN information_schema.REFERENTIAL_CONSTRAINTS r
  ON r.CONSTRAINT_SCHEMA=k.CONSTRAINT_SCHEMA AND r.CONSTRAINT_NAME=k.CONSTRAINT_NAME
WHERE k.TABLE_SCHEMA=%(schema)s AND k.REFERENCED_TABLE_NAME IS NOT NULL
  AND (k.TABLE_NAME > %(after_table)s OR
       (k.TABLE_NAME=%(after_table)s AND
        (k.CONSTRAINT_NAME > %(after_name)s OR
         (k.CONSTRAINT_NAME=%(after_name)s AND k.ORDINAL_POSITION > %(after_position)s))))
ORDER BY k.TABLE_NAME, k.CONSTRAINT_NAME, k.ORDINAL_POSITION
LIMIT 501
""",
                    {
                        "schema": self.database.config.database,
                        "after_table": after_table,
                        "after_name": after_name,
                        "after_position": after_position,
                    },
                    max_rows=500,
                )
                normalized = [_row(item) for item in rows]
                for item in normalized:
                    rows_by_table[str(item.pop("table_name"))].append(item)
                if not normalized or not truncated:
                    break
                last = _row(rows[-1])
                after_table = str(last["table_name"])
                after_name = str(last["constraint_name"])
                after_position = int(last["ordinal_position"])
        result: dict[str, list[dict[str, Any]]] = {}
        for table, table_rows in rows_by_table.items():
            grouped: dict[str, dict[str, Any]] = {}
            for item in table_rows:
                name = str(item["constraint_name"])
                relation = grouped.setdefault(name, {
                    "constraint_name": name,
                    "columns": [],
                    "referenced_table_name": item["referenced_table_name"],
                    "referenced_columns": [],
                    "update_rule": item.get("update_rule"),
                    "delete_rule": item.get("delete_rule"),
                })
                relation["columns"].append(item["column_name"])
                relation["referenced_columns"].append(item["referenced_column_name"])
            result[table] = list(grouped.values())
        return result

    def _bulk_checks(self, *, deadline: float | None = None) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = defaultdict(list)
        after_table = after_name = ""
        try:
            with self.database.session(timeout_ms=self.query_timeout_ms) as session:
                while True:
                    self._prepare_query(session, deadline)
                    rows, truncated = session.query(
                        """
SELECT tc.TABLE_NAME, tc.CONSTRAINT_NAME, cc.CHECK_CLAUSE
FROM information_schema.TABLE_CONSTRAINTS tc
JOIN information_schema.CHECK_CONSTRAINTS cc
  ON cc.CONSTRAINT_SCHEMA=tc.CONSTRAINT_SCHEMA AND cc.CONSTRAINT_NAME=tc.CONSTRAINT_NAME
WHERE tc.TABLE_SCHEMA=%(schema)s AND tc.CONSTRAINT_TYPE='CHECK'
  AND (tc.TABLE_NAME > %(after_table)s OR
       (tc.TABLE_NAME=%(after_table)s AND tc.CONSTRAINT_NAME > %(after_name)s))
ORDER BY tc.TABLE_NAME, tc.CONSTRAINT_NAME
LIMIT 501
""",
                        {
                            "schema": self.database.config.database,
                            "after_table": after_table,
                            "after_name": after_name,
                        },
                        max_rows=500,
                    )
                    normalized = [_row(item) for item in rows]
                    for item in normalized:
                        result[str(item.pop("table_name"))].append(item)
                    if not normalized or not truncated:
                        break
                    last = _row(rows[-1])
                    after_table = str(last["table_name"])
                    after_name = str(last["constraint_name"])
        except TimeoutError:
            raise
        except Exception:
            return {}
        return dict(result)

    def _columns(self, table: str, *, deadline: float | None = None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        after = 0
        with self.database.session(timeout_ms=self.query_timeout_ms) as session:
            while True:
                self._prepare_query(session, deadline)
                rows, truncated = session.query(
                    """
SELECT COLUMN_NAME, ORDINAL_POSITION, COLUMN_DEFAULT, IS_NULLABLE, DATA_TYPE,
       COLUMN_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE,
       CHARACTER_SET_NAME, COLLATION_NAME, COLUMN_KEY, EXTRA, COLUMN_COMMENT,
       GENERATION_EXPRESSION
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA=%(schema)s AND TABLE_NAME=%(table)s AND ORDINAL_POSITION > %(after)s
ORDER BY ORDINAL_POSITION
""",
                    {"schema": self.database.config.database, "table": table, "after": after},
                    max_rows=500,
                )
                normalized = [_row(item) for item in rows]
                result.extend(normalized)
                if not normalized or not truncated:
                    break
                after = int(normalized[-1]["ordinal_position"])
        return result

    def _indexes(self, table: str, *, deadline: float | None = None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        after_name = ""
        after_seq = 0
        with self.database.session(timeout_ms=self.query_timeout_ms) as session:
            while True:
                self._prepare_query(session, deadline)
                rows, truncated = session.query(
                    """
SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, COLLATION,
       SUB_PART, NULLABLE, INDEX_TYPE
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA=%(schema)s AND TABLE_NAME=%(table)s
  AND (INDEX_NAME > %(after_name)s OR (INDEX_NAME=%(after_name)s AND SEQ_IN_INDEX > %(after_seq)s))
ORDER BY INDEX_NAME, SEQ_IN_INDEX
""",
                    {"schema": self.database.config.database, "table": table, "after_name": after_name, "after_seq": after_seq},
                    max_rows=500,
                )
                normalized = [_row(item) for item in rows]
                result.extend(normalized)
                if not normalized or not truncated:
                    break
                after_name = str(normalized[-1]["index_name"])
                after_seq = int(normalized[-1]["seq_in_index"])
        return result

    def _constraints(self, table: str, *, deadline: float | None = None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        after = ""
        with self.database.session(timeout_ms=self.query_timeout_ms) as session:
            while True:
                self._prepare_query(session, deadline)
                rows, truncated = session.query(
                    """
SELECT CONSTRAINT_NAME, CONSTRAINT_TYPE
FROM information_schema.TABLE_CONSTRAINTS
WHERE TABLE_SCHEMA=%(schema)s AND TABLE_NAME=%(table)s AND CONSTRAINT_NAME > %(after)s
ORDER BY CONSTRAINT_NAME
""",
                    {"schema": self.database.config.database, "table": table, "after": after},
                    max_rows=500,
                )
                normalized = [_row(item) for item in rows]
                result.extend(normalized)
                if not normalized or not truncated:
                    break
                after = str(normalized[-1]["constraint_name"])
        return result

    def _foreign_keys(self, table: str, *, deadline: float | None = None) -> list[dict[str, Any]]:
        all_rows: list[dict[str, Any]] = []
        after_name = ""
        after_position = 0
        with self.database.session(timeout_ms=self.query_timeout_ms) as session:
            while True:
                self._prepare_query(session, deadline)
                rows, truncated = session.query(
                    """
SELECT k.CONSTRAINT_NAME, k.COLUMN_NAME, k.ORDINAL_POSITION,
       k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME,
       r.UPDATE_RULE, r.DELETE_RULE
FROM information_schema.KEY_COLUMN_USAGE k
LEFT JOIN information_schema.REFERENTIAL_CONSTRAINTS r
  ON r.CONSTRAINT_SCHEMA=k.CONSTRAINT_SCHEMA AND r.CONSTRAINT_NAME=k.CONSTRAINT_NAME
WHERE k.TABLE_SCHEMA=%(schema)s AND k.TABLE_NAME=%(table)s
  AND k.REFERENCED_TABLE_NAME IS NOT NULL
  AND (k.CONSTRAINT_NAME > %(after_name)s OR
       (k.CONSTRAINT_NAME=%(after_name)s AND k.ORDINAL_POSITION > %(after_position)s))
ORDER BY k.CONSTRAINT_NAME, k.ORDINAL_POSITION
""",
                    {
                        "schema": self.database.config.database, "table": table,
                        "after_name": after_name, "after_position": after_position,
                    },
                    max_rows=500,
                )
                normalized = [_row(item) for item in rows]
                all_rows.extend(normalized)
                if not normalized or not truncated:
                    break
                after_name = str(normalized[-1]["constraint_name"])
                after_position = int(normalized[-1]["ordinal_position"])
        grouped: dict[str, dict[str, Any]] = {}
        for item in all_rows:
            name = str(item["constraint_name"])
            relation = grouped.setdefault(name, {
                "constraint_name": name,
                "columns": [],
                "referenced_table_name": item["referenced_table_name"],
                "referenced_columns": [],
                "update_rule": item.get("update_rule"),
                "delete_rule": item.get("delete_rule"),
            })
            relation["columns"].append(item["column_name"])
            relation["referenced_columns"].append(item["referenced_column_name"])
        return list(grouped.values())

    def _checks(self, table: str, *, deadline: float | None = None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        after = ""
        try:
            with self.database.session(timeout_ms=self.query_timeout_ms) as session:
                while True:
                    self._prepare_query(session, deadline)
                    rows, truncated = session.query(
                        """
SELECT tc.CONSTRAINT_NAME, cc.CHECK_CLAUSE
FROM information_schema.TABLE_CONSTRAINTS tc
JOIN information_schema.CHECK_CONSTRAINTS cc
  ON cc.CONSTRAINT_SCHEMA=tc.CONSTRAINT_SCHEMA AND cc.CONSTRAINT_NAME=tc.CONSTRAINT_NAME
WHERE tc.TABLE_SCHEMA=%(schema)s AND tc.TABLE_NAME=%(table)s
  AND tc.CONSTRAINT_TYPE='CHECK'
  AND tc.CONSTRAINT_NAME > %(after)s
ORDER BY tc.CONSTRAINT_NAME
""",
                        {"schema": self.database.config.database, "table": table, "after": after},
                        max_rows=500,
                    )
                    normalized = [_row(item) for item in rows]
                    result.extend(normalized)
                    if not normalized or not truncated:
                        break
                    after = str(normalized[-1]["constraint_name"])
            return result
        except TimeoutError:
            raise
        except Exception:
            return []

    def validate_relation(
        self,
        source_table: str,
        source_columns: list[str],
        target_table: str,
        target_columns: list[str],
        *,
        min_distinct_values: int,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        if not source_columns or len(source_columns) != len(target_columns):
            raise ValueError("来源字段和目标字段必须是长度相同的非空数组")
        source = _quote(source_table)
        target = _quote(target_table)
        source_cols = [_quote(item) for item in source_columns]
        target_cols = [_quote(item) for item in target_columns]
        non_null = " AND ".join(f"s.{column} IS NOT NULL" for column in source_cols)
        group_by = ", ".join(f"s.{column}" for column in source_cols)
        join = " AND ".join(f"s.{left}=t.{right}" for left, right in zip(source_cols, target_cols))
        target_missing = f"t.{target_cols[0]} IS NULL"
        with self.database.session(timeout_ms=self.query_timeout_ms) as session:
            self._prepare_query(session, deadline)
            non_null_count = int(session.scalar(f"SELECT COUNT(*) AS n FROM {source} s WHERE {non_null}") or 0)
            self._prepare_query(session, deadline)
            distinct_count = int(session.scalar(
                f"SELECT COUNT(*) AS n FROM (SELECT 1 FROM {source} s WHERE {non_null} GROUP BY {group_by}) d"
            ) or 0)
            self._prepare_query(session, deadline)
            unmatched_count = int(session.scalar(
                f"SELECT COUNT(*) AS n FROM {source} s LEFT JOIN {target} t ON {join} "
                f"WHERE {non_null} AND {target_missing}"
            ) or 0)
        passed = distinct_count >= min_distinct_values and unmatched_count == 0
        return {
            "non_null_count": non_null_count,
            "distinct_count": distinct_count,
            "unmatched_count": unmatched_count,
            "source_unique": non_null_count == distinct_count and non_null_count > 0,
            "match_rate": 1.0 if non_null_count and unmatched_count == 0 else (
                round((non_null_count - unmatched_count) / non_null_count, 8) if non_null_count else 0.0
            ),
            "passed": passed,
            "reason": "verified" if passed else (
                "insufficient_distinct_values" if distinct_count < min_distinct_values else "unmatched_values"
            ),
        }


def unique_keys(table: dict[str, Any], *, strict: bool = False) -> list[list[str]]:
    grouped: dict[str, list[tuple[int, str]]] = defaultdict(list)
    invalid = set()
    for index in table.get("indexes", []):
        if strict and (not index.get("column_name") or index.get("sub_part") is not None):
            invalid.add(str(index["index_name"]))
        if int(index.get("non_unique") or 0) == 0 and index.get("column_name"):
            grouped[str(index["index_name"])].append((int(index.get("seq_in_index") or 0), str(index["column_name"])))
    return [[column for _, column in sorted(items)] for name, items in grouped.items()
            if not strict or name not in invalid and sorted(seq for seq, _ in items) == list(range(1, len(items) + 1))]


def generate_candidates(schema: dict[str, Any], *, max_targets_per_source: int = 3) -> list[dict[str, Any]]:
    tables = schema.get("tables", [])
    columns_by_table = {
        str(table["table_name"]): {str(column["column_name"]): column for column in table.get("columns", [])}
        for table in tables
    }
    single_targets: dict[tuple[str, tuple[str, ...]], tuple[dict[str, Any], list[str], dict[str, Any]]] = {}
    composed_index: dict[str, list[tuple[str, tuple[str, ...]]]] = defaultdict(list)
    column_suffix_index: dict[str, list[tuple[str, tuple[str, ...]]]] = defaultdict(list)
    id_alias_index: dict[str, list[tuple[str, tuple[str, ...]]]] = defaultdict(list)
    composite_targets: list[tuple[dict[str, Any], list[str], list[dict[str, Any]]]] = []
    for target_table in tables:
        target_name = str(target_table["table_name"])
        target_map = columns_by_table[target_name]
        target_name_lower = target_name.lower()
        singular = target_name_lower[:-1] if target_name_lower.endswith("s") else target_name_lower
        for target_key in unique_keys(target_table):
            target_columns = [target_map.get(column) for column in target_key]
            if any(column is None for column in target_columns):
                continue
            if len(target_key) != 1:
                composite_targets.append((target_table, target_key, target_columns))
                continue
            identity = (target_name, tuple(target_key))
            target_column = target_columns[0]
            single_targets[identity] = (target_table, target_key, target_column)
            column_name = str(target_column["column_name"]).lower()
            column_suffix_index[column_name].append(identity)
            for expected in (f"{target_name_lower}_{column_name}", f"{singular}_{column_name}"):
                composed_index[re.sub(r"[^a-z0-9]", "", expected)].append(identity)
            if column_name == "id":
                id_alias_index[target_name_lower].append(identity)
                id_alias_index[singular].append(identity)

    candidates: list[dict[str, Any]] = []
    for source_table in tables:
        source_name = str(source_table["table_name"])
        source_map = columns_by_table[source_name]
        for source_column in source_map.values():
            source_column_name = str(source_column["column_name"]).lower()
            compact_source = re.sub(r"[^a-z0-9]", "", source_column_name)
            identities = set(composed_index.get(compact_source, []))
            identities.update(column_suffix_index.get(source_column_name, []))
            name_parts = [part for part in source_column_name.split("_") if part]
            for index in range(len(name_parts)):
                suffix = "_".join(name_parts[index:])
                identities.update(column_suffix_index.get(suffix, []))
            if source_column_name.endswith("_id"):
                base_parts = [part for part in source_column_name[:-3].split("_") if part]
                for index in range(len(base_parts)):
                    identities.update(id_alias_index.get("_".join(base_parts[index:]), []))
            for identity in identities:
                target_table, target_key, target_column = single_targets[identity]
                target_name = str(target_table["table_name"])
                if source_name == target_name or not columns_compatible(source_column, target_column):
                    continue
                score = _candidate_score(source_table, source_column, target_table, target_column)
                if score >= 3:
                    candidates.append({
                        "source_table": source_name,
                        "source_columns": [source_column["column_name"]],
                        "target_table": target_name,
                        "target_columns": target_key,
                        "candidate_score": score,
                    })
        for target_table, target_key, target_columns in composite_targets:
            target_name = str(target_table["table_name"])
            if source_name == target_name:
                continue
            direct = [source_map.get(column) for column in target_key]
            if all(direct) and all(columns_compatible(left, right) for left, right in zip(direct, target_columns)):
                candidates.append({
                    "source_table": source_name,
                    "source_columns": target_key,
                    "target_table": target_name,
                    "target_columns": target_key,
                    "candidate_score": 3,
                })
    grouped: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        key = (candidate["source_table"], tuple(candidate["source_columns"]))
        grouped[key].append(candidate)
    selected: list[dict[str, Any]] = []
    for values in grouped.values():
        values.sort(key=lambda item: (-int(item["candidate_score"]), item["target_table"], item["target_columns"]))
        selected.extend({**item, "candidate_count": len(values), "candidate_truncated": len(values) > max_targets_per_source}
                        for item in values[:max_targets_per_source])
    return selected
