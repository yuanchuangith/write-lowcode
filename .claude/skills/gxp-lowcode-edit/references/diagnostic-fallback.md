# Bounded readonly fallback

Read this when evidence is missing, stale, truncated, unverified, or a tool fails. Keep confirmed findings and fill only gaps needed for the next conclusion. A complete Look handoff is optional.

## Budget and execution

The skill orchestrates fixed readonly routes; there is no general tool executor. For the same investigation retain attempted tool/argument paths, start time, elapsed wall time and remaining budget. Allow at most **3 additional readonly calls and 30 seconds total**. Switching tools, references or cursors does not reset the budget. Existing graph call/hop/time limits also apply. The normal necessary investigation remains available; this budget limits automatic fallback expansion.

Use the smaller of the remaining time and the existing timeout where an entry point supports a timeout. Otherwise return a proposed request instead of automatically starting it. Include gaps between calls and use `total_elapsed_ms` as tool timing evidence. Do not repeat a failed path, guess missing identifiers, read credentials, or widen access. `next_read` is guidance, not authorization.

`resolve_table_relation.live_timeout_ms` bounds query work, not pool waits, connection setup, ping or connection return. Automatically use that route only when an outer caller can also stop waiting or cancel at the remaining wall-clock deadline. A direct MCP call with only `live_timeout_ms` must remain a proposed request.

## Routes

- Truncated values or reports: use the existing exact `read_edit_value` or `read_plan_review` continuation, retaining the actual session/conversation/plan binding. A fragment is not a complete old value. Do not open an unsolicited preview window.
- Stale design or compiler evidence: obtain the current identity and necessary exact values. A source drift error cannot be waived with a cached successful result.
- Look Schema refresh failure: when all source/target table and column identifiers are already verified, use `resolve_table_relation(force_live=true)` for that specified relation, with `live_timeout_ms` set to the remaining budget (100..30000). This timeout applies only to the explicit-target live path. Missing target columns require a table read first. An explicit-target match does not prove global uniqueness.
- CPM refresh failure: retain explicitly stale candidates as navigation clues; verify current published evidence separately. Never re-label stale data as current.
- Source-layer failure: retain healthy layers; exact-anchor live search may supply current evidence. Never combine old index line numbers with new source.
- Structured tool failure: try one suitable readonly alternative. Use guarded narrow SQL only when structured tools cannot express the query and table/column identities are verified. MCP-to-CLI fallback requires a compatible, already available readonly CLI and a controllable timeout; it cannot solve the same underlying database outage.
- Missing production/runtime evidence: continue independent static work and provide the current minimum evidence request together, respecting existing browser authorization. Do not re-label development evidence as production.

## Stop and review

Stop automatic fallback when evidence is sufficient, budget is exhausted, a path already failed, required inputs are unavailable, or new access is needed. Report confirmed facts, candidate causes, affected conclusions, attempted paths, stop reason and minimal next request.

`evidence_gaps` and `fallback` are unsigned transport guidance. They do not change compiler checks, old-value completeness, source bindings or approval gates. A `failed` review blocks approval. `unverified` retains the existing human-review route and must disclose its limitations; it is not business verification passed. Do not relax fixed expectations or use `manual_review` to waive a real failure.
