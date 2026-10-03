"""HA Readonly Inspector: admin-only configuration inspection + guardrailed writes.

v1: read-only GET API for automations, scripts, scenes, registries, states.
v2: guardrailed automation writes (create/update/delete) that reuse the exact
    save path the HA frontend uses: same YAML file, same HA validation,
    same atomic write, same single-automation reload. Writes default to
    dry-run; every applied write is backed up and audit-logged.
v0.7.0: guardrailed generic service calls, file tools under the config dir,
    and Lovelace dashboard read/write. Same rails: admin-only, dry-run
    defaults, audit logging, backups before applied writes.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .views import async_register_views
from .writes import async_register_write_views
from .self_update import async_register_self_update_views
from .services import async_register_service_views
from .files import async_register_file_views
from .lovelace import async_register_lovelace_views


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up from a config entry: register the API views."""
    async_register_views(hass)
    async_register_write_views(hass)
    async_register_self_update_views(hass)
    async_register_service_views(hass)
    async_register_file_views(hass)
    async_register_lovelace_views(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload: nothing stateful to tear down (views are process-global)."""
    return True
