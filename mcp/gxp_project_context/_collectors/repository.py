from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import date, datetime
from typing import Any

from .db import ReadOnlyDatabase, ReadOnlySession
from .sql_guard import validate_identifier


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, bytes):
        if value in (b"\x00", b"\x01"):
            return int.from_bytes(value, byteorder="big")
        return value.decode("utf-8", errors="replace")
    return value


def _row_json(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _json_value(value) for key, value in row.items()}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _chinese_bigrams(value: str) -> set[str]:
    text = "".join(re.findall(r"[\u4e00-\u9fff]", value or ""))
    if len(text) < 2:
        return {text} if text else set()
    return {text[index : index + 2] for index in range(len(text) - 1)}


PAGE_OPERATION_TERMS = ("申请", "修订", "变更", "废除", "审批", "新建", "编辑", "详情", "列表")


def _page_similarity(query: str, display_name: str) -> float:
    query_terms = _chinese_bigrams(query)
    page_terms = _chinese_bigrams(
        re.sub(r"(?:流程|页面)$", "", display_name or "")
    )
    if not query_terms or not page_terms:
        return 0.0
    score = (2.0 * len(query_terms & page_terms)) / (
        len(query_terms) + len(page_terms)
    )
    query_operations = {term for term in PAGE_OPERATION_TERMS if term in query}
    page_operations = {term for term in PAGE_OPERATION_TERMS if term in display_name}
    if query_operations:
        overlap = query_operations & page_operations
        score += 0.35 * (len(overlap) / len(query_operations)) if overlap else -0.3
    if display_name.endswith("流程"):
        score += 0.05
    return max(0.0, min(score, 1.0))


class RepositoryError(RuntimeError):
    pass


