"""Guardrailed write API for Home Assistant automations (v2).

This module implements the same save path the HA frontend itself uses,
verified against HA 2026.6.4 source:

- Persistence: UI-created automations are written to ``automations.yaml``
  (``homeassistant.config.AUTOMATION_CONFIG_PATH``). This module writes to
  the exact same file. No .storage access, no raw config edits elsewhere.
- Validation: every config is validated with HA's own
  ``homeassistant.components.automation.config.async_validate_config_item``
  before anything touches disk. Invalid configs are rejected with 400 and
  nothing is written.
- Write: atomic YAML write (``write_utf8_file_atomic``) under an asyncio
  mutation lock, mirroring ``homeassistant/components/config/view.py``.
- Reload: after an applied create/update, only that single automation is
  reloaded via the ``automation.reload`` service with ``{id: key}`` (the
  UI's own post-write hook). Other automations are untouched. Deletes
  remove the entity-registry entry like the UI does, with no reload.

Additional guardrails this module adds on top of the UI's path:
- ``dry_run`` defaults to true. Nothing is written, backed up, or reloaded
  unless the caller explicitly passes ``"dry_run": false``.
- Every applied write first backs up ``automations.yaml`` to a timestamped
  ``.bak`` file next to the original.
- Every attempt (dry-run or applied) is audit-logged with user, key,
  dry_run/applied flag, and status. Request bodies are never logged.

Deliberate scope limits (do not widen without a design review):
- Automations only. No scripts, scenes, or other domains.
- Admin-only (requires_auth + is_admin), like the v1 read API.
- Validation is syntactic (HA's schema). A config can be valid yet do the
  wrong thing (wrong light, runaway loop). The dry-run diff plus explicit
  human approval is the mitigation for that; this module cannot provide it.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import shutil
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any

import voluptuous as vol
from homeassistant.components.http import HomeAssistantView
from homeassistant.config import AUTOMATION_CONFIG_PATH
from homeassistant.const import CONF_ID, SERVICE_RELOAD
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.util.file import write_utf8_file_atomic
from homeassistant.util.yaml import dump, load_yaml

try:
    # Verified in HA 2026.6.4 source:
    # homeassistant/components/automation/config.py
    from homeassistant.components.automation.config import (
        async_validate_config_item,
    )
except ImportError:  # pragma: no cover - automation component missing
    async_validate_config_item = None  # type: ignore[assignment]

try:
    from homeassistant.components.http.const import KEY_HASS_USER
except ImportError:  # pragma: no cover - very old HA
    KEY_HASS_USER = "hass_user"

from .const import API_BASE, DOMAIN
from .views import _ViewError, _validate_ident

_LOGGER = logging.getLogger(__name__)

_AUTOMATION_DOMAIN = "automation"
_DIFF_MAX_LINES = 200


def _read_yaml(path: str) -> list[dict[str, Any]]:
    """Read automations.yaml in an executor thread. Missing/empty -> []."""
    try:
        data = load_yaml(path)
    except FileNotFoundError:
        return []
    if not data:
        return []
    if not isinstance(data, list):
        raise _ViewError(500, "automations_yaml_unexpected_shape")
    return data


def _write_yaml(path: str, data: list[dict[str, Any]]) -> None:
    """Atomic YAML write, mirroring homeassistant/components/config/view.py."""
    # Dump before opening the file: a dump error must not truncate it.
    contents = dump(data)
    write_utf8_file_atomic(path, contents)


def _backup_yaml(path: str) -> str:
    """Copy automations.yaml to a timestamped .bak next to the original."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = f"{path}.ha_readonly.{stamp}.bak"
    shutil.copy2(path, backup_path)
    return backup_path


def _apply_write_value(
    data: list[dict[str, Any]], config_key: str, new_value: dict[str, Any]
) -> dict[str, Any] | None:
    """Insert or replace one automation, mirroring the UI's _write_value.

    Returns the previous config for that id, or None if it is new.
    """
    # Key ordering matches the frontend's EditAutomationConfigView so diffs
    # against UI-written files stay clean.
    updated_value: dict[str, Any] = {CONF_ID: config_key}
    for key in (
        "alias",
        "description",
        "triggers",
        "trigger",
        "conditions",
        "condition",
        "actions",
        "action",
    ):
        if key in new_value:
            updated_value[key] = new_value[key]
    # Cover any future fields, like the UI does.
    updated_value.update(new_value)

    previous: dict[str, Any] | None = None
    updated = False
    for index, cur_value in enumerate(data):
        if not isinstance(cur_value, dict):
            continue
        if CONF_ID not in cur_value:
            # The UI backfills ids for hand-written entries; do the same so
            # a later delete/reload can address them.
            cur_value[CONF_ID] = uuid.uuid4().hex
        elif cur_value[CONF_ID] == config_key:
            previous = cur_value
            data[index] = updated_value
            updated = True
    if not updated:
        data.append(updated_value)
    return previous


