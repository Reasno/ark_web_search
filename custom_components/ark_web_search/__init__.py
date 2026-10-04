"""The Ark Web Search integration — search and local content tools."""
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
    MAX_COUNT,
    TIME_RANGES,
)
from .content.catalog import (
    ENGLISH,
    STORIES,
    CatalogError,
    ContentCatalog,
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

    stories_directory = conf.get(CONF_STORIES_DIRECTORY, DEFAULT_STORIES_DIRECTORY)
    english_directory = conf.get(CONF_ENGLISH_DIRECTORY, DEFAULT_ENGLISH_DIRECTORY)
    content_limit = conf.get(CONF_CONTENT_DEFAULT_LIMIT, DEFAULT_CONTENT_LIMIT)
    content_max_fetch_chars = conf.get(
        CONF_CONTENT_MAX_FETCH_CHARS, DEFAULT_CONTENT_MAX_FETCH_CHARS
    )

    stories_catalog = await _load_catalog(
        hass, STORIES, stories_directory
    )
    english_catalog = await _load_catalog(
        hass, ENGLISH, english_directory
    )

    api = ArkSearchAPI(
        hass,
        api_key,
        default_count,
        timeout,
        stories_catalog=stories_catalog,
        english_catalog=english_catalog,
        content_limit=content_limit,
        content_max_fetch_chars=content_max_fetch_chars,
    )
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


async def _load_catalog(
    hass: HomeAssistant, kind: str, directory: str
) -> ContentCatalog | None:
    """Load one content catalog in an executor job.

    Missing/broken catalogs never block setup; web_search stays available and
    the affected content tools return structured errors.
    """
    try:
        return await hass.async_add_executor_job(
            ContentCatalog.load, directory, kind
        )
    except CatalogError as err:
        _LOGGER.warning(
            "Local %s content library unavailable at configured directory: %s",
            kind,
            err,
        )
        return None


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
