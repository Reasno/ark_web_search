"""Config flow for the Ark Web Search integration."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_API_KEY,
    CONF_CONTENT_DEFAULT_LIMIT,
    CONF_CONTENT_MAX_FETCH_CHARS,
    CONF_DEFAULT_COUNT,
    CONF_ENGLISH_DIRECTORY,
    CONF_STORIES_DIRECTORY,
    CONF_TIMEOUT,
    DEFAULT_CONTENT_LIMIT,
    DEFAULT_CONTENT_MAX_FETCH_CHARS,
    DEFAULT_COUNT,
    DEFAULT_ENGLISH_DIRECTORY,
    DEFAULT_STORIES_DIRECTORY,
    DEFAULT_TIMEOUT,
    DOMAIN,
    CONTENT_FETCH_CHARS_MAX,
    CONTENT_FETCH_CHARS_MIN,
    CONTENT_LIMIT_MAX,
    CONTENT_LIMIT_MIN,
    MAX_COUNT,
)
from .search_api import ArkSearchError, async_search


class ArkWebSearchConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Ark Web Search."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await async_search(
                    self.hass,
                    user_input[CONF_API_KEY].strip(),
                    "home assistant",
                    count=1,
                    timeout=user_input.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
                )
            except ArkSearchError:
                errors["base"] = "cannot_connect"
            else:
                user_input[CONF_API_KEY] = user_input[CONF_API_KEY].strip()
                return self.async_create_entry(
                    title="Web Search & Kids Content", data=user_input
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_API_KEY): cv.string,
                vol.Optional(
                    CONF_DEFAULT_COUNT, default=DEFAULT_COUNT
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_COUNT)),
                vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): vol.All(
                    vol.Coerce(int), vol.Range(min=5, max=60)
                ),
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return ArkWebSearchOptionsFlow()


class ArkWebSearchOptionsFlow(OptionsFlow):
    """Allow tuning search and content options after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            data = {
                key: value
                for key, value in user_input.items()
                if value is not None and str(value).strip()
            }
            return self.async_create_entry(title="", data=data)

        data = {**self.config_entry.data, **self.config_entry.options}
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_DEFAULT_COUNT,
                    default=data.get(CONF_DEFAULT_COUNT, DEFAULT_COUNT),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_COUNT)),
                vol.Optional(
                    CONF_TIMEOUT,
                    default=data.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
                ): vol.All(vol.Coerce(int), vol.Range(min=5, max=60)),
                vol.Optional(
                    CONF_STORIES_DIRECTORY,
                    default=data.get(
                        CONF_STORIES_DIRECTORY, DEFAULT_STORIES_DIRECTORY
                    ),
                ): cv.string,
                vol.Optional(
                    CONF_ENGLISH_DIRECTORY,
                    default=data.get(
                        CONF_ENGLISH_DIRECTORY, DEFAULT_ENGLISH_DIRECTORY
                    ),
                ): cv.string,
                vol.Optional(
                    CONF_CONTENT_DEFAULT_LIMIT,
                    default=data.get(
                        CONF_CONTENT_DEFAULT_LIMIT, DEFAULT_CONTENT_LIMIT
                    ),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=CONTENT_LIMIT_MIN, max=CONTENT_LIMIT_MAX),
                ),
                vol.Optional(
                    CONF_CONTENT_MAX_FETCH_CHARS,
                    default=data.get(
                        CONF_CONTENT_MAX_FETCH_CHARS,
                        DEFAULT_CONTENT_MAX_FETCH_CHARS,
                    ),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(
                        min=CONTENT_FETCH_CHARS_MIN,
                        max=CONTENT_FETCH_CHARS_MAX,
                    ),
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