def _unified_diff(previous: dict | None, new: dict | None, key: str) -> list[str]:
    """Small unified diff of one automation's YAML for review."""
    old_text = dump(previous) if previous is not None else ""
    new_text = dump(new) if new is not None else ""
    lines = list(
        difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile=f"{key} (current)",
            tofile=f"{key} (proposed)",
            lineterm="",
        )
    )
    return lines[:_DIFF_MAX_LINES]


class _WriteView(HomeAssistantView):
    """Base class: admin-only auth, audit logging, safe error handling."""

    requires_auth = True
    _mutation_lock = asyncio.Lock()

    async def _authed_user(self, request):
        user = request.get(KEY_HASS_USER)
        if user is None or not getattr(user, "is_admin", False):
            _LOGGER.warning("%s: forbidden write to %s", DOMAIN, request.path)
            return None
        return user

    def _audit(self, user, request, key: str, applied: bool, status: int) -> None:
        _LOGGER.info(
            "%s: user=%s method=%s path=%s key=%s %s status=%s",
            DOMAIN,
            getattr(user, "name", "?"),
            request.method,
            request.path,
            key,
            "APPLIED" if applied else "dry_run",
            status,
        )


class AutomationWriteView(_WriteView):
    """Create or update one automation (upsert), mirroring the UI's POST."""

    url = API_BASE + "/automations/write/{config_key}"
    name = "api:ha_readonly:automations:write"

    async def post(self, request, config_key: str):
        user = await self._authed_user(request)
        if user is None:
            return self.json({"error": "admin_required"}, status_code=403)
        hass: HomeAssistant = request.app["hass"]

        try:
            return await self._handle(hass, user, request, config_key)
        except _ViewError as err:
            self._audit(user, request, config_key, False, err.status)
            return self.json({"error": err.message}, status_code=err.status)
        except Exception:  # noqa: BLE001 - never leak tracebacks to clients
            _LOGGER.exception("%s: unhandled write error for %s", DOMAIN, request.path)
            self._audit(user, request, config_key, False, 500)
            return self.json({"error": "internal_error"}, status_code=500)

    async def _handle(self, hass, user, request, config_key: str):
        if not _validate_ident(config_key):
            raise _ViewError(400, "invalid_id")
        try:
            cv.string(config_key)
        except vol.Invalid as err:
            raise _ViewError(400, f"key_malformed: {err}") from err
        if async_validate_config_item is None:
            raise _ViewError(503, "automation_component_unavailable")

        try:
            body = await request.json()
        except ValueError as err:
            raise _ViewError(400, "invalid_json") from err
        if not isinstance(body, dict) or not isinstance(body.get("automation"), dict):
            raise _ViewError(400, "body_must_contain_automation_object")
        new_config = body["automation"]
        dry_run = body.get("dry_run", True)
        if not isinstance(dry_run, bool):
            raise _ViewError(400, "dry_run_must_be_boolean")

        # HA's own validation, same as the frontend uses. Rejects before
        # anything touches disk.
        try:
            await async_validate_config_item(hass, config_key, new_config)
        except (vol.Invalid, HomeAssistantError) as err:
            raise _ViewError(400, f"validation_failed: {err}") from err

        path = hass.config.path(AUTOMATION_CONFIG_PATH)
        async with self._mutation_lock:
            current = await hass.async_add_executor_job(_read_yaml, path)
            previous = _apply_write_value(current, config_key, new_config)
            new_value = next(
                v for v in current if isinstance(v, dict) and v.get(CONF_ID) == config_key
            )
            diff = _unified_diff(previous, new_value, config_key)
            changed = previous != new_value

            backup_path: str | None = None
            applied = False
            if not dry_run and changed:
                try:
                    backup_path = await hass.async_add_executor_job(_backup_yaml, path)
                except FileNotFoundError:
                    # automations.yaml did not exist yet; nothing to back up.
                    backup_path = None
                await hass.async_add_executor_job(_write_yaml, path, current)
                applied = True

        status = 200
        self._audit(user, request, config_key, applied, status)

        if dry_run or not changed:
            return self.json(
                {
                    "result": "dry_run" if dry_run else "ok_no_change",
                    "key": config_key,
                    "changed": changed,
                    "diff": diff,
                    "previous": previous,
                    "new": new_value,
                },
                status_code=status,
            )

        # Applied: reload just this automation, like the UI's post-write hook.
        # Explicit admin context: this view already required an admin user.
        try:
            await hass.services.async_call(
                _AUTOMATION_DOMAIN,
                SERVICE_RELOAD,
                {CONF_ID: config_key},
                context=Context(user_id=user.id),
            )
        except Exception as err:  # noqa: BLE001 - config is saved; report reload issue
            _LOGGER.warning(
                "%s: wrote %s but reload failed: %s", DOMAIN, config_key, err
            )
            return self.json(
                {
                    "result": "ok_write_reload_failed",
                    "key": config_key,
                    "changed": True,
                    "backup": backup_path,
                    "diff": diff,
                    "previous": previous,
                    "new": new_value,
                },
                status_code=status,
            )

        return self.json(
            {
                "result": "ok",
                "key": config_key,
                "changed": True,
                "backup": backup_path,
                "diff": diff,
                "previous": previous,
                "new": new_value,
            },
            status_code=status,
        )


