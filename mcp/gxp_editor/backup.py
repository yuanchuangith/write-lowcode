from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from .config import runtime_root
from .hashing import canonical_json, design_content_hash, sha256_text


class BackupError(RuntimeError):
    pass


class BackupStore:
    SAFE_REF_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
    SAFE_BACKUP_ID = re.compile(r"^[a-f0-9]{32}$")

    def __init__(self, root: Path | None = None, *, integrity_key: str):
        if not integrity_key:
            raise BackupError("Backup integrity key is required")
        self.root = root or runtime_root() / "backups"
        self.integrity_key = integrity_key.encode("utf-8")
        self.root.mkdir(parents=True, exist_ok=True)

    def _manifest_signature(self, manifest: dict[str, Any]) -> str:
        unsigned = {key: value for key, value in manifest.items() if key != "manifest_hmac_sha256"}
        return hmac.new(self.integrity_key, canonical_json(unsigned).encode("utf-8"), hashlib.sha256).hexdigest()

    def create(
        self,
        draft: dict[str, Any],
        compiler: dict[str, Any],
        runtime_artifacts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ref_id = str(draft["ref_id"])
        if not self.SAFE_REF_ID.fullmatch(ref_id):
            raise BackupError("Draft RefId is not safe for an immutable backup path")
        backup_id = uuid.uuid4().hex
        target = self.root / ref_id / backup_id
        temporary = target.with_name(target.name + ".tmp")
        temporary.mkdir(parents=True, exist_ok=False)
        try:
            files = {
                "design.json": json.dumps(
                    {key: value for key, value in draft.items() if key != "relations"},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                "relations.json": json.dumps(draft.get("relations") or [], ensure_ascii=False, indent=2) + "\n",
                "compiler.json": json.dumps(compiler, ensure_ascii=False, indent=2) + "\n",
                "runtime-artifacts.json": json.dumps(
                    runtime_artifacts or {"available": False}, ensure_ascii=False, indent=2
                )
                + "\n",
            }
            hashes: dict[str, dict[str, Any]] = {}
            for name, content in files.items():
                path = temporary / name
                path.write_text(content, encoding="utf-8")
                hashes[name] = {"sha256": sha256_text(content), "length": len(content.encode("utf-8"))}
            manifest = {
                "backup_id": backup_id,
                "ref_id": ref_id,
                "design_id": str(draft["id"]),
                "content_hash": str(draft["content_hash"]),
                "files": hashes,
            }
            manifest["manifest_hmac_sha256"] = self._manifest_signature(manifest)
            manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
            (temporary / "manifest.json").write_text(manifest_text, encoding="utf-8")
            os.chmod(temporary, 0o700)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary.replace(target)
            return {**manifest, "path": str(target)}
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def load(self, ref_id: str, backup_id: str) -> dict[str, Any]:
        if not self.SAFE_REF_ID.fullmatch(str(ref_id)) or not self.SAFE_BACKUP_ID.fullmatch(str(backup_id)):
            raise BackupError("Backup identity is invalid")
        target = self.root / ref_id / backup_id
        manifest_path = target / "manifest.json"
        if not manifest_path.exists():
            raise BackupError(f"Backup {backup_id!r} was not found for {ref_id!r}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected_signature = str(manifest.get("manifest_hmac_sha256") or "")
        if not expected_signature or not hmac.compare_digest(expected_signature, self._manifest_signature(manifest)):
            raise BackupError("Backup manifest signature is invalid")
        contents: dict[str, Any] = {}
        for name, expected in manifest.get("files", {}).items():
            path = target / name
            content = path.read_text(encoding="utf-8")
            if sha256_text(content) != expected.get("sha256"):
                raise BackupError(f"Backup file hash mismatch: {name}")
            contents[name] = json.loads(content)
        design = contents.get("design.json")
        if not isinstance(design, dict):
            raise BackupError("Backup design snapshot is missing")
        if str(manifest.get("design_id") or "") != str(design.get("id") or ""):
            raise BackupError("Backup design identity does not match its manifest")
        if str(manifest.get("ref_id") or "") != str(design.get("ref_id") or ""):
            raise BackupError("Backup RefId does not match its manifest")
        if str(manifest.get("content_hash") or "") != design_content_hash(design):
            raise BackupError("Backup design content hash does not match its manifest")
        return {"manifest": manifest, **contents, "path": str(target)}
