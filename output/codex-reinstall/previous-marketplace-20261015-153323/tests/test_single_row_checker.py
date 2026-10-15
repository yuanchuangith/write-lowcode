from __future__ import annotations

import copy
import unittest
from unittest.mock import Mock

from tests.helpers import relation, transaction
from gxp_editor.repository import EditorRepository, RepositoryError
from gxp_editor.single_row_checker import SingleRowCheckError, check_single_row_statement, check_statement_list


def statement(sql, params):
    return {"sql": sql, "params": params, "design_id": "design-1", "expected_rows": 1}


class SingleRowCheckerTests(unittest.TestCase):
    def test_generated_multiple_changes_are_separate_single_row_statements(self):
        artifact = transaction(current_relations=[relation()], desired_relations={"new-node": "{}"})
        checks = check_statement_list(artifact["statements"])
        self.assertEqual(len(checks), 3)
        self.assertEqual([c["operation"] for c in checks], ["update", "delete", "insert"])
        self.assertTrue(all(c["expected_rows"] == 1 and c["safe"] for c in checks))

    def test_unsafe_shapes_are_blocked_before_execution(self):
        cases = [
            ("DELETE FROM cpm_bizflows_design_relation_data", [], "缺少 WHERE"),
            ("DELETE FROM cpm_bizflows_design_relation_data WHERE design_Id=%s", ["design-1"], "唯一 ID"),
            ("DELETE FROM cpm_bizflows_design_relation_data WHERE id IN (%s,%s) AND design_Id=%s", ["r1", "r2", "design-1"], "批量修改"),
            ("DELETE FROM cpm_bizflows_design_relation_data WHERE id=%s OR design_Id=%s", ["r1", "design-1"], "批量修改"),
            ("DELETE FROM cpm_bizflows_design_relation_data WHERE id>%s AND design_Id=%s", ["r1", "design-1"], "等值限制"),
            ("DELETE FROM cpm_bizflows_design_relation_data WHERE id=%s AND design_Id=%s; DELETE FROM cpm_bizflows_design_relation_data WHERE id=%s AND design_Id=%s", ["r1", "design-1", "r2", "design-1"], "多条 SQL"),
            ("INSERT INTO cpm_bizflows_design_relation_data (id,design_Id) VALUES (%s,%s),(%s,%s)", ["r1", "design-1", "r2", "design-1"], "批量插入"),
            ("INSERT INTO cpm_bizflows_design_relation_data (id,design_Id) SELECT id,design_Id FROM cpm_bizflows_design_relation_data", [], "子查询"),
            ("UPDATE cpm_bizflows_design_relation_data SET id=%s WHERE id=%s AND design_Id=%s", ["r2", "r1", "design-1"], "不允许修改记录唯一 ID"),
        ]
        for sql, params, reason in cases:
            with self.subTest(sql=sql), self.assertRaisesRegex(SingleRowCheckError, reason):
                check_single_row_statement(statement(sql, params))

    def test_empty_list_and_null_ids_and_non_integer_row_limits_are_blocked(self):
        sql = "DELETE FROM cpm_bizflows_design_relation_data WHERE id=%s AND design_Id=%s"
        for row_id in ("", " ", None, ["r1", "r2"], {"id": "r1"}):
            with self.subTest(row_id=row_id), self.assertRaises(SingleRowCheckError):
                check_single_row_statement(statement(sql, [row_id, "design-1"]))
        for rows in (2, 0, True, 1.0, "1"):
            unsafe = statement(sql, ["r1", "design-1"])
            unsafe["expected_rows"] = rows
            with self.subTest(rows=rows), self.assertRaises(SingleRowCheckError):
                check_single_row_statement(unsafe)

    def test_sql_in_bound_code_is_data_and_is_not_treated_as_multiple_statements(self):
        item = transaction()["statements"][0]
        item["params"][0] = "a; UPDATE dangerous SET value=1; WHERE id IN (1,2)"
        check_single_row_statement(item)

    def test_duplicate_target_is_blocked_even_with_case_difference(self):
        item = transaction()["statements"][0]
        duplicate = copy.deepcopy(item)
        duplicate["params"][4] = duplicate["design_id"] = "DESIGN-1"
        with self.assertRaisesRegex(SingleRowCheckError, "同一数据行"):
            check_statement_list([item, duplicate])


class SingleRowDatabaseChecks(unittest.TestCase):
    def repository_and_session(self, *, indexes=None, matches=None, owned=None):
        repository = EditorRepository(Mock())
        session = Mock()
        def query(sql, params=None, *, max_rows):
            if sql.startswith("SHOW INDEX"):
                return indexes if indexes is not None else [{"Key_name": "PRIMARY", "Column_name": "id", "Non_unique": 0, "Sub_part": None}]
            if "AND design_Id" in sql:
                return owned if owned is not None else [{"id": "relation-1"}]
            return matches if matches is not None else [{"id": "design-1"}]
        session.query.side_effect = query
        return repository, session

    def test_unique_index_and_exact_one_match_pass(self):
        repository, session = self.repository_and_session()
        repository.check_statement_targets(transaction()["statements"], session)
        session.execute.assert_not_called()

    def test_non_unique_composite_or_prefix_index_is_blocked(self):
        cases = [[], [{"Key_name": "idx", "Column_name": "id", "Non_unique": 1}],
                 [{"Key_name": "PRIMARY", "Column_name": "id", "Non_unique": 0}, {"Key_name": "PRIMARY", "Column_name": "tenant", "Non_unique": 0}],
                 [{"Key_name": "idx", "Column_name": "id", "Non_unique": 0, "Sub_part": 5}]]
        for rows in cases:
            repository, session = self.repository_and_session(indexes=rows)
            with self.subTest(rows=rows), self.assertRaisesRegex(RepositoryError, "单列唯一索引"):
                repository.check_statement_targets(transaction()["statements"], session)
            session.execute.assert_not_called()

    def test_zero_or_multiple_real_matches_are_blocked(self):
        for rows in ([], [{"id": "design-1"}, {"id": "design-1"}]):
            repository, session = self.repository_and_session(matches=rows)
            with self.subTest(rows=rows), self.assertRaisesRegex(RepositoryError, "实际匹配"):
                repository.check_statement_targets(transaction()["statements"], session)
            session.execute.assert_not_called()

    def test_relation_owner_and_existing_insert_id_are_blocked(self):
        repository, session = self.repository_and_session(owned=[])
        deletion = transaction(current_relations=[relation()])["statements"][1]
        with self.assertRaisesRegex(RepositoryError, "不属于"):
            repository.check_statement_targets([deletion], session)
        insertion = transaction(desired_relations={"new": "{}"})["statements"][1]
        with self.assertRaisesRegex(RepositoryError, "已存在"):
            repository.check_statement_targets([insertion], session)


if __name__ == "__main__":
    unittest.main()
