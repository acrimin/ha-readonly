"""Admin-only, read-only HTTP API for inspecting Home Assistant configuration.

v1 constraints (deliberate, do not relax without a new design review):
- GET routes only. There is no write path anywhere in this module, by design.
- Every route requires an authenticated admin user (requires_auth + is_admin).
- Every request is audit-logged (who / method / path / status). Tokens and
  request bodies are never logged.
- All data comes from Home Assistant's in-memory helpers. No file access,
  no .storage reads, no service calls, no state changes.

Verified against HA 2026.7.2 source (target install: 2026.6.4):
- Auth: request["hass_user"] is set by the auth middleware
  (homeassistant/components/http/auth.py); KEY_HASS_USER = "hass_user"
  (homeassistant/components/http/const.py).
- Automations: hass.data["automation"] is the EntityComponent;
  BaseAutomationEntity.raw_config holds the full config
  (homeassistant/components/automation/__init__.py).
- Scripts: hass.data["script"] is the EntityComponent;
  BaseScriptEntity.raw_config holds the full config
  (homeassistant/components/script/__init__.py).
"""

from __future__ import annotations

import logging
import re

from homeassistant.components.http import HomeAssistantView
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.issue_registry import async_get as async_get_issue_registry

try:
    # Verified in HA 2026.7.2 source: homeassistant/components/http/const.py
    from homeassistant.components.http.const import KEY_HASS_USER
except ImportError:  # pragma: no cover - very old HA
    KEY_HASS_USER = "hass_user"

from .const import API_BASE, DATA_VIEWS_REGISTERED, DOMAIN, INTEGRATION_VERSION

_LOGGER = logging.getLogger(__name__)

# IDs we accept in URL paths: entity ids and unique ids only.
_IDENT_RE = re.compile(r"[A-Za-z0-9_.\-]+")
_DOMAIN_RE = re.compile(r"[a-z0-9_]+")
_MAX_STATES = 1000
_MAX_ITEMS = 5000


