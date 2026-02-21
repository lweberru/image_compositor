"""Config flow for Image Compositor."""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback

from .const import DOMAIN


class ImageCompositorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Image Compositor."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            return self.async_create_entry(title="Image Compositor", data={})

        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ImageCompositorOptionsFlowHandler(config_entry)


class ImageCompositorOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for Image Compositor."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options flow."""
        if user_input is not None:
            return self.async_create_entry(title="", data={})

        return self.async_show_form(step_id="init", data_schema=vol.Schema({}))
