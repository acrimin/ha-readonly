"""Guardrailed Lovelace dashboard read/write (v0.7.0).

- GET  /api/ha_readonly/lovelace             read the default dashboard config
- POST /api/ha_readonly/lovelace/write       replace the dashboard config

How it works: in the default storage mode the dashboard lives in
``.storage/lovelace`` (JSON: {"key": "lovelace", "data": {"config": {...}}});
in YAML mode it is ``ui-lovelace.yaml``. Reads detect the mode; writes keep
the file's own format, take a timestamped backup first, and return a diff.
Same guardrails as ever: admin-only, dry_run defaults true, audit-logged
(config bodies never logged, only view/card counts).

Deliberate scope limits (do not widen without a design review):
- Default dashboard only (no url_path support yet).
- Storage mode: only ``data.config`` is replaced; the surrounding
  .storage envelope (key, version) is preserved byte-for-byte.
- A write that fails validation (config must be a dict with a "views"
  list) is rejected before anything touches disk.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.util.yaml import dump, load_yaml

from .const import API_BASE, DOMAIN
from .views import _ViewError

_LOGGER = logging.getLogger(__name__)

try:
    from homeassistant.components.http.const import KEY_HASS_USER
except ImportError:  # pragma: no cover - very old HA
    KEY_HASS_USER = "hass_user"

_DIFF_MAX_LINES = 300


def _dashboard_paths(hass: HomeAssistant) -> tuple[str, str]:
    """Return (mode, path): storage .storage/lovelace or ui-lovelace.yaml."""
    config_dir = os.path.realpath(hass.config.config_dir)
    storage_path = os.path.join(config_dir, ".storage", "lovelace")
    if os.path.isfile(storage_path):
        return "storage", storage_path
    yaml_path = os.path.join(config_dir, "ui-lovelace.yaml")
    return "yaml", yaml_path


def _read_dashboard(dashboard_path: str, mode: str) -> dict[str, Any]:
    if mode == "storage":
        with open(dashboard_path, encoding="utf-8") as fh:
            envelope = json.load(fh)
        config = (envelope.get("data") or {}).get("config")
        if not isinstance(config, dict):
            raise _ViewError(500, "storage_dashboard_config_unexpected_shape")
        return config
    if not os.path.isfile(dashboard_path):
        raise _ViewError(404, "yaml_dashboard_not_found")
    config = load_yaml(dashboard_path)
    if not isinstance(config, dict):
        raise _ViewError(500, "yaml_dashboard_config_unexpected_shape")
    return config


def _config_summary(config: dict[str, Any]) -> dict[str, Any]:
    views = config.get("views") or []
    return {
        "views": len(views),
        "cards": sum(len(v.get("cards") or []) for v in views if isinstance(v, dict)),
        "view_titles": [v.get("title") for v in views if isinstance(v, dict)][:20],
    }


def _diff_configs(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    old_lines = json.dumps(old, indent=1, sort_keys=True).splitlines()
    new_lines = json.dumps(new, indent=1, sort_keys=True).splitlines()
    return list(
        difflib.unified_diff(old_lines, new_lines, "current", "proposed", lineterm="")
    )[:_DIFF_MAX_LINES]


class _LovelaceView(HomeAssistantView):
    """Base: admin-only auth + audit logging."""

    requires_auth = True

    async def _authed_user(self, request):
        user = request.get(KEY_HASS_USER)
        if user is None or not getattr(user, "is_admin", False):
            _LOGGER.warning("%s: forbidden lovelace access to %s", DOMAIN, request.path)
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


class LovelaceReadView(_LovelaceView):
    """Read the default dashboard config."""

    url = API_BASE + "/lovelace"
    name = API_BASE + ":lovelace:read"

    async def get(self, request):
        user = await self._authed_user(request)
        if user is None:
            return self.json({"error": "admin_required"}, status_code=403)
        hass: HomeAssistant = request.app["hass"]
        try:
            mode, path = await hass.async_add_executor_job(_dashboard_paths, hass)
            config = await hass.async_add_executor_job(_read_dashboard, path, mode)
        except _ViewError as err:
            self._audit(user, request, "read", False, err.status)
            return self.json({"error": err.message}, status_code=err.status)
        self._audit(user, request, "read", False, 200)
        return self.json(
            {
                "result": "ok",
                "mode": mode,
                "summary": _config_summary(config),
                "config": config,
            }
        )


class LovelaceWriteView(_LovelaceView):
    """Replace the default dashboard config (backup + diff, dry-run default).

    Body: {"dry_run": true, "config": {...}}
    """

    url = API_BASE + "/lovelace/write"
    name = API_BASE + ":lovelace:write"

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
        new_config = body.get("config")
        dry_run = body.get("dry_run", True)
        if not isinstance(dry_run, bool):
            raise _ViewError(400, "dry_run_must_be_boolean")
        try:
            return await self._handle(hass, user, request, new_config, dry_run)
        except _ViewError as err:
            self._audit(user, request, "write", False, err.status)
            return self.json({"error": err.message}, status_code=err.status)
        except Exception:  # noqa: BLE001 - never leak tracebacks
            _LOGGER.exception("%s: unhandled lovelace write error", DOMAIN)
            self._audit(user, request, "write", False, 500)
            return self.json({"error": "internal_error"}, status_code=500)

    async def _handle(self, hass, user, request, new_config, dry_run: bool):
        if not isinstance(new_config, dict) or not isinstance(
            new_config.get("views"), list
        ):
            raise _ViewError(400, "config_must_be_dict_with_views_list")
        mode, path = await hass.async_add_executor_job(_dashboard_paths, hass)
        old_config = await hass.async_add_executor_job(_read_dashboard, path, mode)
        diff = await hass.async_add_executor_job(_diff_configs, old_config, new_config)

        if dry_run:
            self._audit(user, request, "write", False, 200)
            return self.json(
                {
                    "result": "dry_run",
                    "mode": mode,
                    "old_summary": _config_summary(old_config),
                    "new_summary": _config_summary(new_config),
                    "diff": diff,
                }
            )

        def _write() -> str:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = f"{path}.bak-{stamp}"
            if os.path.isfile(path):
                shutil.copy2(path, backup)
            if mode == "storage":
                with open(path, encoding="utf-8") as fh:
                    envelope = json.load(fh)
                envelope["data"]["config"] = new_config
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(envelope, fh, indent=2)
                    fh.write("\n")
                os.replace(tmp, path)
            else:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(dump(new_config))
                os.replace(tmp, path)
            return backup

        backup = await hass.async_add_executor_job(_write)
        self._audit(user, request, "write", True, 200)
        return self.json(
            {
                "result": "applied",
                "mode": mode,
                "backup": os.path.basename(backup),
                "old_summary": _config_summary(old_config),
                "new_summary": _config_summary(new_config),
                "diff": diff,
                "note": "Reload the dashboard in the UI to see the change. "
                "Backup taken next to the original.",
            }
        )


_LOVELACE_VIEWS = (LovelaceReadView, LovelaceWriteView)


def async_register_lovelace_views(hass: HomeAssistant) -> None:
    """Register the Lovelace views. Idempotent across reloads."""
    key = f"{DOMAIN}_lovelace_views_registered"
    if hass.data.get(key):
        return
    for view_cls in _LOVELACE_VIEWS:
        hass.http.register_view(view_cls())
    hass.data[key] = True
