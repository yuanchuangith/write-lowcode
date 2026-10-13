# Safety contract

The local approval application defaults to a read-only component preview derived
from the session baseline and signed desired canvas. Clicking expands a component;
the configuration detail window compares input/output parameters before and after
and highlights changed fields. Display titles are accepted only from the bridge
and registry hashes bound to the approval; unavailable metadata falls back to actual
parameter keys and complete values. Preview interaction never edits parameters,
approves statements, or writes a draft. The code-comparison tab compares complete
saved draft JS/C# from the session baseline with the signed candidate code, aligns
insertions/deletions and highlights differences. It compares the whole action, not
an inferred per-node source map; node expressions remain in the component details.
Copy buttons retain the exact original code, including line endings. Full SQL and
raw nodes remain available.

- The editor handles one existing active draft identified by its design primary key.
- Static variable/action reference diagnostics are advisory and remain visible in `validation.warnings`. Ordinary edits do not require repairing existing bindings when structural validation and native code generation succeed. Dedicated reference repair sessions still back up originals before scanning structured variable bindings. Only reported variable references are neutralized in a non-executable inspection projection; structural failures still block entry. Automatic matches require exact names/expressions, compatible types, a unique source and the native ParamInput visibility rules. Ambiguities require explicit candidate choices; custom expressions, property selections and unmapped references are never guessed. Explicitly requested repairs must resolve and the full candidate must be regenerated and twice structurally validated by the real frontend compiler before any plan exists. The original backup, resolved binding provenance, candidate hashes and ordinary per-statement human approval remain mandatory.
- When an old session blocks entry, a local window lets the user keep it or safely abort it. Closing the window retains it. No new session begins automatically. Manual abort atomically cancels unexecuted approval transactions and ready plans and releases the session without deleting its backups or history. Execution must atomically claim a pending transaction before any artifact or database write; cancellation and execution therefore cannot both win. Confirmed no-write failures and confirmed rollbacks release the session automatically while retaining consumed approvals, backups and audit records. Legacy failures with exact pre-write concurrency/grant rejection messages may also be safely aborted. Failed COMMIT acknowledgement is uncertain even if a subsequent rollback succeeds; executing, uncertain, saved or restore sessions cannot be aborted.
- The published version, publish flag, design ID, RefId, version, creator fields, and unrelated business tables are immutable.
- Database DDL is never executed by this plugin.
- Dedicated least-privilege database accounts remain the preferred mode. `shared_restricted` may reuse an application account after explicit local confirmation and pinning the canonical `SHOW GRANTS` SHA-256 fingerprint. `shared_unrestricted` is an explicit high-risk override for a privileged shared account and requires a stronger local confirmation phrase.
- `shared_restricted` rejects global non-USAGE and server-administration grants. `shared_unrestricted` permits them but does not treat the account as least privilege. Both modes recheck the pinned fingerprint immediately before each write and bind the account mode, privilege override, and fingerprint into the approved transaction hash.
- Fixed SQL templates, parameter counts, design ownership, one-row expectations, and the absence of a generic SQL tool are the enforcement boundary for shared accounts. They do not reduce that account's permissions in Navicat, application code, or any client outside this plugin.
- The MCP server creates plans but has no tool that directly executes a write transaction.
- The interactive approval application requires one manual approval per exact statement and one final execute click.
- Each approval item contains exactly one SQL statement targeting exactly one row. Multiple changes must be separate single-row statements with a nonempty ID; stacked SQL, batch VALUES, OR/IN/range guards, duplicate targets, and empty/list IDs are blocked by the independent single-row checker.
- Before the first database write, the executor verifies a full single-column unique index on each target table's id, locks and checks every target, verifies relation ownership, and rejects zero/multiple existing matches or an already-existing insert ID. Every affected-row count must remain exactly one; all statements retain one atomic transaction.
- The approval window defaults to a plain-language canvas location and before/after summary. Complete SQL, complete parameters and raw node evidence remain separate review tabs; an unsafe statement is blocked with a visible error before approval or execution.
- All approvals bind the statement hash, parameters, design ID, old/new content hashes, expiry, transaction ID, blocking compiler evidence, available advisory runtime evidence, and complete transaction metadata hash.
- Approval tokens are consumed when execution is first attempted; a failed or interrupted attempt requires a newly generated transaction and new per-statement approvals.
- All approvals are collected before the database transaction starts.
- The transaction locks the draft by primary key, rechecks draft state and optimistic-lock values, and rolls back unless all row counts match.
- Full design payloads remain on the local machine. MCP responses expose bounded semantic diffs and hashes.
- Backup files have per-file SHA-256 values and a Credential Manager backed HMAC manifest; altered files or manifests cannot be restored.
- Compiler metadata includes the configured frontend Git commit/workspace fingerprint, frontend version, ActionDesign source-rule hash, stable registry hash, compiled bridge hash, and a complete binding hash. Any drift invalidates the current session or plan.
- Invalid candidate JSON, `validation.valid != true`, revalidation mismatch, missing compiler fields, missing relation data, missing code fields, or blank code for an action type are hard stops before a plan or approval transaction exists.
- No backend source, backend editor endpoint, Redis key, DLL, PDB, or server filesystem is modified by this plugin.
- Candidate C# may additionally be compiled locally against read-only runtime references as an advisory compatibility check. `source_mode=deployment_snapshot` can strengthen that evidence, but `development_build`, missing references, compilation failure, or runtime-reference drift do not block an otherwise compiler-valid database draft save.
- The candidate C# hash and any available runtime-reference manifest are bound into the approval transaction. The candidate C# hash must match the exact `csharp_code` being saved; runtime evidence is not revalidated as a write gate.
- Database verification proves only the unpublished draft and relation rows. It never proves server-side DLL generation, cache invalidation, publication, or runtime execution.
- The existing manual publication path remains outside the plugin and happens after the database draft save. It is responsible for generating the dynamic DLL and its normal cache handling; the existing runtime may also lazily compile C# on first use.
- Restore uses the same approval and verification path as forward changes.
- No publish tool or generic write_sql tool may be added.
