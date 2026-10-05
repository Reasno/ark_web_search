"""LLM tools backed by the local stories, English and riddles catalogs.

Tool contracts (names/parameters/response fields) follow the Reachy Mini
content tools design: list tools return metadata only; fetch tools return
sectioned content with pagination. All inputs are validated here and all
catalog exceptions are converted to structured responses; filesystem paths
are never exposed outside this module's log messages.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.util.json import JsonObjectType

from .catalog import (
    MAX_LIST_LIMIT,
    MAX_SECTION_COUNT,
    CatalogError,
    ContentCatalog,
    ContentReadError,
)

_LOGGER = logging.getLogger(__name__)

ERR_UNAVAILABLE = "library_unavailable"
ERR_NOT_FOUND = "not_found"
ERR_INVALID = "invalid_parameters"
ERR_READ = "read_failed"


def _error(code: str, message: str) -> JsonObjectType:
    return {"error_code": code, "message": message}


def _age_text(item: dict[str, Any], catalog: ContentCatalog) -> str | None:
    return catalog.recommended_age(item)


def _build_coaching(item: dict[str, Any]) -> dict[str, Any]:
    """Build deterministic coaching guidance without calling any model."""
    difficulty = item.get("difficulty") or 5
    if difficulty <= 3:
        mode = "repeat_after_me"
        instructions = [
            "Read one short sentence at a time and ask the child to repeat it.",
            "Praise the attempt before correcting anything.",
            "Correct at most one pronunciation issue per sentence.",
            "Explain unfamiliar words in simple Chinese only when needed.",
        ]
    elif difficulty <= 6:
        mode = "read_aloud"
        instructions = [
            "Read one short section at a time.",
            "Ask the child to repeat difficult sentences.",
            "Explain unfamiliar words in Chinese only when needed.",
        ]
    else:
        mode = "guided_reading"
        instructions = [
            "Read one section, then ask one simple comprehension question.",
            "Re-read sentences the child does not understand.",
            "Explain key vocabulary in simple Chinese; keep the practice in English.",
        ]
    coaching: dict[str, Any] = {
        "suggested_mode": mode,
        "instructions": instructions,
    }
    focus_words = item.get("focus_words")
    if isinstance(focus_words, list) and focus_words:
        coaching["focus_words"] = focus_words[:10]
    return coaching


def _limit_value(value: Any, default: int) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(limit, 1), MAX_LIST_LIMIT)


class _BaseListTool(llm.Tool):
    """Common list-tool logic."""

    def __init__(self, catalog: ContentCatalog | None, default_limit: int) -> None:
        self._catalog = catalog
        self._default_limit = default_limit

    def _result(self, kwargs: dict[str, Any]) -> JsonObjectType:
        catalog = self._catalog
        if catalog is None:
            return _error(
                ERR_UNAVAILABLE,
                "The local content library is not available right now.",
            )
        limit = _limit_value(kwargs.get("limit"), self._default_limit)
        # Drop None values and the raw "limit" (it is normalised and passed
        # explicitly, otherwise list_items gets it twice).
        kwargs = {k: v for k, v in kwargs.items() if v is not None and k != "limit"}
        try:
            return catalog.list_items(limit=limit, **kwargs)
        except CatalogError as err:
            _LOGGER.warning("%s failed: %s", self.name, err)
            return _error(
                ERR_INVALID,
                "The filters could not be applied; adjust them and try again.",
            )

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        return await hass.async_add_executor_job(
            self._result, dict(tool_input.tool_args)
        )


class StoriesListTool(_BaseListTool):
    """List metadata from the local story library."""

    name = "stories_list"
    description = (
        "Search and list metadata from the local children's story library. "
        "Use this before stories_fetch when selecting a story. Filters by "
        "keyword, target age, and difficulty (1-5). It does not return the "
        "full text."
    )
    parameters = vol.Schema(
        {
            vol.Optional("query"): str,
            vol.Optional("age"): vol.All(vol.Coerce(int), vol.Range(min=1, max=15)),
            vol.Optional("difficulty"): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=5)
            ),
            vol.Optional("language"): str,
            vol.Optional("limit"): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=MAX_LIST_LIMIT)
            ),
            vol.Optional("cursor"): str,
        }
    )


class EnglishListTool(_BaseListTool):
    """List metadata from the local English reading library."""

    name = "english_list"
    description = (
        "Search and list metadata from the local graded English reading "
        "library. Use this before english_fetch when selecting material. "
        "Filters by keyword, target age, Chinese primary-school grade (1-6), "
        "reading level code, difficulty (1-10), and maximum word count. It "
        "does not return the full text."
    )
    parameters = vol.Schema(
        {
            vol.Optional("query"): str,
            vol.Optional("age"): vol.All(vol.Coerce(int), vol.Range(min=1, max=15)),
            vol.Optional("grade"): vol.All(vol.Coerce(int), vol.Range(min=1, max=6)),
            vol.Optional("level"): str,
            vol.Optional("difficulty"): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=10)
            ),
            vol.Optional("max_words"): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=20000)
            ),
            vol.Optional("limit"): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=MAX_LIST_LIMIT)
            ),
            vol.Optional("cursor"): str,
        }
    )


class RiddlesListTool(_BaseListTool):
    """List riddle questions without exposing answers."""

    name = "riddles_list"
    description = (
        "Select kid-friendly brain teasers, number riddles, and cold jokes "
        "from the editorially reviewed local library. The response contains "
        "questions but never answers. Present one question and wait for the "
        "user to guess; do not call riddles_fetch yet."
    )
    parameters = vol.Schema(
        {
            vol.Optional("query"): str,
            vol.Optional("age"): vol.All(vol.Coerce(int), vol.Range(min=1, max=15)),
            vol.Optional("difficulty"): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=5)
            ),
            vol.Optional("category"): vol.In(
                [
                    "逻辑脑筋急转弯",
                    "冷笑话脑筋急转弯",
                    "数字谜",
                    "brainteaser",
                    "cold_joke",
                    "number_riddle",
                ]
            ),
            vol.Optional("limit"): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=MAX_LIST_LIMIT)
            ),
            vol.Optional("cursor"): str,
        }
    )


class _BaseFetchTool(llm.Tool):
    """Common fetch-tool logic."""

    def __init__(self, catalog: ContentCatalog | None, max_fetch_chars: int) -> None:
        self._catalog = catalog
        self._max_fetch_chars = max_fetch_chars

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        return await hass.async_add_executor_job(
            self._result, dict(tool_input.tool_args)
        )

    @staticmethod
    def _int(value: Any, default: int, lo: int, hi: int) -> int:
        if value is None:
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return min(max(parsed, lo), hi)

    def _fetch(
        self, item: dict[str, Any], section_start: int, section_count: int
    ) -> tuple[list[str], int]:
        catalog = self._catalog
        assert catalog is not None
        return catalog.read_sections(item, section_start, section_count)


class StoriesFetchTool(_BaseFetchTool):
    """Fetch one story's content in sections."""

    name = "stories_fetch"
    description = (
        "Get the text of one story from the local library. Call only with an "
        "ID returned by stories_list. Long stories are split into sections; "
        "use section_start and section_count to page through them when "
        "has_more is true."
    )
    parameters = vol.Schema(
        {
            vol.Required("id"): str,
            vol.Optional("section_start"): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=10000)
            ),
            vol.Optional("section_count"): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=MAX_SECTION_COUNT)
            ),
        }
    )

    def _result(self, kwargs: dict[str, Any]) -> JsonObjectType:
        if self._catalog is None:
            return _error(
                ERR_UNAVAILABLE,
                "The local content library is not available right now.",
            )
        item_id = str(kwargs.get("id") or "").strip()
        if not item_id:
            return _error(
                ERR_INVALID,
                "An item ID from stories_list is required.",
            )
        section_start = self._int(kwargs.get("section_start"), 0, 0, 10000)
        section_count = self._int(kwargs.get("section_count"), 12, 1, MAX_SECTION_COUNT)
        item = self._catalog.get_item(item_id)
        if item is None:
            return _error(
                ERR_NOT_FOUND,
                "No story with that ID. Use stories_list to get valid IDs.",
            )
        try:
            sections, total = self._fetch(item, section_start, section_count)
        except ContentReadError as err:
            _LOGGER.warning("stories_fetch read failure: %s", err)
            return _error(ERR_READ, "That story could not be read. Try another one.")
        except CatalogError as err:
            _LOGGER.warning("stories_fetch failed: %s", err)
            return _error(ERR_READ, "That story could not be read.")

        content, used = self._fit(sections)
        end = section_start + used
        has_more = end < total
        return {
            "id": item["id"],
            "title": item.get("title"),
            "metadata": {
                "difficulty": item.get("difficulty"),
                "recommended_age": self._catalog.recommended_age(item),
                "themes": item.get("themes"),
            },
            "content": content,
            "section_start": section_start,
            "section_count": used,
            "has_more": has_more,
            "next_section_start": end if has_more else None,
        }

    def _fit(self, sections: list[str]) -> tuple[str, int]:
        used = len(sections)
        while used > 1 and len("\n\n".join(sections[:used])) > self._max_fetch_chars:
            used -= 1
        content = "\n\n".join(sections[:used])
        if len(content) > self._max_fetch_chars:
            content = content[: self._max_fetch_chars].rstrip()
        return content, used


class RiddlesFetchTool(_BaseFetchTool):
    """Reveal one riddle answer after the user has attempted it."""

    name = "riddles_fetch"
    description = (
        "Fetch the answer and explanation for one riddle ID returned by "
        "riddles_list. Never call this when first asking the riddle. Call it "
        "only after the user has guessed, or explicitly says they do not know, "
        "give up, or asks for the answer."
    )
    parameters = vol.Schema({vol.Required("id"): str})

    def _result(self, kwargs: dict[str, Any]) -> JsonObjectType:
        if self._catalog is None:
            return _error(
                ERR_UNAVAILABLE,
                "The local riddles library is not available right now.",
            )
        item_id = str(kwargs.get("id") or "").strip()
        if not item_id:
            return _error(
                ERR_INVALID,
                "A riddle ID from riddles_list is required.",
            )
        item = self._catalog.get_item(item_id)
        if item is None:
            return _error(
                ERR_NOT_FOUND,
                "No riddle with that ID. Use riddles_list to get a valid ID.",
            )
        try:
            sections, _ = self._fetch(item, 0, MAX_SECTION_COUNT)
        except ContentReadError as err:
            _LOGGER.warning("riddles_fetch read failure: %s", err)
            return _error(ERR_READ, "That riddle could not be read.")
        except CatalogError as err:
            _LOGGER.warning("riddles_fetch failed: %s", err)
            return _error(ERR_READ, "That riddle could not be read.")
        return {
            "id": item["id"],
            "question": item.get("title"),
            "category": item.get("category"),
            "content": "\n\n".join(sections),
        }


