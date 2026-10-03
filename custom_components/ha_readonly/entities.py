"""Guardrailed entity registry management (v0.7.3).

Disable (not delete) stale entities via HA's entity registry API:

- POST /api/ha_readonly/entities/disable  {"entity_ids": [...], "dry_run": true}
- POST /api/ha_readonly/entities/enable   {"entity_ids": [...], "dry_run": true}

Safety rails:
- Admin-only, every attempt audit-logged (entity IDs only, never states).
- Dry-run defaults true: returns the list that would be affected, no changes.
- Disable sets disabled_by=USER (reversible via the enable endpoint or UI).
- Entities not in the registry, or already in the target state, are reported
  as skipped, not errors.
- Capped at 200 entity IDs per call.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import API_BASE, DOMAIN
from .views import _ViewError

_LOGGER = logging.getLogger(__name__)

try:
    from homeassistant.components.http.const import KEY_HASS_USER
except ImportError:  # pragma: no cover - very old HA
    KEY_HASS_USER = "hass_user"

_MAX_ENTITY_IDS = 200


class _EntityView(HomeAssistantView):
    """Base: admin-only auth + audit logging."""

    requires_auth = True

    async def _authed_user(self, request):
        user = request.get(KEY_HASS_USER)
        if user is None or not getattr(user, "is_admin", False):
            _LOGGER.warning("%s: forbidden entity access to %s", DOMAIN, request.path)
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


class _EntityBulkView(_EntityView):
    """Shared logic for bulk disable/enable."""

    # Set by subclasses
    _disable: bool = True

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
        entity_ids = body.get("entity_ids", [])
        dry_run = body.get("dry_run", True)
        if not isinstance(entity_ids, list) or not all(
            isinstance(e, str) for e in entity_ids
        ):
            raise _ViewError(400, "entity_ids_must_be_string_list")
        if len(entity_ids) > _MAX_ENTITY_IDS:
            raise _ViewError(400, f"too_many_entity_ids_max_{_MAX_ENTITY_IDS}")
        if not isinstance(dry_run, bool):
            raise _ViewError(400, "dry_run_must_be_boolean")
        action = "disable" if self._disable else "enable"
        try:
            return await self._handle(hass, user, request, entity_ids, dry_run, action)
        except _ViewError as err:
            self._audit(user, request, f"{action} {len(entity_ids)} entities", False, err.status)
            return self.json({"error": err.message}, status_code=err.status)
        except Exception:  # noqa: BLE001 - never leak tracebacks
            _LOGGER.exception("%s: unhandled entity %s error", DOMAIN, action)
            self._audit(user, request, f"{action} {len(entity_ids)} entities", False, 500)
            return self.json({"error": "internal_error"}, status_code=500)

    async def _handle(
        self,
        hass: HomeAssistant,
        user,
        request,
        entity_ids: list[str],
        dry_run: bool,
        action: str,
    ):
        registry = er.async_get(hass)

        def _classify() -> dict[str, list[str]]:
            would_change: list[str] = []
            skipped: list[str] = []
            not_found: list[str] = []
            for eid in entity_ids:
                entry = registry.async_get(eid)
                if entry is None:
                    not_found.append(eid)
                    continue
                is_disabled = entry.disabled_by is not None
                if self._disable and is_disabled:
                    skipped.append(eid)
                elif not self._disable and not is_disabled:
                    skipped.append(eid)
                else:
                    would_change.append(eid)
            return {
                "would_change": would_change,
                "skipped": skipped,
                "not_found": not_found,
            }

        result = await hass.async_add_executor_job(_classify)

        if dry_run:
            self._audit(user, request, f"{action} {len(entity_ids)} entities", False, 200)
            return self.json(
                {
                    "result": "dry_run",
                    "action": action,
                    **result,
                    "note": "Disable is reversible via the enable endpoint or the HA UI.",
                }
            )

        def _apply(ids: list[str]) -> None:
            for eid in ids:
                if self._disable:
                    registry.async_update_entity(
                        eid, disabled_by=er.RegistryEntryDisabler.USER
                    )
                else:
                    registry.async_update_entity(eid, disabled_by=None)

        await hass.async_add_executor_job(_apply, result["would_change"])
        self._audit(user, request, f"{action} {len(entity_ids)} entities", True, 200)
        return self.json(
            {
                "result": action + "d",
                "action": action,
                **result,
                "note": "Reversible via the enable endpoint or the HA UI.",
            }
        )


class EntityDisableView(_EntityBulkView):
    """Disable entities (reversible)."""

    url = API_BASE + "/entities/disable"
    name = API_BASE + ":entities:disable"
    _disable = True


class EntityEnableView(_EntityBulkView):
    """Re-enable entities."""

    url = API_BASE + "/entities/enable"
    name = API_BASE + ":entities:enable"
    _disable = False


_ENTITY_VIEWS = (EntityDisableView, EntityEnableView)


def async_register_entity_views(hass: HomeAssistant) -> None:
    """Register the entity-management views. Idempotent across reloads."""
    key = f"{DOMAIN}_entity_views_registered"
    if hass.data.get(key):
        return
    for view_cls in _ENTITY_VIEWS:
        hass.http.register_view(view_cls())
    hass.data[key] = True
