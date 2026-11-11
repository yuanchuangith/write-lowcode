from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cpm_config import (
    CpmConfig,
    _atomic_json,
    cpm_lock_path,
    bind_cpm_cache,
    cpm_status_path,
    get_cpm_password,
    load_cpm_config,
)


STATUS_VERSION = 1
MAX_STATUS_BYTES = 1_000_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _empty_status() -> dict[str, Any]:
    return {"version": STATUS_VERSION, "full": {}, "pages": {}}


def _read_status(config=None) -> dict[str, Any]:
    path = cpm_status_path(config)
    try:
        if path.stat().st_size > MAX_STATUS_BYTES:
            return _empty_status()
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != STATUS_VERSION:
            return _empty_status()
        if not isinstance(value.get("full"), dict) or not isinstance(value.get("pages"), dict):
            return _empty_status()
        return value
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError, OSError):
        return _empty_status()


def _write_status(status: dict[str, Any], config=None) -> None:
    _atomic_json(cpm_status_path(config), status)


class _FileLock(AbstractContextManager["_FileLock"]):
    def __init__(self, path: Path, timeout_seconds: float) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.stream: Any = None

    def __enter__(self) -> "_FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0, os.SEEK_END)
        if self.stream.tell() == 0:
            self.stream.write(b"0")
            self.stream.flush()
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self.stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    self.stream.close()
                    self.stream = None
                    raise TimeoutError("CPM_REFRESH_LOCK_TIMEOUT")
                time.sleep(0.1)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.stream is None:
            return
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()
            self.stream = None


def _cli_version(config: CpmConfig) -> str | None:
    package = Path(config.cli_path).resolve().parent.parent / "package.json"
    try:
        data = json.loads(package.read_text(encoding="utf-8"))
        value = data.get("version")
        return str(value) if value else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return None


def _fresh_timestamp(status: dict[str, Any], page_identifier: str | None) -> float | None:
    candidates = [_parse_time(status.get("full", {}).get("last_completed_at"))]
    if page_identifier:
        candidates.append(_parse_time(status.get("pages", {}).get(page_identifier, {}).get("last_completed_at")))
    values = [value for value in candidates if value is not None]
    return max(values) if values else None


def _is_fresh(config: CpmConfig, status: dict[str, Any], page_identifier: str | None) -> bool:
    completed = _fresh_timestamp(status, page_identifier)
    return completed is not None and time.time() - completed <= config.ttl_seconds