class GxpRepository:
    """Read-only access to action metadata, design history and business rows."""

    def __init__(self, database: ReadOnlyDatabase):
        self.database = database
        self._column_cache: dict[str, dict[str, str]] = {}
        self._action_cache: dict[str, list[dict[str, Any]]] = {}
        # One repository is created per MCP request. This is an identity index,
        # not a cross-request snapshot or an alias-resolution shortcut.
        self._metadata_cache: dict[str, dict[str, Any]] = {}

    def _remember_metadata(self, results: list[dict[str, Any]]) -> None:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in results:
            if item.get("ref_id"):
                grouped.setdefault(str(item["ref_id"]), []).append(item)
        for ref_id, matches in grouped.items():
            if len(matches) == 1:
                self._metadata_cache[ref_id] = deepcopy(matches[0])
                self._metadata_cache[ref_id].pop("matched_by", None)
            else:
                self._metadata_cache.pop(ref_id, None)

    def _columns(self, table: str, session: ReadOnlySession | None = None) -> dict[str, str]:
        table = validate_identifier(table, "table")
        if table in self._column_cache:
            return self._column_cache[table]

        def load(active: ReadOnlySession) -> dict[str, str]:
            rows, _ = active.query(f"DESCRIBE `{table}`", max_rows=500)
            result = {
                str(row.get("Field", "")).lower(): str(row.get("Field", ""))
                for row in rows
                if row.get("Field")
            }
            if not result:
                raise RepositoryError(f"Table {table!r} was not found or has no columns")
            self._column_cache[table] = result
            return result

        if session is not None:
            return load(session)
        with self.database.session() as active:
            return load(active)

    @staticmethod
    def _pick(columns: dict[str, str], *names: str, required: bool = False) -> str | None:
        for name in names:
            if name.lower() in columns:
                return columns[name.lower()]
        if required:
            raise RepositoryError(f"Required column is missing: {'/'.join(names)}")
        return None

    def resolve_action(self, identifier: str) -> list[dict[str, Any]]:
        identifier = (identifier or "").strip()
        if not identifier or len(identifier) > 128:
            raise ValueError("Action identifier must be 1-128 characters")
        if identifier in self._action_cache:
            return self._action_cache[identifier]
        results = self.resolve_actions([identifier])
        matches = [
            item
            for item in results
            if identifier in {str(item.get("ref_id", "")), str(item.get("action_code", ""))}
        ]
        self._action_cache[identifier] = matches
        return matches

    def search_actions(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        query = (query or "").strip()
        if not query or len(query) > 128:
            raise ValueError("Action search query must be 1-128 characters")
        safe_limit = max(1, min(int(limit), 50))
        sql = f"""
SELECT 'public' AS action_type, id AS ref_id, code AS action_code,
       name AS action_name, NULL AS page_id,
       CASE WHEN id=%(query)s OR code=%(query)s OR name=%(query)s THEN 0 ELSE 1 END AS match_rank
FROM cpm_public_flows
WHERE isDeleted=0
  AND (id=%(query)s OR code=%(query)s OR name LIKE %(pattern)s)
UNION ALL
SELECT 'form' AS action_type, id AS ref_id, code AS action_code,
       `describe` AS action_name, page_id,
       CASE WHEN id=%(query)s OR code=%(query)s OR `describe`=%(query)s THEN 0 ELSE 1 END AS match_rank
FROM cpm_bizflows
WHERE isDeleted=0
  AND (id=%(query)s OR code=%(query)s OR `describe` LIKE %(pattern)s)
ORDER BY match_rank, action_type, action_name, ref_id
LIMIT {safe_limit}
"""
        with self.database.session() as session:
            rows, _ = session.query(
                sql, {"query": query, "pattern": f"%{query}%"}, max_rows=safe_limit
            )
            results = [_row_json(row) for row in rows]
            for result in results:
                result.pop("match_rank", None)
                result["matched_by"] = "action_catalog_name_or_identifier"
                if result.get("action_type") == "form":
                    result["form_name"] = self._load_form_name(
                        _text(result.get("page_id")), session
                    )
        self._remember_metadata(results)
        return results

    def resolve_actions(self, identifiers: list[str]) -> list[dict[str, Any]]:
        values = list(
            dict.fromkeys(
                value.strip()
                for value in identifiers
                if value and value.strip() and len(value.strip()) <= 128
            )
        )[:100]
        if not values:
            return []
        placeholders = ",".join(["%s"] * len(values))
        sql = f"""
SELECT 'public' AS action_type, id AS ref_id, code AS action_code,
       name AS action_name, NULL AS page_id
FROM cpm_public_flows
WHERE isDeleted=0 AND (id IN ({placeholders}) OR code IN ({placeholders}))
UNION ALL
SELECT 'form' AS action_type, id AS ref_id, code AS action_code,
       `describe` AS action_name, page_id
FROM cpm_bizflows
WHERE isDeleted=0 AND (id IN ({placeholders}) OR code IN ({placeholders}))
ORDER BY action_type, ref_id
"""
        with self.database.session() as session:
            params = [*values, *values, *values, *values]
            rows, _ = session.query(sql, params, max_rows=200)
            results = [_row_json(row) for row in rows]
            for result in results:
                if result.get("action_type") == "form":
                    result["form_name"] = self._load_form_name(
                        _text(result.get("page_id")), session
                    )
        for value in values:
            self._action_cache[value] = [
                item
                for item in results
                if value in {str(item.get("ref_id", "")), str(item.get("action_code", ""))}
            ]
        self._remember_metadata(results)
        return results

    def _load_form_name(self, page_id: str, session: ReadOnlySession) -> str:
        if not page_id:
            return ""
        sql = """
SELECT COALESCE(NULLIF(language.LangText,''), page.Name) AS form_name
FROM inbiz_page page
LEFT JOIN inbiz_language language
  ON language.`Key` = CASE
       WHEN page.Name LIKE %(multilingual_name_pattern)s
       THEN SUBSTRING_INDEX(page.Name,'global.',-1)
       ELSE page.Name
     END
 AND language.KindCode='zh-cn'
 AND language.InUse=1
 AND (language.AppId=page.AppId OR language.AppId IS NULL OR language.AppId='')
WHERE page.IsDeleted=0
  AND (page.Route=%(page_id)s OR page.OutId=%(page_id)s OR page.Id=%(page_id)s)
ORDER BY CASE WHEN page.Route=%(page_id)s THEN 0
              WHEN page.OutId=%(page_id)s THEN 1 ELSE 2 END
LIMIT 1
"""
        rows, _ = session.query(
            sql,
            {
                "page_id": page_id,
                "multilingual_name_pattern": "{multilingual}global.%",
            },
            max_rows=1,
        )
        return _text(rows[0].get("form_name")) if rows else ""

    @staticmethod
    def _page_catalog_select() -> str:
        return """
SELECT page.Id AS page_id, page.Route AS route, page.OutId AS out_id,
       page.AppId AS application_id, page.Name AS raw_name,
       COALESCE(NULLIF((
         SELECT language.LangText
         FROM inbiz_language language
         WHERE language.`Key` = CASE
                 WHEN page.Name LIKE %(multilingual_name_pattern)s
                 THEN SUBSTRING_INDEX(page.Name,'global.',-1)
                 ELSE page.Name
               END
           AND language.KindCode='zh-cn'
           AND language.InUse=1
           AND (language.AppId=page.AppId OR language.AppId IS NULL OR language.AppId='')
         ORDER BY CASE WHEN language.AppId=page.AppId THEN 0 ELSE 1 END
         LIMIT 1
       ), ''), page.Name) AS display_name
FROM inbiz_page page
WHERE page.IsDeleted=0
"""

    @staticmethod
    def _page_result(row: dict[str, Any], query: str) -> dict[str, Any]:
        item = _row_json(row)
        query_lower = query.lower()
        identifiers = {
            _text(item.get("page_id")).lower(),
            _text(item.get("route")).lower(),
            _text(item.get("out_id")).lower(),
        }
        display_name = _text(item.get("display_name"))
        if query_lower in identifiers:
            matched_by = "exact_identifier"
        elif query_lower == display_name.lower():
            matched_by = "exact_display_name"
        elif query_lower in display_name.lower() or query_lower in _text(
            item.get("raw_name")
        ).lower():
            matched_by = "display_name_contains"
        else:
            matched_by = "chinese_bigram_similarity"
        item["matched_by"] = matched_by
        item["similarity"] = round(_page_similarity(query, display_name), 4)
        item["application"] = {"id": item.get("application_id")}
        return item

    def resolve_pages(self, page_identifier: str) -> list[dict[str, Any]]:
        identifier = (page_identifier or "").strip()
        if not identifier or len(identifier) > 128:
            raise ValueError("Page identifier must be 1-128 characters")
        sql = f"""
SELECT * FROM ({self._page_catalog_select()}) page_catalog
WHERE page_catalog.route=%(page_identifier)s
   OR page_catalog.page_id=%(page_identifier)s
   OR page_catalog.out_id=%(page_identifier)s
ORDER BY CASE WHEN page_catalog.route=%(page_identifier)s THEN 0
              WHEN page_catalog.page_id=%(page_identifier)s THEN 1 ELSE 2 END
LIMIT 20
"""
        params = {
            "page_identifier": identifier,
            "multilingual_name_pattern": "{multilingual}global.%",
        }
        with self.database.session(timeout_ms=5000) as session:
            rows, _ = session.query(sql, params, max_rows=20)
        return [self._page_result(row, identifier) for row in rows]

    def search_pages(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        query = (query or "").strip()
        if not query or len(query) > 128:
            raise ValueError("Page search query must be 1-128 characters")
        safe_limit = max(1, min(int(limit), 50))
        catalog = self._page_catalog_select()
        params: dict[str, Any] = {
            "query": query,
            "pattern": f"%{query}%",
            "multilingual_name_pattern": "{multilingual}global.%",
        }
        exact_sql = f"""
SELECT * FROM ({catalog}) page_catalog
WHERE page_catalog.route=%(query)s
   OR page_catalog.page_id=%(query)s
   OR page_catalog.out_id=%(query)s
   OR page_catalog.display_name=%(query)s
   OR page_catalog.display_name LIKE %(pattern)s
   OR page_catalog.raw_name LIKE %(pattern)s
ORDER BY CASE
  WHEN page_catalog.route=%(query)s OR page_catalog.page_id=%(query)s
    OR page_catalog.out_id=%(query)s THEN 0
  WHEN page_catalog.display_name=%(query)s THEN 1
  ELSE 2 END, page_catalog.display_name, page_catalog.route
LIMIT {safe_limit}
"""
        with self.database.session(timeout_ms=5000) as session:
            rows, _ = session.query(exact_sql, params, max_rows=safe_limit)
            if rows:
                return [self._page_result(row, query) for row in rows]

            bigrams = sorted(_chinese_bigrams(query))[:12]
            if not bigrams:
                return []
            clauses = []
            for index, bigram in enumerate(bigrams):
                key = f"bigram_{index}"
                clauses.append(f"page_catalog.display_name LIKE %({key})s")
                params[key] = f"%{bigram}%"
            candidate_limit = min(200, max(20, safe_limit * 10))
            fuzzy_sql = f"""
SELECT * FROM ({catalog}) page_catalog
WHERE {' OR '.join(clauses)}
ORDER BY page_catalog.display_name, page_catalog.route
LIMIT {candidate_limit}
"""
            rows, _ = session.query(
                fuzzy_sql, params, max_rows=candidate_limit
            )
        candidates = [self._page_result(row, query) for row in rows]
        candidates = [item for item in candidates if item["similarity"] > 0]
        candidates.sort(
            key=lambda item: (
                -float(item.get("similarity", 0)),
                _text(item.get("display_name")),
                _text(item.get("route")),
            )
        )
        return candidates[:safe_limit]

    def list_page_actions(self, page: dict[str, Any], *, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        identifiers = list(
            dict.fromkeys(
                _text(page.get(key))
                for key in ("route", "page_id", "out_id")
                if _text(page.get(key))
            )
        )
        if not identifiers:
            return []
        placeholders = ",".join(["%s"] * len(identifiers))
        sql = f"""
SELECT id AS ref_id, code AS action_code, `describe` AS action_name,
       page_id, app_id AS application_id, `order` AS action_order
FROM cpm_bizflows
WHERE isDeleted=0 AND page_id IN ({placeholders})
ORDER BY `order`, `describe`, code, id
LIMIT {safe_limit}
"""
        with self.database.session(timeout_ms=5000) as session:
            rows, _ = session.query(sql, identifiers, max_rows=safe_limit)
        results = []
        for row in rows:
            item = _row_json(row)
            item["action_type"] = "form"
            results.append(item)
        return results

    def action_metadata(self, ref_id: str) -> dict[str, Any]:
        if ref_id in self._metadata_cache:
            return deepcopy(self._metadata_cache[ref_id])
        matches = self.resolve_action(ref_id)
        if not matches:
            return {
                "action_type": "unknown",
                "ref_id": ref_id,
                "action_code": "",
                "action_name": "",
                "page_id": "",
                "form_name": "",
            }
        return deepcopy(matches[0])

    def _design_select(
        self, *, include_content: bool, include_hashes: bool = True, include_csharp: bool = True
    ) -> tuple[str, dict[str, str]]:
        columns = self._columns("cpm_bizflows_design")
        aliases: dict[str, str] = {}
        candidates = {
            "design_id": ("id",),
            "ref_id": ("ref_Id", "ref_id"),
            "is_publish": ("is_publish", "isPublish"),
            "is_deleted": ("isDeleted", "is_deleted"),
            "modified_time": ("lastModificationTime", "modified_time", "updateTime"),
            "created_time": ("creationTime", "createTime", "created_time"),
            "data": ("data",),
            "csharp_code": ("csharp_code", "csharpCode"),
        }
        required = {"ref_id", "is_publish", "is_deleted", "data"}
        parts: list[str] = []
        for alias, names in candidates.items():
            if alias in ("data", "csharp_code") and not include_content:
                continue
            if alias == "csharp_code" and not include_csharp:
                continue
            column = self._pick(columns, *names, required=alias in required)
            if column:
                aliases[alias] = column
                parts.append(f"`{column}` AS `{alias}`")
            elif alias == "csharp_code" and include_content:
                parts.append("'' AS `csharp_code`")
        if include_content and not include_csharp:
            csharp_column = self._pick(columns, "csharp_code", "csharpCode")
            expression = f"IFNULL(`{csharp_column}`,'')" if csharp_column else "''"
            # Match Python's SHA256(text.encode('utf-8')) even if the database
            # column uses another text character set. No C# content is returned.
            parts.append(f"SHA2(CONVERT({expression} USING utf8mb4),256) AS `csharp_sha256`")
        if not include_content:
            data_column = self._pick(columns, "data", required=True)
            csharp_column = self._pick(columns, "csharp_code", "csharpCode")
            parts.append(f"LENGTH(`{data_column}`) AS `data_length`")
            if csharp_column:
                parts.append(
                    f"LENGTH(IFNULL(`{csharp_column}`,'')) AS `csharp_length`"
                )
            else:
                parts.append("0 AS `csharp_length`")
            if include_hashes:
                parts.append(f"SHA2(CONVERT(IFNULL(`{data_column}`,'') USING utf8mb4),256) AS `data_sha256`")
                if csharp_column:
                    parts.append(
                        f"SHA2(CONVERT(IFNULL(`{csharp_column}`, '') USING utf8mb4),256) AS `csharp_sha256`"
                    )
                else:
                    parts.append("SHA2('',256) AS `csharp_sha256`")
        return ", ".join(parts), aliases

    def get_design_versions(
        self,
        ref_id: str,
        *,
        include_deleted: bool = True,
        include_content: bool = False,
        include_hashes: bool = True,
    ) -> list[dict[str, Any]]:
        select_list, aliases = self._design_select(
            include_content=include_content, include_hashes=include_hashes
        )
        ref_column = aliases.get("ref_id") or self._pick(
            self._columns("cpm_bizflows_design"), "ref_Id", "ref_id", required=True
        )
        deleted_column = aliases.get("is_deleted") or self._pick(
            self._columns("cpm_bizflows_design"), "isDeleted", "is_deleted", required=True
        )
        modified_column = aliases.get("modified_time")
        created_column = aliases.get("created_time")
        order_parts = [f"`{modified_column}` DESC"] if modified_column else []
        if created_column:
            order_parts.append(f"`{created_column}` DESC")
        order_parts.append(f"`{deleted_column}` ASC")
        if aliases.get("design_id"):
            order_parts.append(f"`{aliases['design_id']}` ASC")
        where = f"`{ref_column}`=%(ref_id)s"
        if not include_deleted:
            where += f" AND `{deleted_column}`=0"
        sql = (
            f"SELECT {select_list} FROM `cpm_bizflows_design` "
            f"WHERE {where} ORDER BY {', '.join(order_parts)}"
        )
        with self.database.session(timeout_ms=5000) as session:
            rows, _ = session.query(sql, {"ref_id": ref_id}, max_rows=100)
        results: list[dict[str, Any]] = []
        for row in rows:
            results.append(self._design_row(row, include_content=include_content))
        return results

    @staticmethod
    def _design_row(row: dict[str, Any], *, include_content: bool, include_csharp: bool = True) -> dict[str, Any]:
        item = _row_json(row)
        item["version"] = "published" if bool(item.get("is_publish")) else "draft"
        if include_content:
            item["data"] = _text(row.get("data"))
            item["data_sha256"] = hashlib.sha256(item["data"].encode()).hexdigest()
            if include_csharp:
                item["csharp_code"] = _text(row.get("csharp_code"))
                item["csharp_sha256"] = hashlib.sha256(item["csharp_code"].encode()).hexdigest()
            elif not re.fullmatch(r"[0-9a-f]{64}", str(item.get("csharp_sha256", ""))):
                raise RepositoryError("Design C# content fingerprint was not returned by the database")
        return item

    def _load_design_content(
        self,
        ref_id: str,
        selected: dict[str, Any],
        *,
        include_deleted: bool,
        include_csharp: bool = True,
    ) -> dict[str, Any]:
        """Load content for one already-selected design row.

        Version selection is performed from the small metadata query above. The
        content query is then keyed by the design id so a large action history
        does not require transferring every design JSON/C# payload.
        """
        select_list, aliases = self._design_select(include_content=True, include_csharp=include_csharp)
        ref_column = aliases["ref_id"]
        id_column = aliases.get("design_id")
        deleted_column = aliases.get("is_deleted")
        if not id_column:
            raise RepositoryError(
                "cpm_bizflows_design.id is required to load one design payload"
            )

        where = f"`{ref_column}`=%(ref_id)s AND `{id_column}`=%(design_id)s"
        params: dict[str, Any] = {
            "ref_id": ref_id,
            "design_id": selected.get("design_id"),
        }
        if not include_deleted and deleted_column:
            where += f" AND `{deleted_column}`=0"
        sql = f"SELECT {select_list} FROM `cpm_bizflows_design` WHERE {where} LIMIT 1"
        with self.database.session(timeout_ms=10000) as session:
            rows, _ = session.query(sql, params, max_rows=1)
        if not rows:
            raise RepositoryError(
                f"Design payload not found for ref_Id={ref_id}, design_id={selected.get('design_id')}"
            )
        return self._design_row(rows[0], include_content=True, include_csharp=include_csharp)

    def load_design(
        self,
        ref_id: str,
        *,
        version: str = "published",
        design_id: str | None = None,
        at_time: str | None = None,
        include_deleted: bool = True,
        include_csharp: bool = True,
    ) -> dict[str, Any]:
        if version not in {"published", "draft"}:
            raise ValueError("version must be published or draft")
        # Select the target version from metadata only. Fetch the large JSON/C#
        # payload after the target row is known, using its primary key.
        versions = self.get_design_versions(
            ref_id,
            include_deleted=include_deleted,
            include_content=False,
            include_hashes=False,
        )
        if design_id:
            versions = [item for item in versions if _text(item.get("design_id")) == design_id]
        elif at_time:
            cutoff = at_time.replace("T", " ")

            def effective_time(item: dict[str, Any]) -> str:
                if item.get("version") == "published":
                    return _text(item.get("created_time") or item.get("modified_time"))
                return _text(item.get("modified_time") or item.get("created_time"))

            versions = [
                item
                for item in versions
                if item.get("version") == version
                and (not effective_time(item) or effective_time(item) <= cutoff)
            ]
            versions.sort(key=effective_time, reverse=True)
        else:
            versions = [item for item in versions if item.get("version") == version]
            active = [item for item in versions if not bool(item.get("is_deleted"))]
            versions = active or versions
        if not versions:
            detail = f" at {at_time}" if at_time else ""
            raise RepositoryError(f"No {version} design found for ref_Id={ref_id}{detail}")
        item = versions[0]
        if "data" not in item:
            item = self._load_design_content(
                ref_id,
                item,
                include_deleted=include_deleted,
                include_csharp=include_csharp,
            )
        try:
            item["data_json"] = json.loads(item.get("data") or "{}")
        except json.JSONDecodeError as exc:
            raise RepositoryError(f"Design JSON is invalid for ref_Id={ref_id}: {exc}") from exc
        item["metadata"] = self.action_metadata(ref_id)
        return item

    def find_designs_by_generated_class(self, class_name: str) -> list[dict[str, Any]]:
        class_name = (class_name or "").strip()
        if not class_name or len(class_name) > 128:
            raise ValueError("Generated class name must be 1-128 characters")
        columns = self._columns("cpm_bizflows_design")
        ref_column = self._pick(columns, "ref_Id", "ref_id", required=True)
        csharp_column = self._pick(columns, "csharp_code", "csharpCode", required=True)
        deleted_column = self._pick(columns, "isDeleted", "is_deleted", required=True)
        publish_column = self._pick(columns, "is_publish", "isPublish", required=True)
        modified_column = self._pick(columns, "lastModificationTime", "modified_time")
        id_column = self._pick(columns, "id")
        select_id = f"`{id_column}` AS design_id," if id_column else ""
        select_modified = (
            f"`{modified_column}` AS modified_time," if modified_column else ""
        )
        sql = f"""
SELECT {select_id} `{ref_column}` AS ref_id, `{publish_column}` AS is_publish,
       `{deleted_column}` AS is_deleted, {select_modified}
       LENGTH(IFNULL(`{csharp_column}`,'')) AS csharp_length
FROM `cpm_bizflows_design`
WHERE `{csharp_column}` LIKE %(needle)s
ORDER BY `{deleted_column}` ASC, `{publish_column}` DESC{f', `{id_column}` ASC' if id_column else ''}
LIMIT 100
"""
        with self.database.session(timeout_ms=8000) as session:
            rows, _ = session.query(
                sql, {"needle": f"%{class_name}%"}, max_rows=100
            )
        results = []
        for row in rows:
            item = _row_json(row)
            item["version"] = "published" if bool(item.get("is_publish")) else "draft"
            item["metadata"] = self.action_metadata(_text(item.get("ref_id")))
            results.append(item)
        return results

    def search_design_text(self, text: str, *, max_rows: int = 50) -> list[dict[str, Any]]:
        text = (text or "").strip()
        if not text or len(text) > 500:
            raise ValueError("Search text must be 1-500 characters")
        columns = self._columns("cpm_bizflows_design")
        ref_column = self._pick(columns, "ref_Id", "ref_id", required=True)
        csharp_column = self._pick(columns, "csharp_code", "csharpCode", required=True)
        deleted_column = self._pick(columns, "isDeleted", "is_deleted", required=True)
        publish_column = self._pick(columns, "is_publish", "isPublish", required=True)
        modified_column = self._pick(columns, "lastModificationTime", "modified_time")
        modified_select = (
            f", `{modified_column}` AS modified_time" if modified_column else ""
        )
        sql = f"""
SELECT `{ref_column}` AS ref_id, `{publish_column}` AS is_publish,
       `{deleted_column}` AS is_deleted{modified_select}
FROM `cpm_bizflows_design`
WHERE `{csharp_column}` LIKE %(needle)s
ORDER BY `{deleted_column}` ASC, `{publish_column}` DESC
LIMIT {max(1, min(max_rows, 100))}
"""
        with self.database.session(timeout_ms=8000) as session:
            rows, _ = session.query(sql, {"needle": f"%{text}%"}, max_rows=max_rows)
        return [_row_json(row) for row in rows]

    def describe_table(self, table: str) -> dict[str, Any]:
        table = validate_identifier(table, "table")
        with self.database.session() as session:
            columns, columns_truncated = session.query(
                f"DESCRIBE `{table}`", max_rows=500
            )
            indexes, indexes_truncated = session.query(
                f"SHOW INDEX FROM `{table}`", max_rows=500
            )
        return {
            "table": table,
            "columns": [_row_json(row) for row in columns],
            "indexes": [_row_json(row) for row in indexes],
            "truncated": columns_truncated or indexes_truncated,
        }

    def get_records(
        self,
        table: str,
        *,
        filters: list[dict[str, Any]],
        columns: list[str] | None = None,
        order_by: str | None = None,
        descending: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        table = validate_identifier(table, "table")
        known = self._columns(table)
        if columns:
            selected = []
            for column in columns:
                valid = validate_identifier(column, "column")
                if valid.lower() not in known:
                    raise ValueError(f"Unknown column {column!r} in {table}")
                selected.append(f"`{known[valid.lower()]}`")
            select_list = ", ".join(selected)
        else:
            select_list = "*"

        operator_sql = {
            "eq": "=",
            "ne": "<>",
            "lt": "<",
            "lte": "<=",
            "gt": ">",
            "gte": ">=",
            "like": "LIKE",
        }
        clauses: list[str] = []
        params: list[Any] = []
        for index, item in enumerate(filters or []):
            if not isinstance(item, dict):
                raise ValueError(
                    f"Filter at index {index} must be an object with a non-empty 'field'"
                )
            raw_field = item.get("field")
            if not isinstance(raw_field, str) or not raw_field.strip():
                raise ValueError(
                    f"Filter at index {index} must include a non-empty 'field'"
                )
            column = validate_identifier(raw_field.strip(), "filter field")
            if column.lower() not in known:
                raise ValueError(f"Unknown filter column {column!r} in {table}")
            actual = known[column.lower()]
            operator = str(item.get("operator", "eq")).lower()
            if operator in {"is_null", "not_null"}:
                clauses.append(f"`{actual}` IS {'NOT ' if operator == 'not_null' else ''}NULL")
            elif operator in {"in", "not_in"}:
                values = item.get("value")
                if not isinstance(values, list) or not values:
                    raise ValueError(f"{operator} requires a non-empty value list")
                if len(values) > 100:
                    raise ValueError("Filter value list may contain at most 100 items")
                placeholders = ",".join(["%s"] * len(values))
                clauses.append(
                    f"`{actual}` {'NOT IN' if operator == 'not_in' else 'IN'} ({placeholders})"
                )
                params.extend(values)
            elif operator in operator_sql:
                clauses.append(f"`{actual}` {operator_sql[operator]} %s")
                params.append(item.get("value"))
            else:
                raise ValueError(f"Unsupported filter operator: {operator}")

        if not clauses:
            raise ValueError("get_records requires at least one narrow filter")
        order_clause = ""
        if order_by:
            order = validate_identifier(order_by, "order column")
            if order.lower() not in known:
                raise ValueError(f"Unknown order column {order_by!r} in {table}")
            order_clause = f" ORDER BY `{known[order.lower()]}` {'DESC' if descending else 'ASC'}"
        safe_limit = max(1, min(int(limit), 500))
        sql = (
            f"SELECT {select_list} FROM `{table}` WHERE {' AND '.join(clauses)}"
            f"{order_clause} LIMIT {safe_limit + 1}"
        )
        with self.database.session(timeout_ms=5000) as session:
            rows, _ = session.query(sql, params, max_rows=safe_limit + 1)
        truncated = len(rows) > safe_limit
        return {
            "table": table,
            "rows": [_row_json(row) for row in rows[:safe_limit]],
            "row_count": min(len(rows), safe_limit),
            "truncated": truncated,
        }
