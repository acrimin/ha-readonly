"""HA Readonly Inspector: admin-only configuration inspection + guardrailed writes.

v1: read-only GET API for automations, scripts, scenes, registries, states.
v2: guardrailed automation writes (create/update/delete) that reuse the exact
    save path the HA frontend uses: same YAML file, same HA validation,
    same atomic write, same single-automation reload. Writes default to
    dry-run; every applied write is backed up and audit-logged.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .views import async_register_views
from .writes import async_register_write_views


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up from a config entry: register the API views."""
    async_register_views(hass)
    async_register_write_views(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload: nothing stateful to tear down (views are process-global)."""
    return True
