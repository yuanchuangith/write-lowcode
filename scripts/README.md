# Skill mirror maintenance

Edit skill files under `skills/`; `.claude/skills/` is the generated mirror for
Claude consumers. After editing, run:

```powershell
python scripts/sync_skills.py
python scripts/sync_skills.py --check
```

The script copies only Git-tracked source skill files. For a newly added source,
stage it first, generate the mirror, then stage the generated counterpart before
running `--check`. Check mode compares both tracked file sets and exact bytes;
`tests/test_skill_contract.py` runs the same consistency check.

Local junctions or hard links that already refer to the source are left untouched.
Sync never deletes files or overwrites conflicting untracked user files. If a
source was removed, review and remove its obsolete tracked mirror separately.

## Offline generation profiling

`profile_generation.py` measures canvas differences, flow comparison, generated
code alignment, and available business-review evidence using saved local inputs:

```powershell
python scripts/profile_generation.py --before before.json --after after.json --output output/profile.json
python scripts/profile_generation.py --artifact saved-approval.json --baseline baseline.json --output output/profile.json
python scripts/profile_generation.py --artifact saved-approval.json --state-db path/to/editor.sqlite3 --output output/profile.json
```

Canvas inputs can be raw `{actionData, globalVariables}` objects or design
objects containing `data` / `data_json`. Code alignment is measured when both
designs include `js_code` or `csharp_code`. Artifact mode reads `desired_design`
and obtains the baseline from `--baseline`, an embedded `baseline`, or a read-only
local SQLite session lookup. On Windows, the existing
`%LOCALAPPDATA%/GxpLowcodeEditor/state/editor.sqlite3` is the default lookup file.
The script does not initialize or migrate that file.

Add `--frontend path/to/frontend` to opt in to the verified native compiler:

```powershell
python scripts/profile_generation.py --artifact saved-approval.json --baseline baseline.json --frontend path/to/frontend --output output/native-profile.json
```

`--bridge` defaults to `dist/action-design-compiler/bridge.js` relative to that
repository. Raw canvas files also need `--action-code ActionIdentifier`. Native
profiling applies one same-parameters patch in memory and uses the normal event
validation and regeneration consistency checks. It requires a parameterized
SelectData, QueryData, SetVariable, or SetVariableValue node. It never executes
generated code, saves a draft, loads database configuration, or connects to the
business database. Each Node process has a 25-second timeout; native stages run
once each. `--repeats` accepts 1–3 model executions (default 3: two wall-clock
samples and one separately instrumented pass). JSON output contains timings,
call counts and validation flags; business expressions and source code are not
included. Use the editor's Python environment so its existing dependencies are
available.
