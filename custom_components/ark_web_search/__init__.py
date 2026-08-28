"""The Ark Web Search integration — internet search tool for HA Assist."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, llm

from .const import (
    CONF_API_KEY,
    CONF_DEFAULT_COUNT,
    CONF_TIMEOUT,
    DEFAULT_COUNT,
    DEFAULT_TIMEOUT,
    DOMAIN,
    MAX_COUNT,
    TIME_RANGES,
)
from .llm_api import ArkSearchAPI
from .search_api import ArkSearchError, async_search

_LOGGER = logging.getLogger(__name__)

SERVICE_SEARCH = "search"

SEARCH_SCHEMA = vol.Schema(
    {
        vol.Required("query"): cv.string,
        vol.Optional("count"): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_COUNT)
        ),
        vol.Optional("time_range"): vol.In(TIME_RANGES),
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Ark Web Search from a config entry."""
    conf = {**entry.data, **entry.options}
    api_key = conf[CONF_API_KEY]
    default_count = conf.get(CONF_DEFAULT_COUNT, DEFAULT_COUNT)
    timeout = conf.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)

    api = ArkSearchAPI(hass, api_key, default_count, timeout)
    unregister_api = llm.async_register_api(hass, api)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "unregister_api": unregister_api,
    }

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    async def handle_search(call: ServiceCall) -> ServiceResponse:
        try:
            return await async_search(
                hass,
                api_key,
                call.data["query"],
                count=call.data.get("count", default_count),
                time_range=call.data.get("time_range"),
                timeout=timeout,
            )
        except ArkSearchError as err:
            raise HomeAssistantError(str(err)) from err

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEARCH,
        handle_search,
        schema=SEARCH_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    _LOGGER.info("Ark Web Search LLM API registered (id=%s)", DOMAIN)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    slot = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if slot is not None:
        slot["unregister_api"]()
    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_SEARCH)
    return True