class _ViewError(Exception):
    """An expected, client-facing API failure."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _validate_ident(value: str) -> bool:
    """Only allow safe characters in URL path identifiers."""
    return bool(value) and _IDENT_RE.fullmatch(value) is not None


def _enum_str(value) -> str | None:
    """Render an enum (or plain value) as a string, tolerantly."""
    if value is None:
        return None
    return getattr(value, "value", value)


def _component(hass: HomeAssistant, domain: str):
    """Return the EntityComponent for a domain, or raise a clean error."""
    component = hass.data.get(domain)
    if component is None:
        raise _ViewError(503, f"{domain}_not_loaded")
    return component


def _find_entity(component, ident: str):
    """Find an entity by entity_id or unique_id, or raise a clean error."""
    if not _validate_ident(ident):
        raise _ViewError(400, "invalid_id")
    for entity in component.entities:
        if entity.entity_id == ident or entity.unique_id == ident:
            return entity
    raise _ViewError(404, "not_found")


def _state_summary(hass: HomeAssistant, entity_id: str) -> dict:
    state = hass.states.get(entity_id)
    if state is None:
        return {"state": None, "last_triggered": None}
    return {
        "state": state.state,
        "last_triggered": state.attributes.get("last_triggered"),
    }


class _ReadonlyView(HomeAssistantView):
    """Base class: admin-only auth, audit logging, safe error handling."""

    requires_auth = True

    async def _get_data(self, hass: HomeAssistant, request, **kwargs):
        raise NotImplementedError

    async def get(self, request, **kwargs):
        user = request.get(KEY_HASS_USER)
        if user is None or not getattr(user, "is_admin", False):
            _LOGGER.warning("%s: forbidden request to %s", DOMAIN, request.path)
            return self.json({"error": "admin_required"}, status_code=403)
        try:
            payload = await self._get_data(request.app["hass"], request, **kwargs)
            status = 200
        except _ViewError as err:
            payload, status = {"error": err.message}, err.status
        except Exception:  # noqa: BLE001 - never leak tracebacks to clients
            _LOGGER.exception("%s: unhandled error for %s", DOMAIN, request.path)
            payload, status = {"error": "internal_error"}, 500
        _LOGGER.info(
            "%s: user=%s method=%s path=%s status=%s",
            DOMAIN,
            getattr(user, "name", "?"),
            request.method,
            request.path,
            status,
        )
        return self.json(payload, status_code=status)


class InfoView(_ReadonlyView):
    """Integration and endpoint inventory."""

    url = API_BASE + "/info"
    name = API_BASE + ":info"

    async def _get_data(self, hass, request, **kwargs):
        return {
            "integration": DOMAIN,
            "version": INTEGRATION_VERSION,
            "homeassistant": HA_VERSION,
            "endpoints": sorted(view.url for view in _VIEWS),
        }


def _automation_summary(hass: HomeAssistant, entity) -> dict:
    return {
        "entity_id": entity.entity_id,
        "unique_id": entity.unique_id,
        "name": entity.name,
        **_state_summary(hass, entity.entity_id),
    }


class AutomationListView(_ReadonlyView):
    """List all automations (summary only)."""

    url = API_BASE + "/automations"
    name = API_BASE + ":automations"

    async def _get_data(self, hass, request, **kwargs):
        entities = list(_component(hass, "automation").entities)
        if len(entities) > _MAX_ITEMS:
            raise _ViewError(500, "too_many_items")
        return {"automations": [_automation_summary(hass, e) for e in entities]}


class AutomationDetailView(_ReadonlyView):
    """Full raw config for one automation, by entity_id or unique_id."""

    url = API_BASE + "/automations/{ident}"
    name = API_BASE + ":automation_detail"

    async def _get_data(self, hass, request, ident, **kwargs):
        entity = _find_entity(_component(hass, "automation"), ident)
        return {
            **_automation_summary(hass, entity),
            "raw_config": getattr(entity, "raw_config", None),
        }


def _script_summary(hass: HomeAssistant, entity) -> dict:
    state = hass.states.get(entity.entity_id)
    return {
        "entity_id": entity.entity_id,
        "unique_id": entity.unique_id,
        "name": entity.name,
        "state": state.state if state else None,
        "last_triggered": state.attributes.get("last_triggered") if state else None,
    }


class ScriptListView(_ReadonlyView):
    """List all scripts (summary only)."""

    url = API_BASE + "/scripts"
    name = API_BASE + ":scripts"

    async def _get_data(self, hass, request, **kwargs):
        entities = list(_component(hass, "script").entities)
        if len(entities) > _MAX_ITEMS:
            raise _ViewError(500, "too_many_items")
        return {"scripts": [_script_summary(hass, e) for e in entities]}


class ScriptDetailView(_ReadonlyView):
    """Full raw config for one script, by entity_id or unique_id."""

    url = API_BASE + "/scripts/{ident}"
    name = API_BASE + ":script_detail"

    async def _get_data(self, hass, request, ident, **kwargs):
        entity = _find_entity(_component(hass, "script"), ident)
        return {
            **_script_summary(hass, entity),
            "raw_config": getattr(entity, "raw_config", None),
        }


def _scene_summary(hass: HomeAssistant, entity) -> dict:
    state = hass.states.get(entity.entity_id)
    return {
        "entity_id": entity.entity_id,
        "unique_id": entity.unique_id,
        "name": entity.name,
        "state": state.state if state else None,
    }


class SceneListView(_ReadonlyView):
    """List all scenes (summary only)."""

    url = API_BASE + "/scenes"
    name = API_BASE + ":scenes"

    async def _get_data(self, hass, request, **kwargs):
        entities = list(_component(hass, "scene").entities)
        if len(entities) > _MAX_ITEMS:
            raise _ViewError(500, "too_many_items")
        return {"scenes": [_scene_summary(hass, e) for e in entities]}


def _scene_config_payload(entity) -> dict | None:
    """Best-effort serializable scene configuration.

    Automation/script entities carry ``raw_config``. Scenes generally don't;
    ``HomeAssistantScene`` exposes ``scene_config`` (a NamedTuple with id,
    name, icon, and a states mapping), which we serialize by hand.
    """
    raw_config = getattr(entity, "raw_config", None)
    if raw_config is not None:
        return raw_config
    scene_config = getattr(entity, "scene_config", None)
    if scene_config is None:
        return None
    states = {}
    for entity_id, state in (getattr(scene_config, "states", None) or {}).items():
        if hasattr(state, "state") and hasattr(state, "attributes"):
            states[entity_id] = {
                "state": state.state,
                "attributes": dict(state.attributes),
            }
        else:
            states[entity_id] = {"state": state, "attributes": {}}
    return {
        "id": getattr(scene_config, "id", None),
        "name": getattr(scene_config, "name", None),
        "icon": getattr(scene_config, "icon", None),
        "states": states,
    }


class SceneDetailView(_ReadonlyView):
    """Scene configuration, best-effort (clean 501 if unavailable)."""

    url = API_BASE + "/scenes/{ident}"
    name = API_BASE + ":scene_detail"

    async def _get_data(self, hass, request, ident, **kwargs):
        entity = _find_entity(_component(hass, "scene"), ident)
        payload = _scene_config_payload(entity)
        if payload is None:
            raise _ViewError(501, "scene_config_unavailable")
        return {**_scene_summary(hass, entity), "raw_config": payload}


class EntityRegistryView(_ReadonlyView):
    """Dump the entity registry (in-memory, no I/O)."""

    url = API_BASE + "/registries/entities"
    name = API_BASE + ":registry_entities"

    async def _get_data(self, hass, request, **kwargs):
        entries = list(er.async_get(hass).entities.values())
        if len(entries) > _MAX_ITEMS:
            raise _ViewError(500, "too_many_items")
        return {
            "entities": [
                {
                    "entity_id": e.entity_id,
                    "unique_id": e.unique_id,
                    "platform": e.platform,
                    "device_id": e.device_id,
                    "area_id": e.area_id,
                    "disabled_by": _enum_str(e.disabled_by),
                    "hidden_by": _enum_str(e.hidden_by),
                    "name": e.name,
                }
                for e in entries
            ]
        }


class DeviceRegistryView(_ReadonlyView):
    """Dump the device registry (in-memory, no I/O)."""

    url = API_BASE + "/registries/devices"
    name = API_BASE + ":registry_devices"

    async def _get_data(self, hass, request, **kwargs):
        entries = list(dr.async_get(hass).devices.values())
        if len(entries) > _MAX_ITEMS:
            raise _ViewError(500, "too_many_items")
        return {
            "devices": [
                {
                    "id": e.id,
                    "name": e.name,
                    "manufacturer": e.manufacturer,
                    "model": e.model,
                    "area_id": e.area_id,
                    "disabled_by": _enum_str(e.disabled_by),
                    "via_device_id": e.via_device_id,
                }
                for e in entries
            ]
        }


class AreaRegistryView(_ReadonlyView):
    """Dump the area registry (in-memory, no I/O)."""

    url = API_BASE + "/registries/areas"
    name = API_BASE + ":registry_areas"

    async def _get_data(self, hass, request, **kwargs):
        entries = list(ar.async_get(hass).areas.values())
        if len(entries) > _MAX_ITEMS:
            raise _ViewError(500, "too_many_items")
        return {"areas": [{"id": e.id, "name": e.name} for e in entries]}


class StatesView(_ReadonlyView):
    """Entity states, optionally filtered by domain, bounded by limit."""

    url = API_BASE + "/states"
    name = API_BASE + ":states"

    async def _get_data(self, hass, request, **kwargs):
        domain = request.query.get("domain")
        if domain is not None and _DOMAIN_RE.fullmatch(domain) is None:
            raise _ViewError(400, "invalid_domain")
        try:
            limit = int(request.query.get("limit", _MAX_STATES))
        except (TypeError, ValueError):
            raise _ViewError(400, "invalid_limit")
        if not 1 <= limit <= _MAX_STATES:
            raise _ViewError(400, "invalid_limit")
        states = hass.states.async_all(domain) if domain else hass.states.async_all()
        return {
            "states": [
                {
                    "entity_id": s.entity_id,
                    "state": s.state,
                    "attributes": dict(s.attributes),
                    "last_changed": s.last_changed,
                    "last_updated": s.last_updated,
                }
                for s in states[:limit]
            ],
            "truncated": len(states) > limit,
        }


class RepairsView(_ReadonlyView):
    """Active repair issues (metadata only, no payloads)."""

    url = API_BASE + "/repairs"
    name = API_BASE + ":repairs"

    async def _get_data(self, hass, request, **kwargs):
        issues = list(async_get_issue_registry(hass).issues.items())
        if len(issues) > _MAX_ITEMS:
            raise _ViewError(500, "too_many_items")
        return {
            "issues": [
                {
                    "domain": domain,
                    "issue_id": issue_id,
                    "severity": _enum_str(issue.severity),
                    "is_fixable": issue.is_fixable,
                    "translation_key": issue.translation_key,
                    "learn_more_url": issue.learn_more_url,
                }
                for (domain, issue_id), issue in issues
            ]
        }


_VIEWS = (
    InfoView,
    AutomationListView,
    AutomationDetailView,
    ScriptListView,
    ScriptDetailView,
    SceneListView,
    SceneDetailView,
    EntityRegistryView,
    DeviceRegistryView,
    AreaRegistryView,
    StatesView,
    RepairsView,
)


def async_register_views(hass: HomeAssistant) -> None:
    """Register all read-only views. Idempotent across reloads."""
    if hass.data.get(DATA_VIEWS_REGISTERED):
        return
    for view_cls in _VIEWS:
        hass.http.register_view(view_cls())
    hass.data[DATA_VIEWS_REGISTERED] = True
