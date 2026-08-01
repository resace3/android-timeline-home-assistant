"""Config flow for Android Timeline."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_ADMIN_TOKEN, CONF_BASE_URL, DEFAULT_BASE_URL, DOMAIN

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BASE_URL, default=DEFAULT_BASE_URL): str,
        vol.Optional(CONF_ADMIN_TOKEN, default=""): str,
    }
)


class AndroidTimelineConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the app's URL and, if it is not reachable via ingress, a token."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            base_url = str(user_input[CONF_BASE_URL]).rstrip("/")
            token = str(user_input.get(CONF_ADMIN_TOKEN, ""))

            await self.async_set_unique_id(base_url)
            self._abort_if_unique_id_configured()

            session = async_get_clientsession(self.hass)
            try:
                async with session.get(f"{base_url}/api/v1/health", timeout=15) as response:
                    if response.status >= 400:
                        errors["base"] = "cannot_connect"
                    else:
                        payload = await response.json()
                        if payload.get("status") != "ok":
                            errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "cannot_connect"

            if not errors:
                # The token is only checked against an admin endpoint,
                # because /api/v1/health is deliberately unauthenticated.
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                try:
                    async with session.get(
                        f"{base_url}/api/v1/admin/devices",
                        headers=headers,
                        timeout=15,
                    ) as response:
                        if response.status in (401, 403):
                            errors[CONF_ADMIN_TOKEN] = "invalid_auth"
                except Exception:
                    errors["base"] = "cannot_connect"

            if not errors:
                return self.async_create_entry(
                    title="Android Timeline",
                    data={CONF_BASE_URL: base_url, CONF_ADMIN_TOKEN: token},
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )
