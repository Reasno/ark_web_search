"""LLM API exposing web search and local content tools to conversation agents.

Registered via `llm.async_register_api`, so it shows up in an agent's config
flow under "Control Home Assistant" -> API. Works with local_openai, OpenAI,
Anthropic, Google, AI Tasks etc.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from homeassistant.util.json import JsonObjectType

from .const import (
    DEFAULT_CONTENT_LIMIT,
    DEFAULT_CONTENT_MAX_FETCH_CHARS,
    DEFAULT_COUNT,
    DOMAIN,
    MAX_COUNT,
    TIME_RANGES,
)
from .content.catalog import ContentCatalog
from .content.tools import (
    EnglishFetchTool,
    EnglishListTool,
    RiddlesFetchTool,
    RiddlesListTool,
    StoriesFetchTool,
    StoriesListTool,
)
from .search_api import ArkSearchError, async_search

_LOGGER = logging.getLogger(__name__)

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

CONTENT_PROMPT = """You have access to a local children's content library through the stories,
English, and riddles tools.

MANDATORY INTENT ROUTING — apply these rules before writing any response:
- Any request to tell, hear, choose, or continue with a children's story MUST
  use the stories tools. For a new or unspecified story, your first action MUST
  be stories_list. Do not answer from memory and do not say that no story is
  available before calling stories_list.
- Any request to guess, hear, or play a riddle, brain teaser, or cold joke MUST
  use the riddles tools. For a new item, your first action MUST be riddles_list.
  Do not invent a question or answer from memory.
- Any request to practice, read, learn, or quiz English with library material
  MUST use the English tools. For a new or unspecified item, your first action
  MUST be english_list. Do not invent practice material from memory.
- Treat short or colloquial commands as complete tool requests. Examples:
  "讲个故事", "来个故事", "讲儿童故事" -> stories_list;
  "猜个谜", "来个脑筋急转弯", "讲个冷笑话" -> riddles_list;
  "练英语", "读英语", "来篇英语阅读" -> english_list.
- Tool use is mandatory for these intents even when the user does not mention a
  tool name, title, age, topic, grade, or level. Missing filters are not a reason
  to ask a follow-up question: call the matching list tool with broad/default
  filters first.
- Never replace a required local-content tool call with web_search, general
  knowledge, an apology, or a claim that suitable content cannot be found.

For storytelling:
- For a new story, use stories_list before choosing it unless the user supplies
  a valid content ID already returned by stories_list in this conversation.
- Match the story to the child's age, interests, requested theme, and desired
  length.
- Call stories_fetch only with an ID returned by stories_list.
- Always tell the story in Chinese, whether the original text is English or
  Chinese; translate English stories into Chinese. Do not read Markdown
  markers, metadata, licenses, or section separators aloud.
- Stay as close to the original as possible (verbatim): preserve the original
  plot, events, order, and dialogue wording. Do not rewrite, improvise,
  modernize, or add/remove events. Translating into Chinese still counts as
  verbatim as long as the meaning and wording stay faithful.
- For long stories with multiple sections, narrate only the sections already
  fetched (the current chapter). When has_more is true, stop at the end of the
  current chapter and wait for the user to confirm before fetching the next
  section with next_section_start. Do not fetch ahead or continue on your own.
- After the whole story is finished, optionally ask one simple question about
  the story.

For English practice:
- Use english_list to select material matching the child's grade, reading
  level, interests, and requested duration.
- Call english_fetch only with an ID returned by english_list.
- Work through short sections instead of reading a long article all at once.
- Default to speaking the English passage first, then invite the child to
  repeat or answer a simple question.
- Keep corrections encouraging and brief. Correct one or two important issues
  at a time.
- Explain difficult vocabulary in simple Chinese when helpful, but keep the
  practice itself mainly in English.
- Never claim that a child pronounced a word correctly or incorrectly unless
  the conversation input provides enough evidence.

For brain teasers, riddles, and cold jokes:
- Use riddles_list to choose a suitable item. Its question field is the riddle
  to ask, and it deliberately does not contain the answer.
- Ask only one riddle at a time. After presenting the question, STOP and wait
  for the user to guess. Never reveal, explain, hint at, or otherwise spoil the
  twist or answer in the same turn as the question.
- Do not call riddles_fetch when asking the question. Keep the returned riddle
  ID available for the next turn.
- Only after the user has made a guess, or explicitly says they do not know,
  give up, or asks for the answer, call riddles_fetch with that ID. Then say
  whether the guess matches, reveal the answer/twist, and briefly explain the
  wordplay if useful.
- If the user asks for another one, choose a new item and repeat this two-turn
  question-then-answer flow.

General rules:
- Never invent titles, content IDs, levels, or passages.
- Do not expose filesystem paths or internal tool details.
- Do not use web_search to replace local stories or English material unless
  the user explicitly asks for internet content.
- If no suitable item is found, say so and adjust the list filters instead of
  fabricating content."""

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
        content_limit: int = DEFAULT_CONTENT_LIMIT,
        content_max_fetch_chars: int = DEFAULT_CONTENT_MAX_FETCH_CHARS,
    ) -> None:
        super().__init__(hass=hass, id=DOMAIN, name="Web Search & Kids Content (Ark)")
        self._tools: list[llm.Tool] = [
            WebSearchTool(api_key, default_count, timeout),
            StoriesListTool(stories_catalog, content_limit),
            StoriesFetchTool(stories_catalog, content_max_fetch_chars),
            EnglishListTool(english_catalog, content_limit),
            EnglishFetchTool(english_catalog, content_max_fetch_chars),
            RiddlesListTool(riddles_catalog, content_limit),
            RiddlesFetchTool(riddles_catalog, content_max_fetch_chars),
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
