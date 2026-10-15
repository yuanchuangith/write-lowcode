"""Fail-closed single-statement/single-row checks, independent of SQL templates."""
from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlglot import exp


class SingleRowCheckError(ValueError):
    pass


def _block(reason: str) -> None:
    raise SingleRowCheckError("已阻止修改：" + reason)


def check_single_row_statement(statement: dict[str, Any]) -> dict[str, Any]:
    sql, params = statement.get("sql"), statement.get("params")
    if not isinstance(sql, str) or not isinstance(params, list) or sql.count("%s") != len(params):
        _block("SQL 与绑定参数不完整。")
    if type(statement.get("expected_rows")) is not int or statement["expected_rows"] != 1:
        _block("每条修改语句必须且只能影响 1 行。")
    index = iter(range(len(params)))
    source = re.sub(r"%s", lambda match: f":gxp{next(index)}", sql)
    try:
        parsed = sqlglot.parse(source, read="mysql")
    except sqlglot.errors.SqlglotError:
        _block("SQL 无法通过单条语句检查。")
    if len(parsed) != 1 or parsed[0] is None:
        _block("一项审批中含有多条 SQL，必须拆成独立单条审批。")
    tree = parsed[0]
    if not isinstance(tree, (exp.Update, exp.Delete, exp.Insert)):
        _block("仅允许受控的单行更新、删除或插入。")
    if any(tree.find_all(exp.Select, exp.Join)):
        _block("不允许子查询或 JOIN 批量修改。")
    tables = list(tree.find_all(exp.Table))
    if len(tables) != 1 or tables[0].db or tables[0].catalog or tables[0].alias:
        _block("不允许多表、跨库或带别名的修改。")
    table = tables[0].name.lower()
    if table not in {"cpm_bizflows_design", "cpm_bizflows_design_relation_data"}:
        _block("目标表不属于受控草稿或动作关联表。")

    def value(node: exp.Expression) -> Any:
        if isinstance(node, exp.Placeholder) and re.fullmatch(r"gxp\d+", node.name):
            parameter_index = int(node.name[3:])
            if parameter_index >= len(params):
                _block("限制条件引用了不存在的绑定参数。")
            return params[parameter_index]
        if isinstance(node, exp.Literal):
            return node.this
        _block("唯一限制必须绑定明确的单个 ID，不能使用表达式。")

    guards: dict[str, Any] = {}
    if isinstance(tree, exp.Insert):
        schema, rows = tree.this, tree.args.get("expression")
        if table != "cpm_bizflows_design_relation_data" or not isinstance(schema, exp.Schema):
            _block("仅允许插入一条有明确 ID 的动作关联记录。")
        if not isinstance(rows, exp.Values) or len(rows.expressions) != 1 or not isinstance(rows.expressions[0], exp.Tuple):
            _block("检测到批量插入；每条 INSERT 只能包含一条数据。")
        columns = [column.name.lower() for column in schema.expressions]
        values = rows.expressions[0].expressions
        if len(columns) != len(values) or len(columns) != len(set(columns)):
            _block("插入字段或参数数量不一致。")
        for key in ("id", "design_id"):
            if key not in columns:
                _block(f"插入缺少唯一 ID 或草稿归属字段：{key}。")
            guards[key] = value(values[columns.index(key)])
        if tree.args.get("conflict") or tree.args.get("ignore") or tree.args.get("alternative"):
            _block("不允许忽略冲突、替换或重复键更新。")
    else:
        where = tree.args.get("where")
        if where is None:
            _block("缺少 WHERE 唯一限制，可能修改整张表。")
        if any(where.find_all(exp.Or, exp.In, exp.Between, exp.Like)):
            _block("WHERE 含 OR、IN、范围或模糊条件，可能批量修改。")

        def collect(node: exp.Expression) -> None:
            if isinstance(node, exp.Paren):
                collect(node.this)
            elif isinstance(node, exp.And):
                collect(node.this)
                collect(node.expression)
            elif isinstance(node, exp.EQ) and isinstance(node.this, exp.Column) and not node.this.table:
                key = node.this.name.lower()
                if key in guards:
                    _block("WHERE 中有重复限制字段。")
                guards[key] = value(node.expression)
            else:
                _block("WHERE 必须以 AND 连接明确的等值限制。")
        collect(where.this)
        if isinstance(tree, exp.Update):
            for assignment in tree.expressions:
                if not isinstance(assignment, exp.EQ) or not isinstance(assignment.this, exp.Column):
                    _block("不允许无法确认的更新字段。")
                if assignment.this.name.lower() in {"id", "ref_id", "design_id", "is_publish"}:
                    _block("不允许修改记录唯一 ID、归属或发布标记。")
        if table == "cpm_bizflows_design":
            if not isinstance(tree, exp.Update) or str(guards.get("is_publish")) != "0" or str(guards.get("isdeleted")) != "0" or not guards.get("lastmodificationtime"):
                _block("草稿修改必须限制未发布、未删除及原修改时间。")
    row_id = guards.get("id")
    if not isinstance(row_id, str) or not row_id.strip() or len(row_id) > 128:
        _block("缺少非空的单个唯一 ID；不允许空值、列表或批量 ID。")
    design_id = statement.get("design_id")
    owner = row_id if table == "cpm_bizflows_design" else guards.get("design_id")
    if not isinstance(design_id, str) or not design_id.strip() or owner != design_id:
        _block("语句的唯一 ID 或所属草稿与审批目标不一致。")
    return {"safe": True, "table": table, "row_id": row_id, "design_id": design_id,
            "operation": tree.key, "expected_rows": 1,
            "summary": "检查通过：单条 SQL，单个非空 ID，限定一个草稿，预计影响 1 行。"}


def check_statement_list(statements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(statements, list) or not statements:
        _block("没有可以独立审批的语句。")
    checks = [check_single_row_statement(statement) for statement in statements]
    targets: set[tuple[str, str]] = set()
    for check in checks:
        key = (check["table"], check["row_id"].casefold())
        if key in targets:
            _block("同一数据行出现多条修改，必须合并为一条明确的单行语句。")
        targets.add(key)
    return checks