def _partial_baseline_exists(snapshot: Path) -> bool:
    try:
        manifest = snapshot / "manifest.json"
        if not manifest.is_file() or not isinstance(json.loads(manifest.read_text(encoding="utf-8")), dict):
            return False
        pages = snapshot / "pages"
        dirs = [entry for entry in pages.iterdir() if entry.is_dir()]
        return bool(dirs) and all((entry / "page-meta.json").is_file() for entry in dirs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def _safe_message(value: Any, password: str) -> str:
    text = str(value or "")
    if password:
        text = text.replace(password, "<redacted>")
    return text[:2000]


def _filesystem_path(path: Path) -> str:
    value = os.path.abspath(os.fspath(path))
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def _remove_tree(path: Path) -> None:
    shutil.rmtree(_filesystem_path(path))


class CpmRefreshManager:
    def __init__(self, config: CpmConfig | None = None) -> None:
        self.config = bind_cpm_cache(config) if config is not None else load_cpm_config()

    def snapshot_status(self) -> dict[str, Any]:
        status = _read_status(self.config)
        snapshot = Path(self.config.snapshot_dir)
        return {
            "source": "cpm_snapshot",
            "configured": True,
            "snapshot_dir": str(snapshot),
            "snapshot_exists": snapshot.is_dir(),
            "stale": not _is_fresh(self.config, status, None),
            "ttl_seconds": self.config.ttl_seconds,
            "timeout_seconds": self.config.timeout_seconds,
            "cli_version": _cli_version(self.config),
            "refresh_mode": None,
            "refreshed_at": status.get("full", {}).get("last_completed_at"),
            "failures": status.get("full", {}).get("report", {}).get("failures", []),
            "refresh_status": status,
        }

    def validate_token(self) -> dict[str, Any]:
        """Validate the cached platform token through the CLI's lightweight online check."""
        snapshot = Path(self.config.snapshot_dir)
        if not (snapshot / ".cpm" / "config.json").is_file():
            return {
                "source": "cpm_snapshot",
                "ok": False,
                "online": True,
                "token_valid": False,
                "cli_version": _cli_version(self.config),
                "failures": [{"type": "CONFIG_REQUIRED", "reason": "CPM 快照尚未绑定平台"}],
                "error": {"code": "CONFIG_REQUIRED", "message": "CPM 快照尚未绑定平台，请先完成配置和首次拉取"},
            }
        try:
            env = os.environ.copy()
            env.pop("CPM_ACCOUNT", None)
            env.pop("CPM_PASSWORD", None)
            result = self._run(
                [self.config.node_path, self.config.cli_path, "whoami", "--out", str(snapshot)],
                env,
                min(float(self.config.timeout_seconds), 30.0),
            )
        except subprocess.TimeoutExpired:
            return {
                "source": "cpm_snapshot",
                "ok": False,
                "online": True,
                "token_valid": False,
                "cli_version": _cli_version(self.config),
                "failures": [{"type": "TOKEN_CHECK_TIMEOUT", "reason": "在线 token 验证超时"}],
                "error": {"code": "TOKEN_CHECK_TIMEOUT", "message": "在线 token 验证超过 30 秒"},
            }
        valid = result.returncode == 0
        message = _safe_message(result.stdout or result.stderr, "").strip() if valid else (
            "token 无效或已过期；请运行平台对应的 configure_cpm 脚本，"
            "或执行 cpm pull 通过操作系统凭据存储安全重登"
        )
        return {
            "source": "cpm_snapshot",
            "ok": valid,
            "online": True,
            "token_valid": valid,
            "cli_version": _cli_version(self.config),
            "message": message,
            "failures": [] if valid else [{"type": "TOKEN_INVALID", "reason": message or "token 无效"}],
            "error": None if valid else {"code": "TOKEN_INVALID", "message": message or "token 无效或已过期"},
        }

    def ensure_fresh(self, page_identifier: str | None = None) -> dict[str, Any]:
        status = _read_status(self.config)
        if _is_fresh(self.config, status, page_identifier) and Path(self.config.snapshot_dir).is_dir():
            return self._cached_response(status, page_identifier)
        return self.refresh(force=False, page_identifier=page_identifier)

    def _cached_response(self, status: dict[str, Any], page_identifier: str | None) -> dict[str, Any]:
        page_state = status.get("pages", {}).get(page_identifier, {}) if page_identifier else {}
        full_state = status.get("full", {})
        selected = page_state if _parse_time(page_state.get("last_completed_at")) else full_state
        return {
            "source": "cpm_snapshot",
            "ok": True,
            "skipped": True,
            "refresh_mode": "page" if selected is page_state else "full",
            "refreshed_at": selected.get("last_completed_at"),
            "cli_version": _cli_version(self.config),
            "failures": selected.get("report", {}).get("failures", []),
        }

    def refresh(self, force: bool = False, page_identifier: str | None = None) -> dict[str, Any]:
        password = get_cpm_password(self.config)
        timeout = self.config.timeout_seconds
        started = time.monotonic()
        try:
            with _FileLock(cpm_lock_path(self.config), timeout):
                current = _read_status(self.config)
                if not force and _is_fresh(self.config, current, page_identifier):
                    return self._cached_response(current, page_identifier)
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError("CPM_REFRESH_TIMEOUT")
                mode = "page" if page_identifier and _partial_baseline_exists(Path(self.config.snapshot_dir)) else "full"
                return self._refresh_locked(mode, page_identifier if mode == "page" else None, password, remaining)
        except TimeoutError as exc:
            return self._failure("REFRESH_TIMEOUT", str(exc), page_identifier, password)

    def _run(self, args: list[str], env: dict[str, str], timeout: float) -> subprocess.CompletedProcess[str]:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        return subprocess.run(
            args,
            env=env,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(0.1, timeout),
            creationflags=flags,
            check=False,
        )

    def _login(self, work_dir: Path, env: dict[str, str], timeout: float, password: str) -> None:
        if self.config.login_url:
            from gxp_project_context.platform_auth import request_login
            token = request_login(self.config.login_url, self.config.account, password, timeout=max(0.1, timeout))
            native = work_dir / ".cpm" / "config.json"
            binding = json.loads(native.read_text(encoding="utf-8")) if native.is_file() else {}
            binding.update(url=self.config.platform_url, apiPrefix=self.config.api_prefix, loginUrl=self.config.login_url)
            for key, value in (("appId", self.config.app_id), ("siteOutId", self.config.site_out_id)):
                if value:
                    binding[key] = value
            _atomic_json(native, binding)
            _atomic_json(work_dir / ".cpm" / "token.json", {"token": token, "obtainedAt": _now()})
            return
        result = self._run(
            [self.config.node_path, self.config.cli_path, "login", "--url", self.config.platform_url, "--out", str(work_dir)],
            env,
            timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(f"CPM_LOGIN_FAILED: {_safe_message(result.stdout or result.stderr, password)}")

    def _pull(self, work_dir: Path, mode: str, page_identifier: str | None, env: dict[str, str], timeout: float, password: str) -> dict[str, Any]:
        args = [
            self.config.node_path,
            self.config.cli_path,
            "pull",
            "--out",
            str(work_dir),
            "--json",
            "--concurrency",
            str(self.config.concurrency),
        ]
        if mode == "page" and page_identifier:
            args.extend(["--page", page_identifier])
        result = self._run(args, env, timeout)
        report: dict[str, Any] | None = None
        for line in reversed(result.stdout.splitlines()):
            try:
                candidate = json.loads(line)
                if isinstance(candidate, dict) and "ok" in candidate:
                    report = candidate
                    break
            except json.JSONDecodeError:
                continue
        if report is None:
            raise RuntimeError(f"CPM_INVALID_JSON: {_safe_message(result.stdout or result.stderr, password)}")
        if result.returncode == 0 and not report.get("ok"):
            raise RuntimeError("CPM_EXIT_CONTRACT_MISMATCH")
        return report

    def _refresh_locked(self, mode: str, page_identifier: str | None, password: str, timeout: float) -> dict[str, Any]:
        snapshot = Path(self.config.snapshot_dir)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        work_dir = Path(tempfile.mkdtemp(prefix=".cpm-refresh-", dir=snapshot.parent))
        started = time.monotonic()
        try:
            if snapshot.is_dir() and not self.config.fresh_pull:
                # The staging copy traverses every resource, even for a single
                # page. Both ends need extended paths on Windows, just like
                # backup cleanup, or an unrelated long filename blocks refresh.
                shutil.copytree(_filesystem_path(snapshot), _filesystem_path(work_dir), dirs_exist_ok=True)
            legacy_credentials = work_dir / ".cpm" / "credentials.json"
            legacy_credentials.unlink(missing_ok=True)
            env = os.environ.copy()
            env["CPM_ACCOUNT"] = self.config.account
            env["CPM_PASSWORD"] = password
            self._mark_attempt(mode, page_identifier)

            def remaining() -> float:
                value = timeout - (time.monotonic() - started)
                if value <= 0:
                    raise TimeoutError("CPM_REFRESH_TIMEOUT")
                return value

            native = work_dir / ".cpm" / "config.json"
            login_required = not native.is_file()
            previous = json.loads(native.read_text(encoding="utf-8")) if native.is_file() else {}
            if (self.config.app_id and self.config.site_out_id) or self.config.login_url or previous.get("loginUrl"):
                expected = {"url": self.config.platform_url, "apiPrefix": self.config.api_prefix}
                if self.config.app_id and self.config.site_out_id:
                    expected.update(appId=self.config.app_id, siteOutId=self.config.site_out_id)
                expected["loginUrl"] = self.config.login_url
                if any((previous.get(key) or "" if key == "loginUrl" else previous.get(key)) != value
                       for key, value in expected.items()):
                    (work_dir / ".cpm" / "token.json").unlink(missing_ok=True)
                    _atomic_json(native, expected)
                    login_required = True
            if login_required:
                self._login(work_dir, env, remaining(), password)
            report = self._pull(work_dir, mode, page_identifier, env, remaining(), password)
            code = report.get("error", {}).get("code") if isinstance(report.get("error"), dict) else None
            if code == "AUTH_EXPIRED":
                self._login(work_dir, env, remaining(), password)
                report = self._pull(work_dir, mode, page_identifier, env, remaining(), password)
            report_error = report.get("error") if isinstance(report.get("error"), dict) else {}
            if mode == "page" and report_error.get("code") == "PARTIAL_BASELINE_REQUIRED":
                mode = "full"
                page_identifier = None
                report = self._pull(work_dir, mode, None, env, remaining(), password)
            if not report.get("ok"):
                error = report.get("error") if isinstance(report.get("error"), dict) else {}
                raise RuntimeError(f"{error.get('code', 'CPM_PULL_FAILED')}: {error.get('message', 'pull failed')}")

            report["outDir"] = str(snapshot)
            self._commit_snapshot(snapshot, work_dir)
            self._mark_completed(mode, page_identifier, report)
            return {
                **report,
                "source": "cpm_snapshot",
                "refresh_mode": mode,
                "refreshed_at": _now(),
                "cli_version": _cli_version(self.config),
            }
        except subprocess.TimeoutExpired as exc:
            return self._failure("REFRESH_TIMEOUT", "CPM CLI 超过刷新超时，旧快照已保留。", page_identifier, password, mode)
        except Exception as exc:
            return self._failure("REFRESH_FAILED", _safe_message(exc, password), page_identifier, password, mode)
        finally:
            if work_dir.exists():
                try:
                    _remove_tree(work_dir)
                except OSError:
                    pass

    @staticmethod
    def _commit_snapshot(snapshot: Path, work_dir: Path) -> None:
        backup = snapshot.parent / f".{snapshot.name}.previous"
        if backup.exists():
            _remove_tree(backup)
        if snapshot.exists():
            os.replace(snapshot, backup)
        try:
            os.replace(work_dir, snapshot)
        except Exception:
            if backup.exists() and not snapshot.exists():
                os.replace(backup, snapshot)
            raise
        if backup.exists():
            try:
                _remove_tree(backup)
            except OSError:
                # The new snapshot is committed; stale-backup cleanup must not
                # turn a successful pull into a failed refresh.
                pass

    def _state_slot(self, status: dict[str, Any], mode: str, page_identifier: str | None) -> dict[str, Any]:
        if mode == "page" and page_identifier:
            return status.setdefault("pages", {}).setdefault(page_identifier, {})
        return status.setdefault("full", {})

    def _mark_attempt(self, mode: str, page_identifier: str | None) -> None:
        status = _read_status(self.config)
        slot = self._state_slot(status, mode, page_identifier)
        slot["last_attempt_at"] = _now()
        _write_status(status, self.config)

    def _mark_completed(self, mode: str, page_identifier: str | None, report: dict[str, Any]) -> None:
        status = _read_status(self.config)
        slot = self._state_slot(status, mode, page_identifier)
        completed = _now()
        slot.update({"last_completed_at": completed, "error": None, "report": report})
        if not report.get("failures"):
            slot["last_clean_at"] = completed
        _write_status(status, self.config)

    def _failure(
        self,
        code: str,
        message: str,
        page_identifier: str | None,
        password: str,
        mode: str | None = None,
    ) -> dict[str, Any]:
        safe = _safe_message(message, password)
        actual_mode = mode or ("page" if page_identifier else "full")
        status = _read_status(self.config)
        slot = self._state_slot(status, actual_mode, page_identifier)
        slot["error"] = {"code": code, "message": safe}
        slot.setdefault("last_attempt_at", _now())
        _write_status(status, self.config)
        return {
            "source": "cpm_snapshot",
            "ok": False,
            "refresh_mode": actual_mode,
            "refreshed_at": slot.get("last_completed_at"),
            "cli_version": _cli_version(self.config),
            "failures": [{"type": code, "reason": safe}],
            "error": {"code": code, "message": safe},
        }
