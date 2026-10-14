"""LLM API exposing web search and local content tools to conversation agents.

Registered via `llm.async_register_api`, so it shows up in an agent's config
flow under "Control Home Assistant" -> API. Works with local_openai, OpenAI,
Anthropic, Google, AI Tasks etc.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import llm
from homeassistant.util.json import JsonObjectType
from voluptuous_openapi import UNSUPPORTED

from .const import (
    DEFAULT_CONTENT_MAX_FETCH_CHARS,
    DEFAULT_COUNT,
    DOMAIN,
    MAX_COUNT,
    TIME_RANGES,
)
from .content.catalog import ContentCatalog
from .content.service import ContentService
from .content.tools import (
    EnglishStartTool,
    EnglishContinueTool,
    RiddleNextTool,
    RiddleAnswerTool,
    StoryStartTool,
    StoryContinueTool,
)
from .search_api import ArkSearchError, async_search

_LOGGER = logging.getLogger(__name__)


def _schema_serializer(value: Any) -> Any:
    """Keep coercing validators represented by their original JSON type."""
    if value is cv.boolean:
        return {"type": "boolean"}
    return UNSUPPORTED


WEB_SEARCH_PROMPT = (
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

CONTENT_PROMPT = """Use the local library for children's stories, riddles and English practice.
Start with story_start, riddle_next or english_start; unspecified filters may be omitted.
Present only the returned material. Tell stories faithfully in Chinese; practise English
in short passages. Reveal riddle answers only after a guess or an explicit request.
Wait for the user before continuing, and copy next_cursor exactly. A null cursor means
completion. If topic_matched, age_matched or difficulty_matched is false, explain the mismatch briefly.
Never invent content, references or unavailable results. On invalid_reference, explain
that the reading expired and offer a new item. Follow each tool's interaction rules."""

API_PROMPT = WEB_SEARCH_PROMPT + "\n\n" + CONTENT_PROMPT


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
    """LLM API bundle exposing web search plus the local content tools."""

    def __init__(
        self,
        hass: HomeAssistant,
        api_key: str,
        default_count: int = DEFAULT_COUNT,
        timeout: int = 20,
        stories_catalog: ContentCatalog | None = None,
        english_catalog: ContentCatalog | None = None,
        riddles_catalog: ContentCatalog | None = None,
        content_max_fetch_chars: int = DEFAULT_CONTENT_MAX_FETCH_CHARS,
    ) -> None:
        super().__init__(hass=hass, id=DOMAIN, name="Web Search & Kids Content (Ark)")
        service = ContentService(
            {"stories": stories_catalog, "english": english_catalog, "riddles": riddles_catalog},
            max_chars=content_max_fetch_chars,
        )
        self._tools: list[llm.Tool] = [
            WebSearchTool(api_key, default_count, timeout),
            StoryStartTool(service),
            StoryContinueTool(service),
            EnglishStartTool(service),
            EnglishContinueTool(service),
            RiddleNextTool(service),
            RiddleAnswerTool(service),
        ]

    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        return llm.APIInstance(
            api=self,
            api_prompt=API_PROMPT,
            llm_context=llm_context,
            tools=self._tools,
            custom_serializer=_schema_serializer,
        )
