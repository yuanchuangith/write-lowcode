"""Generate tracked Claude skill mirrors from skills/ without deleting user files."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


SOURCE = Path("skills")
MIRROR = Path(".claude/skills")


def tracked_skill_paths(repo_root: Path) -> tuple[set[Path], set[Path]]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z", "--", SOURCE.as_posix(), MIRROR.as_posix()],
        check=True, capture_output=True, encoding="utf-8",
    )
    paths = [Path(name) for name in result.stdout.split("\0") if name]
    return tuple({path.relative_to(prefix) for path in paths if path.is_relative_to(prefix)}
                 for prefix in (SOURCE, MIRROR))


def _managed_path(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError(f"Invalid managed skill path: {relative}")
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Skill path leaves its managed directory: {path}")
    return path


def _same_file(source: Path, target: Path) -> bool:
    return target.exists() and source.samefile(target)


def mirror_problems(repo_root: Path, source_files: set[Path], mirror_files: set[Path]) -> list[str]:
    problems = [f"Untracked mirror counterpart: {MIRROR / path}" for path in sorted(source_files - mirror_files)]
    problems += [f"Obsolete tracked mirror: {MIRROR / path}" for path in sorted(mirror_files - source_files)]
    for relative in sorted(source_files):
        source = _managed_path(repo_root / SOURCE, relative)
        target = repo_root / MIRROR / relative
        if not source.is_file():
            problems.append(f"Missing source: {SOURCE / relative}")
        elif _same_file(source, target):
            continue  # Includes directory junctions and hard-linked files.
        else:
            target = _managed_path(repo_root / MIRROR, relative)
            if not target.is_file():
                problems.append(f"Missing mirror: {MIRROR / relative}")
            elif source.read_bytes() != target.read_bytes():
                problems.append(f"Different mirror content: {MIRROR / relative}")
    return problems


def sync_skills(repo_root: Path, source_files: set[Path], mirror_files: set[Path]) -> list[Path]:
    pending = []
    for relative in sorted(source_files):
        source = _managed_path(repo_root / SOURCE, relative)
        if not source.is_file():
            raise ValueError(f"Missing source: {SOURCE / relative}")
        target = repo_root / MIRROR / relative
        if _same_file(source, target):
            continue  # Never open the source itself for writing through a junction.
        target = _managed_path(repo_root / MIRROR, relative)
        content = source.read_bytes()
        if target.is_file() and target.read_bytes() == content:
            continue
        if target.exists() and relative not in mirror_files:
            raise ValueError(f"Refusing to overwrite an untracked user file: {MIRROR / relative}")
        pending.append((relative, target, content))
    # Validate all paths and conflicts before making changes. Never remove files.
    for _, target, content in pending:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return [relative for relative, _, _ in pending]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check tracked file sets and exact bytes without writing")
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    try:
        source_files, mirror_files = tracked_skill_paths(repo_root)
        if not source_files:
            raise ValueError("No tracked source skills found")
        if args.check:
            problems = mirror_problems(repo_root, source_files, mirror_files)
            for problem in problems:
                print(problem, file=sys.stderr)
            if problems:
                return 1
            print(f"Skill mirrors match ({len(source_files)} tracked files).")
        else:
            changed = sync_skills(repo_root, source_files, mirror_files)
            print(f"Updated {len(changed)} skill mirror files; linked or matching files were left untouched.")
            if source_files != mirror_files:
                print("Review and stage new mirror files or obsolete tracked paths, then run --check.")
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Skill mirror sync failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
