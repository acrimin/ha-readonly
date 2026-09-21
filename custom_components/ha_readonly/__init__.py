"""HA Readonly Inspector: admin-only, read-only configuration inspection API."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .views import async_register_views


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up from a config entry: register the read-only API views."""
    async_register_views(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload: nothing stateful to tear down (views are process-global)."""
    return True