class AutomationDeleteView(_WriteView):
    """Delete one automation, mirroring the UI's DELETE."""

    url = API_BASE + "/automations/write/{config_key}/delete"
    name = "api:ha_readonly:automations:delete"

    async def post(self, request, config_key: str):
        user = await self._authed_user(request)
        if user is None:
            return self.json({"error": "admin_required"}, status_code=403)
        hass: HomeAssistant = request.app["hass"]

        try:
            return await self._handle(hass, user, request, config_key)
        except _ViewError as err:
            self._audit(user, request, config_key, False, err.status)
            return self.json({"error": err.message}, status_code=err.status)
        except Exception:  # noqa: BLE001 - never leak tracebacks to clients
            _LOGGER.exception("%s: unhandled delete error for %s", DOMAIN, request.path)
            self._audit(user, request, config_key, False, 500)
            return self.json({"error": "internal_error"}, status_code=500)

    async def _handle(self, hass, user, request, config_key: str):
        if not _validate_ident(config_key):
            raise _ViewError(400, "invalid_id")

        try:
            body = await request.json()
        except ValueError:
            body = {}
        dry_run = body.get("dry_run", True) if isinstance(body, dict) else True
        if not isinstance(dry_run, bool):
            raise _ViewError(400, "dry_run_must_be_boolean")

        path = hass.config.path(AUTOMATION_CONFIG_PATH)
        async with self._mutation_lock:
            current = await hass.async_add_executor_job(_read_yaml, path)
            index = next(
                (
                    i
                    for i, v in enumerate(current)
                    if isinstance(v, dict) and v.get(CONF_ID) == config_key
                ),
                None,
            )
            if index is None:
                raise _ViewError(404, "not_found")
            previous = current[index]
            diff = _unified_diff(previous, None, config_key)

            backup_path: str | None = None
            entity_id: str | None = None
            applied = False
            if not dry_run:
                try:
                    backup_path = await hass.async_add_executor_job(_backup_yaml, path)
                except FileNotFoundError:
                    backup_path = None
                current.pop(index)
                await hass.async_add_executor_job(_write_yaml, path, current)
                applied = True

        status = 200
        self._audit(user, request, config_key, applied, status)

        if not dry_run:
            # Mirror the UI's delete hook: drop the entity-registry entry.
            ent_reg = er.async_get(hass)
            entity_id = ent_reg.async_get_entity_id(
                _AUTOMATION_DOMAIN, _AUTOMATION_DOMAIN, config_key
            )
            if entity_id is not None:
                ent_reg.async_remove(entity_id)

        return self.json(
            {
                "result": "dry_run" if dry_run else "ok",
                "key": config_key,
                "backup": backup_path,
                "diff": diff,
                "previous": previous,
                "removed_entity_id": entity_id,
            },
            status_code=status,
        )


_WRITE_VIEWS = (
    AutomationWriteView,
    AutomationDeleteView,
)


def async_register_write_views(hass: HomeAssistant) -> None:
    """Register the guardrailed write views. Idempotent across reloads."""
    key = f"{DOMAIN}_write_views_registered"
    if hass.data.get(key):
        return
    for view_cls in _WRITE_VIEWS:
        hass.http.register_view(view_cls())
    hass.data[key] = True
