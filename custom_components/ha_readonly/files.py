"""Guardrailed file management under the HA config dir (v0.7.0).

The integration runs inside Home Assistant with filesystem access; these
endpoints expose a safe subset so routine cleanup (stale backups, etc.)
never again needs a manual terminal session:

- GET  /api/ha_readonly/files?path=<rel>          list a directory
- GET  /api/ha_readonly/files/read?path=<rel>     read a text file
- POST /api/ha_readonly/files/delete              delete a file or dir

Safety rails (same philosophy as the write endpoints):
- Admin-only, every attempt audit-logged (never file contents).
- All paths are resolved against the HA config dir; anything escaping it
  ("..", absolute paths, symlinks pointing out) is rejected with 400.
- Hidden trap: ``custom_components/`` is special. Deleting or replacing
  integration code can break HA at the next restart, so deletes under
  ``custom_components/`` require an explicit ``"confirm": "yes-i-know"``
  field in the body, in addition to dry_run=false.
- Delete defaults to dry_run=true (pure echo of what would go). Applied
  deletes move the target into ``<config>/ha_readonly_backups/trash/``
  (timestamped) instead of unlinking, so recovery is a manual move.
- Reads are capped (default 2000 lines, 256 KiB) and text-only; binary
  files are refused. Listing is capped at 2000 entries.

Deliberate scope limits (do not widen without a design review):
- Config dir only. No writes outside it, no file creation or editing
  (use the domain-specific endpoints for automations/scripts/dashboards).
- No recursive delete of ``custom_components`` itself or ``.storage``.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import API_BASE, DOMAIN
from .views import _ViewError

_LOGGER = logging.getLogger(__name__)

try:
    from homeassistant.components.http.const import KEY_HASS_USER
except ImportError:  # pragma: no cover - very old HA
    KEY_HASS_USER = "hass_user"

_MAX_LIST_ENTRIES = 2000
_MAX_READ_LINES = 2000
_MAX_READ_BYTES = 256 * 1024

# Subtrees where a delete needs the extra confirmation handshake.
_SENSITIVE_PREFIXES = ("custom_components", ".storage")


def _resolve(hass: HomeAssistant, rel: str) -> str:
    """Resolve a user-supplied relative path under the config dir.

    Rejects absolute paths, ".." escapes, and symlinks that point out.
    Returns the absolute path. Raises _ViewError(400) on any violation.
    """
    if not isinstance(rel, str) or not rel or rel.startswith("/"):
        raise _ViewError(400, "path_must_be_relative")
    base = os.path.realpath(hass.config.config_dir)
    # normpath collapses ".."; realpath resolves symlinks in parents.
    abs_path = os.path.realpath(os.path.join(base, os.path.normpath(rel)))
    if abs_path != base and not abs_path.startswith(base + os.sep):
        raise _ViewError(400, "path_escapes_config_dir")
    return abs_path


def _rel_of(hass: HomeAssistant, abs_path: str) -> str:
    return os.path.relpath(abs_path, os.path.realpath(hass.config.config_dir))


class _FileView(HomeAssistantView):
    """Base: admin-only auth + audit logging."""

    requires_auth = True

    async def _authed_user(self, request):
        user = request.get(KEY_HASS_USER)
        if user is None or not getattr(user, "is_admin", False):
            _LOGGER.warning("%s: forbidden file access to %s", DOMAIN, request.path)
            return None
        return user

    def _audit(self, user, request, detail: str, applied: bool, status: int) -> None:
        _LOGGER.info(
            "%s: user=%s method=%s path=%s %s %s status=%s",
            DOMAIN,
            getattr(user, "name", "?"),
            request.method,
            request.path,
            detail,
            "APPLIED" if applied else "dry_run",
            status,
        )


class FileListView(_FileView):
    """List a directory under the config dir."""

    url = API_BASE + "/files"
    name = API_BASE + ":files:list"

    async def get(self, request):
        user = await self._authed_user(request)
        if user is None:
            return self.json({"error": "admin_required"}, status_code=403)
        hass: HomeAssistant = request.app["hass"]
        rel = request.query.get("path", "")
        try:
            abs_path = await hass.async_add_executor_job(_resolve, hass, rel or ".")
            entries = await hass.async_add_executor_job(self._list, abs_path)
        except _ViewError as err:
            self._audit(user, request, f"list {rel}", False, err.status)
            return self.json({"error": err.message}, status_code=err.status)
        self._audit(user, request, f"list {rel}", False, 200)
        return self.json({"result": "ok", "path": rel or ".", "entries": entries})

    @staticmethod
    def _list(abs_path: str) -> list[dict[str, Any]]:
        if not os.path.isdir(abs_path):
            raise _ViewError(404, "not_a_directory")
        out: list[dict[str, Any]] = []
        with os.scandir(abs_path) as it:
            for entry in it:
                out.append(
                    {
                        "name": entry.name,
                        "is_dir": entry.is_dir(follow_symlinks=False),
                        "size": entry.stat(follow_symlinks=False).st_size
                        if not entry.is_dir(follow_symlinks=False)
                        else None,
                    }
                )
                if len(out) >= _MAX_LIST_ENTRIES:
                    break
        out.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return out


class FileReadView(_FileView):
    """Read a text file under the config dir (bounded)."""

    url = API_BASE + "/files/read"
    name = API_BASE + ":files:read"

    async def get(self, request):
        user = await self._authed_user(request)
        if user is None:
            return self.json({"error": "admin_required"}, status_code=403)
        hass: HomeAssistant = request.app["hass"]
        rel = request.query.get("path", "")
        if not rel:
            return self.json({"error": "path_required"}, status_code=400)
        try:
            abs_path = await hass.async_add_executor_job(_resolve, hass, rel)
            content, truncated = await hass.async_add_executor_job(self._read, abs_path)
        except _ViewError as err:
            self._audit(user, request, f"read {rel}", False, err.status)
            return self.json({"error": err.message}, status_code=err.status)
        # Never log contents; audit records the path only.
        self._audit(user, request, f"read {rel}", False, 200)
        return self.json(
            {
                "result": "ok",
                "path": rel,
                "truncated": truncated,
                "content": content,
            }
        )

    @staticmethod
    def _read(abs_path: str) -> tuple[str, bool]:
        if not os.path.isfile(abs_path):
            raise _ViewError(404, "not_a_file")
        if os.path.getsize(abs_path) > _MAX_READ_BYTES * 4:
            raise _ViewError(413, "file_too_large")
        with open(abs_path, "rb") as fh:
            raw = fh.read(_MAX_READ_BYTES + 1)
        if b"\x00" in raw[:8192]:
            raise _ViewError(415, "binary_file_refused")
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        truncated = len(raw) > _MAX_READ_BYTES or len(lines) > _MAX_READ_LINES
        return "\n".join(lines[:_MAX_READ_LINES]), truncated


class FileDeleteView(_FileView):
    """Delete a file/dir by moving it to the trash (never unlink).

    Body: {"dry_run": true, "path": "<rel>", "confirm": "yes-i-know"}
    ``confirm`` is required only for paths under custom_components/ or
    .storage. Applied deletes move the target to
    <config>/ha_readonly_backups/trash/<stamp>-<name>.
    """

    url = API_BASE + "/files/delete"
    name = API_BASE + ":files:delete"

    async def post(self, request):
        user = await self._authed_user(request)
        if user is None:
            return self.json({"error": "admin_required"}, status_code=403)
        hass: HomeAssistant = request.app["hass"]
        try:
            body = await request.json()
        except ValueError as err:
            raise _ViewError(400, "invalid_json") from err
        if not isinstance(body, dict):
            raise _ViewError(400, "body_must_be_object")
        rel = body.get("path", "")
        dry_run = body.get("dry_run", True)
        if not isinstance(dry_run, bool):
            raise _ViewError(400, "dry_run_must_be_boolean")
        try:
            return await self._handle(hass, user, request, rel, dry_run, body)
        except _ViewError as err:
            self._audit(user, request, f"delete {rel}", False, err.status)
            return self.json({"error": err.message}, status_code=err.status)
        except Exception:  # noqa: BLE001 - never leak tracebacks
            _LOGGER.exception("%s: unhandled file delete error for %s", DOMAIN, request.path)
            self._audit(user, request, f"delete {rel}", False, 500)
            return self.json({"error": "internal_error"}, status_code=500)

    async def _handle(self, hass, user, request, rel: str, dry_run: bool, body: dict):
        abs_path = await hass.async_add_executor_job(_resolve, hass, rel)
        norm_rel = _rel_of(hass, abs_path)
        if norm_rel in (".", ""):
            raise _ViewError(400, "refusing_to_delete_config_root")
        first = norm_rel.split(os.sep)[0]
        if first in _SENSITIVE_PREFIXES:
            if body.get("confirm") != "yes-i-know":
                raise _ViewError(
                    400,
                    "confirm_required: deleting under "
                    f"{first}/ can break HA; add \"confirm\": \"yes-i-know\"",
                )
            if first == ".storage":
                raise _ViewError(400, "refusing_to_delete_storage")
        exists = await hass.async_add_executor_job(os.path.lexists, abs_path)
        if not exists:
            raise _ViewError(404, "path_not_found")

        if dry_run:
            self._audit(user, request, f"delete {norm_rel}", False, 200)
            return self.json(
                {
                    "result": "dry_run",
                    "would_delete": norm_rel,
                    "method": "move_to_trash",
                    "note": "Applied deletes move the target to "
                    "<config>/ha_readonly_backups/trash/ (never unlink).",
                }
            )

        def _trash() -> str:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            trash_dir = os.path.join(
                os.path.realpath(hass.config.config_dir),
                "ha_readonly_backups",
                "trash",
            )
            os.makedirs(trash_dir, exist_ok=True)
            dest = os.path.join(trash_dir, f"{stamp}-{os.path.basename(abs_path)}")
            shutil.move(abs_path, dest)
            return dest

        dest = await hass.async_add_executor_job(_trash)
        self._audit(user, request, f"delete {norm_rel}", True, 200)
        return self.json(
            {
                "result": "deleted",
                "path": norm_rel,
                "trashed_to": _rel_of(hass, dest),
                "note": "Moved to trash, not unlinked. Restore manually if needed.",
            }
        )


_FILE_VIEWS = (FileListView, FileReadView, FileDeleteView)


def async_register_file_views(hass: HomeAssistant) -> None:
    """Register the file-management views. Idempotent across reloads."""
    key = f"{DOMAIN}_file_views_registered"
    if hass.data.get(key):
        return
    for view_cls in _FILE_VIEWS:
        hass.http.register_view(view_cls())
    hass.data[key] = True
