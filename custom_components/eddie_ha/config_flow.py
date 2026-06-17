"""Config flow for EDDIE Home Assistant."""

from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.const import CONF_URL
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .api import EddieHaAuthError, EddieHaCannotConnect, EddieHaClient, normalize_base_url, websocket_url
from .const import CONF_PAIRING_ID, CONF_TOKEN, CONF_WS_URL, DEFAULT_DISPLAY_NAME, DOMAIN


class EddieHaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle an EDDIE Home Assistant config flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> config_entries.FlowResult:
        """Pair Home Assistant with EDDIE."""
        errors: dict[str, str] = {}

        if user_input is not None:
            base_url = normalize_base_url(user_input[CONF_URL])
            session = async_get_clientsession(self.hass)
            client = EddieHaClient(session, base_url)

            try:
                response = await client.claim_pairing(
                    user_input["pairing_code"],
                    user_input["display_name"],
                )
            except EddieHaAuthError:
                errors["base"] = "invalid_pairing"
            except EddieHaCannotConnect:
                errors["base"] = "cannot_connect"
            else:
                pairing_id = str(response["id"])
                await self.async_set_unique_id(pairing_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input["display_name"],
                    data={
                        "base_url": base_url,
                        CONF_PAIRING_ID: pairing_id,
                        CONF_TOKEN: response["token"],
                        CONF_WS_URL: websocket_url(base_url),
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_URL, default="https://wien.ddns.net:62002"): str,
                    vol.Required("pairing_code"): str,
                    vol.Optional("display_name", default=DEFAULT_DISPLAY_NAME): str,
                }
            ),
            errors=errors,
        )

