"""Config flow for the HA Readonly Inspector integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigFlow

from .const import DOMAIN


class HaReadonlyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Single-step, single-instance config flow (no options needed)."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step: create the entry immediately."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        return self.async_create_entry(title="HA Readonly Inspector", data={})