class EnglishFetchTool(_BaseFetchTool):
    """Fetch one English item's content plus optional coaching info."""

    name = "english_fetch"
    description = (
        "Get the text of one graded English reading from the local library, "
        "with optional structured coaching guidance. Call only with an ID "
        "returned by english_list. Long items are split into sections; use "
        "section_start and section_count to page through them when has_more "
        "is true."
    )
    parameters = vol.Schema(
        {
            vol.Required("id"): str,
            vol.Optional("section_start"): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=10000)
            ),
            vol.Optional("section_count"): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=MAX_SECTION_COUNT)
            ),
            vol.Optional("include_coaching"): bool,
        }
    )

    def _result(self, kwargs: dict[str, Any]) -> JsonObjectType:
        if self._catalog is None:
            return _error(
                ERR_UNAVAILABLE,
                "The local content library is not available right now.",
            )
        item_id = str(kwargs.get("id") or "").strip()
        if not item_id:
            return _error(
                ERR_INVALID,
                "An item ID from english_list is required.",
            )
        section_start = self._int(kwargs.get("section_start"), 0, 0, 10000)
        section_count = self._int(kwargs.get("section_count"), 12, 1, MAX_SECTION_COUNT)
        include_coaching = kwargs.get("include_coaching", True)
        if include_coaching is None:
            include_coaching = True

        item = self._catalog.get_item(item_id)
        if item is None:
            return _error(
                ERR_NOT_FOUND,
                "No English reading with that ID. Use english_list to get valid IDs.",
            )
        try:
            sections, total = self._fetch(item, section_start, section_count)
        except ContentReadError as err:
            _LOGGER.warning("english_fetch read failure: %s", err)
            return _error(ERR_READ, "That reading could not be read. Try another one.")
        except CatalogError as err:
            _LOGGER.warning("english_fetch failed: %s", err)
            return _error(ERR_READ, "That reading could not be read.")

        content, used = self._fit(sections)
        end = section_start + used
        has_more = end < total
        response: JsonObjectType = {
            "id": item["id"],
            "title": item.get("title"),
            "level": item.get("level"),
            "content": content,
            "section_start": section_start,
            "section_count": used,
            "has_more": has_more,
            "next_section_start": end if has_more else None,
        }
        if include_coaching:
            response["coaching"] = _build_coaching(item)
        return response

    def _fit(self, sections: list[str]) -> tuple[str, int]:
        used = len(sections)
        while used > 1 and len("\n\n".join(sections[:used])) > self._max_fetch_chars:
            used -= 1
        content = "\n\n".join(sections[:used])
        if len(content) > self._max_fetch_chars:
            content = content[: self._max_fetch_chars].rstrip()
        return content, used
