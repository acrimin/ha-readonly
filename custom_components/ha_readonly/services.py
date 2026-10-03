"""Guardrailed generic service calls (v0.7.0).

POST /api/ha_readonly/services/{domain}/{service}
    Call any Home Assistant service through the same guardrails as the
    automation/script writes: admin-only, dry_run defaults true, every
    attempt audit-logged (never bodies).

    Request body:
    {
      "dry_run": true,                 # default; false to actually call
      "target": {"entity_id": [...]},  # optional service target
      "data": {...}                    # optional service data
    }

    Dry-run validates that the domain/service exists and echoes the exact
    call that would be made. There is no deeper preview: a service call
    is an action, not a config change. The dry-run plus explicit human
    approval is the mitigation, same as the UI's own confirmation.

    Applied calls use hass.services.async_call with blocking=True and
    return the (empty) result. Service calls are not backed up: they are
    actions, not files. The audit log records who called what, when.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import API_BASE, DOMAIN
from .views import _ViewError, _validate_ident

_LOGGER = logging.getLogger(__name__)

try:
    from homeassistant.components.http.const import KEY_HASS_USER
except ImportError:  # pragma: no cover - very old HA
    KEY_HASS_USER = "hass_user"


class _ServiceView(HomeAssistantView):
    """Base: admin-only auth + audit logging, mirroring writes.py."""

    requires_auth = True

    async def _authed_user(self, request):
        user = request.get(KEY_HASS_USER)
        if user is None or not getattr(user, "is_admin", False):
            _LOGGER.warning("%s: forbidden service call to %s", DOMAIN, request.path)
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


class ServiceCallView(_ServiceView):
    """Call any HA service. Dry-run validates and echoes; applied calls."""

    url = API_BASE + "/services/{domain}/{service}"
    name = API_BASE + ":services:call"

    async def post(self, request, domain: str, service: str):
        user = await self._authed_user(request)
        if user is None:
            return self.json({"error": "admin_required"}, status_code=403)
        hass: HomeAssistant = request.app["hass"]
        detail = f"{domain}.{service}"
        try:
            return await self._handle(hass, user, request, domain, service)
        except _ViewError as err:
            self._audit(user, request, detail, False, err.status)
            return self.json({"error": err.message}, status_code=err.status)
        except Exception:  # noqa: BLE001 - never leak tracebacks
            _LOGGER.exception("%s: unhandled service call error for %s", DOMAIN, request.path)
            self._audit(user, request, detail, False, 500)
            return self.json({"error": "internal_error"}, status_code=500)

    async def _handle(self, hass, user, request, domain: str, service: str):
        if not _validate_ident(domain) or not _validate_ident(service):
            raise _ViewError(400, "invalid_domain_or_service")
        if not hass.services.has_service(domain, service):
            raise _ViewError(404, f"unknown_service: {domain}.{service}")

        try:
            body = await request.json()
        except ValueError as err:
            raise _ViewError(400, "invalid_json") from err
        if not isinstance(body, dict):
            raise _ViewError(400, "body_must_be_object")
        dry_run = body.get("dry_run", True)
        if not isinstance(dry_run, bool):
            raise _ViewError(400, "dry_run_must_be_boolean")
        target = body.get("target") or {}
        data = body.get("data") or {}
        if not isinstance(target, dict) or not isinstance(data, dict):
            raise _ViewError(400, "target_and_data_must_be_objects")

        call_summary: dict[str, Any] = {
            "domain": domain,
            "service": service,
            "target": target,
            "data": data,
        }
        if dry_run:
            self._audit(user, request, f"{domain}.{service}", False, 200)
            return self.json(
                {
                    "result": "dry_run",
                    "would_call": call_summary,
                    "note": "Dry-run validates the service exists and echoes the call. "
                    "Pass dry_run=false to actually call it.",
                }
            )

        await hass.services.async_call(domain, service, data, target=target, blocking=True)
        self._audit(user, request, f"{domain}.{service}", True, 200)
        return self.json({"result": "called", "call": call_summary})


_SERVICE_VIEWS = (ServiceCallView,)


def async_register_service_views(hass: HomeAssistant) -> None:
    """Register the service-call views. Idempotent across reloads."""
    key = f"{DOMAIN}_service_views_registered"
    if hass.data.get(key):
        return
    for view_cls in _SERVICE_VIEWS:
        hass.http.register_view(view_cls())
    hass.data[key] = True
