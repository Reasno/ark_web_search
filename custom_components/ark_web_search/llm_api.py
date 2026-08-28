"""LLM API exposing a `web_search` tool to any HA conversation agent.

Registered via `llm.async_register_api`, so it shows up in the agent's
config flow under "Control Home Assistant" -> API, alongside "assist" and
"memory". Works with local_openai, OpenAI, Anthropic, Google, AI Tasks etc.
"""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from homeassistant.util.json import JsonObjectType

from .const import DEFAULT_COUNT, DOMAIN, MAX_COUNT, TIME_RANGES
from .search_api import ArkSearchError, async_search

_LOGGER = logging.getLogger(__name__)

API_PROMPT = (
    "You can search the live internet with the web_search tool. Call it only "
    "when the answer depends on current, external, or fast-changing "
    "information (news, weather, prices, schedules, sports, product facts) "
    "that you cannot answer from the Home Assistant state or your own "
    "knowledge. Never call it for questions about devices in this home.\n"
    "Use one short keyword-style query per call (max 100 characters); do not "
    "chain multiple questions into one query. Answer in your own words and "
    "cite the source site name when it matters. Do not paste raw search "
    "results back to the user, and do not mention the tool itself."
)


class WebSearchTool(llm.Tool):
    """Search the web via Volcengine Ark (Doubao) Search."""

    name = "web_search"
    description = (
        "Search the live internet for current information: news, weather, "
        "prices, schedules, sports results, or any fact that may have changed "
        "recently or falls outside your training data. Returns ranked results "
        "with summaries and source URLs."
    )
    parameters = vol.Schema(
        {
            vol.Required("query"): str,
            vol.Optional("time_range"): vol.In(TIME_RANGES),
            vol.Optional("count"): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=MAX_COUNT)
            ),
        }
    )

    def __init__(self, api_key: str, default_count: int, timeout: int) -> None:
        self._api_key = api_key
        self._default_count = default_count
        self._timeout = timeout

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        args = tool_input.tool_args
        try:
            return await async_search(
                hass,
                self._api_key,
                args["query"],
                count=int(args.get("count") or self._default_count),
                time_range=args.get("time_range"),
                timeout=self._timeout,
            )
        except ArkSearchError as err:
            _LOGGER.warning("web_search failed: %s", err)
            raise HomeAssistantError(str(err)) from err


class ArkSearchAPI(llm.API):
    """LLM API bundle exposing the single web_search tool."""

    def __init__(
        self,
        hass: HomeAssistant,
        api_key: str,
        default_count: int = DEFAULT_COUNT,
        timeout: int = 20,
    ) -> None:
        super().__init__(hass=hass, id=DOMAIN, name="Web Search (Ark)")
        self._tools: list[llm.Tool] = [
            WebSearchTool(api_key, default_count, timeout)
        ]

    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        return llm.APIInstance(
            api=self,
            api_prompt=API_PROMPT,
            llm_context=llm_context,
            tools=self._tools,
        )
