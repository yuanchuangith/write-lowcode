"""Resource budgets shared by planning, validation and capability discovery."""

MAX_OPERATION_BYTES = 2 * 1024 * 1024
MAX_REVIEW_BYTES = 2 * 1024 * 1024
MAX_SCENARIO_BYTES = 1024 * 1024


def planning_limits():
    return {"max_operation_bytes": MAX_OPERATION_BYTES, "max_review_bytes": MAX_REVIEW_BYTES,
            "max_scenario_bytes": MAX_SCENARIO_BYTES, "operation_count_limit": None,
            "scenario_count_limit": None, "scenario_step_count_limit": None,
            "append_parameter": "append_to_plan_id", "review_aliases_supported": True}
