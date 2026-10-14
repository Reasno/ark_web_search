"""Small speech-oriented tool contracts over a shared content service."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.util.json import JsonObjectType

from .service import ContentService


def _session_parameter() -> dict:
    return {
        vol.Optional("session_id", description="Client-managed conversation ID; reuse across requests."): vol.All(
            str, vol.Length(min=1, max=128)
        ),
    }


def _start_parameters(*, english: bool = False, riddle: bool = False) -> vol.Schema:
    parameters = {
        **_session_parameter(),
        vol.Optional("topic", description="Short topic or title; omit if unspecified."): vol.All(str, vol.Length(max=100)),
        vol.Optional("age"): vol.All(int, vol.Range(min=1, max=15)),
        vol.Optional("difficulty"): vol.In(["easy", "medium", "hard"]),
    }
    if english:
        parameters[vol.Optional("grade", description="Chinese primary-school grade; takes priority if difficulty conflicts.")] = vol.All(int, vol.Range(min=1, max=6))
        parameters[vol.Optional("reading_id", description="Saved English material reference; takes priority over filters.")] = vol.All(
            str, vol.Length(min=1, max=200)
        )
    if riddle:
        parameters[vol.Optional("kind")] = vol.In(["brainteaser", "cold_joke", "number_riddle"])
    return vol.Schema(parameters)


class ContentTool(llm.Tool):
    def __init__(self, service: ContentService) -> None:
        self.service = service

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        return await hass.async_add_executor_job(
            self.service.call, self.name, dict(tool_input.tool_args),
            llm_context.context.user_id or "",
        )


class RiddleNextTool(ContentTool):
    name = "riddle_next"
    description = (
        "Use for a new riddle, brain teaser or cold joke, including '猜个谜/再来一个'. "
        "Returns one complete question and optional choices, without its answer. "
        "No filters required. Ask the question, then STOP and wait for a guess. "
        "Do not call riddle_answer in the question turn. If topic_matched=false, "
        "explain briefly that this is a different topic."
    )
    parameters = _start_parameters(riddle=True)


class RiddleAnswerTool(ContentTool):
    name = "riddle_answer"
    description = (
        "Reveal the current riddle's answer ONLY after the user guesses, gives up "
        "or asks for the answer. id may be omitted for the current session's riddle. "
        "Otherwise copy the id from riddle_next. Compare the guess and explain briefly."
    )
    parameters = vol.Schema({
        **_session_parameter(),
        vol.Optional("id"): vol.All(str, vol.Length(min=1, max=24)),
    })


class StoryStartTool(ContentTool):
    name = "story_start"
    description = (
        "Use for a new children's story, including '讲个故事'. Returns one story's "
        "first passage immediately; no filters required. Narrate the text faithfully "
        "in Chinese, translating if needed. Preserve plot and dialogue; omit Markdown. "
        "If topic_matched=false, briefly identify the different topic. "
        "When next_cursor is present, STOP and wait for the user to request more."
    )
    parameters = _start_parameters()


class StoryContinueTool(ContentTool):
    name = "story_continue"
    description = (
        "Continue a story ONLY when the user requests more. Copy next_cursor from "
        "the previous story result into cursor. Narrate only this passage faithfully "
        "in Chinese. Stop at its end; next_cursor=null means the story is complete. "
        "Repeating the same cursor rereads the same passage."
    )
    parameters = vol.Schema({**_session_parameter(), vol.Required("cursor"): str})


class EnglishStartTool(ContentTool):
    name = "english_start"
    description = (
        "Use for English reading practice, including '练英语'. Returns one short "
        "passage immediately; no filters required. Read the English, then invite "
        "practice. To revisit saved material, pass its reading_id; this starts at "
        "the beginning. Save returned reading_id and level as learning progress when appropriate. "
        "For the current reading use english_continue. Invite "
        "repetition (practice=repeat/read_aloud) or ask one comprehension question. "
        "Explain unfamiliar words briefly in Chinese. Judge pronunciation only from "
        "actual audio evidence. If topic_matched=false, identify the different topic. "
        "If difficulty_matched=false, explain that the passage follows the requested grade. "
        "STOP and wait for the learner before continuing."
    )
    parameters = _start_parameters(english=True)


class EnglishContinueTool(ContentTool):
    name = "english_continue"
    description = (
        "Get the next short English passage when the learner is ready. Copy "
        "next_cursor from the previous English result into cursor. Read it and invite "
        "practice, then wait. next_cursor=null means the reading is complete. "
        "Repeating the same cursor rereads the same passage."
    )
    parameters = vol.Schema({**_session_parameter(), vol.Required("cursor"): str})
