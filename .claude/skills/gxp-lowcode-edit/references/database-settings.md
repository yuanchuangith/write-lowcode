# Shared Look / Write database settings

Use this local entry only when the user requests configuration inspection or changes. It is available without database configuration, editor activation or a conversation ID; it does not grant business-editing or approval authority. Run `python scripts/database_settings.py` from either plugin directory for the same manual window (`python3` on macOS/Linux).

| Operation | Behavior |
| --- | --- |
| `status` (default) | Read non-secret local settings, their source and revision without connecting or returning passwords. |
| `open` | Open the local settings window for manual edits and hidden password entry into the operating-system credential store. |
| `update` | Submit user-specified non-secret fields with the current `expected_revision`; validate and test before atomically saving the shared profile. |

Only `host`, `port`, `database`, `look_user` and `write_user` are accepted in `changes`. Preserve omitted values. Obtain missing required target values from the user, never from guessed source constants or sample settings. Never pass a password, full connection string or credential-store contents through MCP, chat or logs. Use `open` when credentials are missing or need replacement.

Look and Write share the destination, with separate accounts and their existing permission checks. Plugin-specific timeout, compiler, source and runtime settings remain intact. Legacy settings remain readable; saving through this entry adopts the shared destination without hand-editing both plugin files.

The old `scripts/configure_connection.py` and its platform wrappers open the shared settings window when a shared profile exists. They keep the original configuration workflow before adoption, without writing ignored legacy endpoint changes or overwriting active shared credential references.

A revision mismatch means another settings window saved first: re-read status and reapply only the user's intended fields. Active editing workflows, approvals or execution block destination switching. Report the blocker and follow the established completion/recovery procedure; never auto-cancel, release or clear sessions merely to change configuration. Failed validation/testing keeps the original configuration. Do not repeat an identical failed test or lower account-safety settings to make it pass.

After a successful change, new work uses the shared settings. Reverify earlier evidence against the selected environment. Editing activation, compiler checks, account safety, human approval and saved-draft verification continue to apply. A settings change is not a business save or publication.
