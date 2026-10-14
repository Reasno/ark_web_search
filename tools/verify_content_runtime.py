"""Validate HA schemas and all configured material in an isolated process."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components/ark_web_search"))

from content.catalog import ContentCatalog
from content.service import ContentService, chunk_text, parse_riddle
from content.tools import (
    RiddleNextTool, RiddleAnswerTool, StoryStartTool, StoryContinueTool,
    EnglishStartTool, EnglishContinueTool,
)
import voluptuous as vol
from voluptuous_openapi import convert


async def main(root: Path):
    catalogs = {kind: ContentCatalog.load(str(root / kind), kind) for kind in ("riddles", "stories", "english")}
    counts = {}
    for kind, catalog in catalogs.items():
        for item in catalog.items:
            sections, _ = catalog.read_sections(item, 0, 1_048_576)
            assert sections, (kind, item["id"], "empty")
            if kind == "riddles":
                parse_riddle(sections)
            else:
                text = "\n\n".join(sections)
                chunks = chunk_text(text, 700 if kind == "stories" else 240)
                assert "".join(chunks) == text, (kind, item["id"], "content lost")
        counts[kind] = len(catalog.items)
    service = ContentService(catalogs)
    for grade in range(1, 7):
        for difficulty in ("easy", "medium", "hard"):
            result = service.call("english_start", {"grade": grade, "difficulty": difficulty}, "preflight")
            assert not result.get("error_code"), (grade, difficulty, result)

    class Hass:
        async def async_add_executor_job(self, func, *args):
            return await asyncio.to_thread(func, *args)

    context = SimpleNamespace(context=SimpleNamespace(user_id="preflight"))
    schemas = {}
    for cls in (RiddleNextTool, RiddleAnswerTool, StoryStartTool, StoryContinueTool, EnglishStartTool, EnglishContinueTool):
        tool = cls(service)
        schema = convert(tool.parameters)
        json.dumps(schema)
        schemas[tool.name] = schema
        for key in ("age", "grade"):
            if key in schema["properties"]:
                assert schema["properties"][key]["type"] == "integer", schema
                try:
                    tool.parameters({key: "six"})
                except vol.Invalid:
                    pass
                else:
                    raise AssertionError("invalid numeric argument accepted")
        if tool.name.endswith(("_start", "_next")):
            args = tool.parameters({"session_id": "preflight"})
            result = await tool.async_call(Hass(), SimpleNamespace(tool_args=args), context)
            assert not result.get("error_code"), (tool.name, result)
    print(json.dumps({"catalogs_verified": counts, "tool_schemas": schemas}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/config/media/reachy_content"))
    asyncio.run(main(parser.parse_args().root))
