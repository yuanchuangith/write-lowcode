from __future__ import annotations

import json
import hmac
import re
from typing import Any

from .db import DatabaseSession, EditorDatabase
from .hashing import design_content_hash, json_default, sha256_text
from .single_row_checker import check_statement_list


class RepositoryError(RuntimeError):
    pass


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    return {key: json.loads(json.dumps(value, default=json_default)) for key, value in row.items()}


def _flag(value: Any) -> bool:
    if isinstance(value, bytes):
        return bool(int.from_bytes(value, "big"))
    return bool(value)


class EditorRepository:
    REQUIRED_INDEXES = {
        "cpm_bizflows_design": ["ref_Id", "is_publish", "isDeleted", "version"],
        "cpm_bizflows_design_relation_data": ["design_Id", "action_data_Id"],
    }

    def __init__(self, database: EditorDatabase):
        self.database = database

    def resolve_action(self, identifier: str, session: DatabaseSession) -> dict[str, Any]:
        identifier = (identifier or "").strip()
        if not identifier or len(identifier) > 128:
            raise ValueError("identifier must be 1-128 characters")
        rows: list[dict[str, Any]] = []
        sources = (
            (
                "public",
                "cpm_public_flows",
                "name",
                "NULL",
            ),
            (
                "form",
                "cpm_bizflows",
                "`describe`",
                "page_id",
            ),
        )
        for action_type, table, name_column, page_column in sources:
            for column in ("id", "code"):
                sql = (
                    f"SELECT %s AS action_type, id AS ref_id, code AS action_code, "
                    f"{name_column} AS action_name, {page_column} AS page_id "
                    f"FROM `{table}` WHERE isDeleted=0 AND `{column}`=%s LIMIT 2"
                )
                rows.extend(session.query(sql, (action_type, identifier), max_rows=2))
        unique = {(str(row["action_type"]), str(row["ref_id"])): row for row in rows}
        rows = list(unique.values())
        if not rows:
            raise RepositoryError(f"Action {identifier!r} was not found")
        if len(rows) != 1:
            raise RepositoryError(f"Action identifier {identifier!r} is ambiguous")
        return _clean(rows[0])

    def _draft_select(self) -> str:
        return """
SELECT id, ref_Id AS ref_id, code, name, `describe`, data, js_code, csharp_code,
       version, is_publish, isDeleted, creatorId, lastModifierId, department,
       creationTime, lastModificationTime AS last_modification_time, cpm_app_id
FROM cpm_bizflows_design
"""

    def load_draft_by_identifier(self, identifier: str) -> dict[str, Any]:
        with self.database.read_session() as session:
            metadata = self.resolve_action(identifier, session)
            rows = session.query(
                self._draft_select()
                + " WHERE ref_Id=%(ref_id)s AND is_publish=0 AND isDeleted=0"
                + " ORDER BY version DESC, lastModificationTime DESC LIMIT 2",
                {"ref_id": metadata["ref_id"]},
                max_rows=2,
            )
            if len(rows) != 1:
                raise RepositoryError(
                    f"Expected exactly one active draft for ref_Id={metadata['ref_id']}; found {len(rows)}"
                )
            draft = _clean(rows[0])
            relations = self._relations(str(draft["id"]), session)
        if _flag(draft.get("is_publish")) or _flag(draft.get("isDeleted")):
            raise RepositoryError("Resolved design is not an active unpublished draft")
        try:
            draft["data_json"] = json.loads(str(draft.get("data") or "{}"))
        except json.JSONDecodeError as exc:
            raise RepositoryError(f"Draft data JSON is invalid: {exc}") from exc
        draft["metadata"] = metadata
        draft["content_hash"] = design_content_hash(draft)
        draft["relations"] = relations
        return draft

    def load_draft_by_id(self, design_id: str, *, for_update: bool = False, session: DatabaseSession | None = None) -> dict[str, Any]:
        suffix = " FOR UPDATE" if for_update else ""
        sql = self._draft_select() + " WHERE id=%(design_id)s" + suffix

        def load(active: DatabaseSession) -> dict[str, Any]:
            row = active.one(sql, {"design_id": design_id})
            if not row:
                raise RepositoryError(f"Draft design {design_id!r} was not found")
            draft = _clean(row)
            draft["content_hash"] = design_content_hash(draft)
            return draft

        if session is not None:
            return load(session)
        with self.database.read_session() as active:
            return load(active)

    def load_relations(self, design_id: str) -> list[dict[str, Any]]:
        with self.database.read_session() as session:
            return self._relations(design_id, session)

    def _relations(self, design_id: str, session: DatabaseSession) -> list[dict[str, Any]]:
        rows = session.query(
            """
SELECT id, isDeleted, creatorId, creationTime, department, lastModifierId,
       lastModificationTime, org_id, org_name,
       action_data_Id AS action_data_id, design_Id AS design_id, data
FROM cpm_bizflows_design_relation_data
WHERE design_Id=%(design_id)s
ORDER BY action_data_Id, id
LIMIT 1001
""",
            {"design_id": design_id},
            max_rows=1000,
        )
        return [_clean(row) for row in rows]

    def index_status(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        with self.database.read_session() as session:
            for table, required in self.REQUIRED_INDEXES.items():
                rows = session.query(f"SHOW INDEX FROM `{table}`", max_rows=500)
                grouped: dict[str, list[tuple[int, str]]] = {}
                for row in rows:
                    grouped.setdefault(str(row.get("Key_name")), []).append(
                        (int(row.get("Seq_in_index") or 0), str(row.get("Column_name") or ""))
                    )
                indexes = {
                    name: [column for _, column in sorted(columns)]
                    for name, columns in grouped.items()
                }
                matching = [name for name, columns in indexes.items() if columns[: len(required)] == required]
                unique_id = self._unique_id_indexes(rows)
                result[table] = {
                    "required_columns": required,
                    "matching_indexes": matching,
                    "ready": bool(matching) and bool(unique_id),
                    "unique_id_indexes": unique_id,
                    "indexes": indexes,
                }
        return result

    @staticmethod
    def _unique_id_indexes(rows: list[dict[str, Any]]) -> list[str]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row.get("Key_name") or ""), []).append(row)
        return [name for name, entries in grouped.items()
                if len(entries) == 1 and str(entries[0].get("Column_name") or "").lower() == "id"
                and entries[0].get("Non_unique") in (0, "0") and entries[0].get("Sub_part") is None]

    def check_statement_targets(self, statements: list[dict[str, Any]], session: DatabaseSession) -> None:
        """Check and lock every target before the first DML in the transaction."""
        checks = check_statement_list(statements)
        for table in sorted({check["table"] for check in checks}):
            indexes = session.query(f"SHOW INDEX FROM `{table}`", max_rows=500)
            if not self._unique_id_indexes(indexes):
                raise RepositoryError(f"已阻止修改：{table}.id 未确认有单列唯一索引，不能保证只修改一条数据。")
        for check in checks:
            table = check["table"]
            rows = session.query(f"SELECT id FROM `{table}` WHERE id=%s LIMIT 2 FOR UPDATE", [check["row_id"]], max_rows=2)
            if check["operation"] == "insert":
                if rows:
                    raise RepositoryError("已阻止修改：待新增的唯一 ID 已存在。")
            elif len(rows) != 1:
                raise RepositoryError(f"已阻止修改：唯一 ID 实际匹配 {len(rows)} 条数据，必须恰好匹配 1 条。")
            if table == "cpm_bizflows_design_relation_data" and check["operation"] != "insert":
                owned = session.query(f"SELECT id FROM `{table}` WHERE id=%s AND design_Id=%s LIMIT 2 FOR UPDATE",
                                      [check["row_id"], check["design_id"]], max_rows=2)
                if len(owned) != 1:
                    raise RepositoryError("已阻止修改：关联记录不属于本次审批的草稿。")

    def grant_status(
        self,
        *,
        account_mode: str = "dedicated",
        expected_grants_sha256: str = "",
    ) -> dict[str, Any]:
        if account_mode not in {"dedicated", "shared_restricted", "shared_unrestricted"}:
            raise RepositoryError(f"Unsupported database account mode: {account_mode}")
        with self.database.read_session() as session:
            rows = session.query("SHOW GRANTS", max_rows=50)
        grants = [str(next(iter(row.values()))) for row in rows if row]
        canonical_grants = sorted(
            re.sub(r"\s+", " ", grant.strip().rstrip(";")) for grant in grants
        )
        grants_sha256 = sha256_text("\n".join(canonical_grants))
        expected_fingerprint = expected_grants_sha256.strip().lower()
        fingerprint_configured = bool(re.fullmatch(r"[0-9a-f]{64}", expected_fingerprint))
        fingerprint_matches = fingerprint_configured and hmac.compare_digest(
            grants_sha256,
            expected_fingerprint,
        )
        upper = "\n".join(grants).upper().replace("`", "")
        forbidden_tokens = (
            "ALL PRIVILEGES",
            " SUPER",
            " ALTER",
            " DROP",
            " CREATE",
            " GRANT OPTION",
            " FILE",
            " PROCESS",
            " RELOAD",
            " SHUTDOWN",
            " EXECUTE",
            " TRIGGER",
            " EVENT",
            " LOCK TABLES",
            " REFERENCES",
            " REPLICATION",
            " SYSTEM_USER",
            " SET_USER_ID",
        )
        forbidden = [token.strip() for token in forbidden_tokens if token in upper]
        broad_write = False
        write_targets: list[str] = []
        unsafe_write_targets: list[str] = []
        design_update_columns: list[str] = []
        relation_write_verbs: set[str] = set()
        global_non_usage_grants = 0
        for grant in grants:
            normalized = grant.replace("`", "")
            match = re.search(r"GRANT\s+(.+?)\s+ON\s+([^\s]+)\s+TO\s+", normalized, re.I)
            if not match:
                continue
            privileges, target = match.group(1), match.group(2).lower()
            normalized_privileges = re.sub(r"\s+", " ", privileges.strip()).upper()
            if target == "*.*" and normalized_privileges != "USAGE":
                global_non_usage_grants += 1
            if not re.search(r"\b(UPDATE|INSERT|DELETE)\b", privileges, re.I):
                continue
            table = target.split(".")[-1]
            if target == "*.*":
                target_scope = "global"
            elif table == "*":
                target_scope = "database_all_tables"
            elif table == "cpm_bizflows_design":
                target_scope = "design_table"
            elif table == "cpm_bizflows_design_relation_data":
                target_scope = "relation_table"
            else:
                target_scope = "other_table"
            write_targets.append(target_scope)
            if target_scope in {"global", "database_all_tables"}:
                broad_write = True
            if table not in {"cpm_bizflows_design", "cpm_bizflows_design_relation_data"}:
                unsafe_write_targets.append(target_scope)
            if table == "cpm_bizflows_design":
                if re.search(r"\b(INSERT|DELETE)\b", privileges, re.I):
                    unsafe_write_targets.append("design_table:forbidden-write-verb")
                column_match = re.search(r"UPDATE\s*\(([^)]+)\)", privileges, re.I)
                if not column_match:
                    unsafe_write_targets.append("design_table:table-wide-update")
                else:
                    design_update_columns.extend(
                        column.strip().strip("`").lower() for column in column_match.group(1).split(",")
                    )
            if table == "cpm_bizflows_design_relation_data":
                relation_write_verbs.update(
                    verb for verb in ("INSERT", "UPDATE", "DELETE") if re.search(rf"\b{verb}\b", privileges, re.I)
                )
        allowed_design_columns = {"data", "js_code", "csharp_code", "lastmodificationtime"}
        extra_design_columns = sorted(set(design_update_columns) - allowed_design_columns)
        missing_design_columns = sorted(allowed_design_columns - set(design_update_columns))
        missing_relation_verbs = sorted({"INSERT", "UPDATE", "DELETE"} - relation_write_verbs)
        dedicated_safe = (
            not forbidden
            and not broad_write
            and not unsafe_write_targets
            and not extra_design_columns
            and not missing_design_columns
            and not missing_relation_verbs
        )
        shared_forbidden_tokens = (
            " SUPER",
            " GRANT OPTION",
            " FILE",
            " PROCESS",
            " RELOAD",
            " SHUTDOWN",
            " EXECUTE",
            " REPLICATION",
            " SYSTEM_USER",
            " SET_USER_ID",
        )
        shared_forbidden = [
            token.strip() for token in shared_forbidden_tokens if token in upper
        ]
        shared_policy_eligible = not shared_forbidden and global_non_usage_grants == 0
        shared_restricted_safe = (
            shared_policy_eligible and fingerprint_matches
        )
        shared_unrestricted_safe = fingerprint_matches
        if account_mode == "dedicated":
            safe = dedicated_safe
        elif account_mode == "shared_restricted":
            safe = shared_restricted_safe
        else:
            safe = shared_unrestricted_safe
        return {
            "safe": safe,
            "account_mode": account_mode,
            "dedicated_safe": dedicated_safe,
            "shared_policy_eligible": shared_policy_eligible,
            "shared_restricted_safe": shared_restricted_safe,
            "shared_unrestricted_safe": shared_unrestricted_safe,
            "privileged_shared_override": account_mode == "shared_unrestricted",
            "grants_sha256": grants_sha256,
            "fingerprint_configured": fingerprint_configured,
            "fingerprint_matches": fingerprint_matches,
            "shared_forbidden_matches": shared_forbidden,
            "global_non_usage_grant_count": global_non_usage_grants,
            "shared_account_warning": account_mode != "dedicated",
            "forbidden_matches": forbidden,
            "broad_write": broad_write,
            "write_targets": sorted(set(write_targets)),
            "unsafe_write_targets": sorted(set(unsafe_write_targets)),
            "design_update_columns": sorted(set(design_update_columns)),
            "extra_design_update_columns": extra_design_columns,
            "missing_design_update_columns": missing_design_columns,
            "relation_write_verbs": sorted(relation_write_verbs),
            "missing_relation_write_verbs": missing_relation_verbs,
            "grant_statement_count": len(grants),
        }
