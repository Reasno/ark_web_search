"""Thin async client for Volcengine Ark (Doubao) Web Search Custom API."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    API_URL,
    MAX_SUMMARY_CHARS,
    MAX_TOTAL_CHARS,
)

_LOGGER = logging.getLogger(__name__)


class ArkSearchError(Exception):
    """Raised when the Ark Search API call fails."""


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


async def async_search(
    hass: HomeAssistant,
    api_key: str,
    query: str,
    *,
    count: int = 5,
    time_range: str | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    """Run a web search and return a compact, model-friendly dict."""
    query = (query or "").strip()
    if not query:
        raise ArkSearchError("Empty search query")
    # API accepts 1~100 chars, longer gets truncated server-side anyway.
    query = query[:100]

    payload: dict[str, Any] = {
        "Query": query,
        "SearchType": "web",
        "Count": count,
        "ContentFormats": "text",
    }
    if time_range:
        payload["TimeRange"] = time_range

    session = async_get_clientsession(hass)
    try:
        async with session.post(
            API_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise ArkSearchError(
                    f"Ark Search HTTP {resp.status}: {body[:300]}"
                )
            data = await resp.json(content_type=None)
    except aiohttp.ClientError as err:
        raise ArkSearchError(f"Ark Search request failed: {err}") from err
    except TimeoutError as err:
        raise ArkSearchError("Ark Search request timed out") from err

    meta = data.get("ResponseMetadata") or {}
    if meta.get("Error"):
        raise ArkSearchError(f"Ark Search error: {meta['Error']}")

    result = data.get("Result") or {}
    web_results = result.get("WebResults") or []

    items: list[dict[str, Any]] = []
    total = 0
    for idx, item in enumerate(web_results, start=1):
        summary = _truncate(
            item.get("Summary") or item.get("Snippet") or "", MAX_SUMMARY_CHARS
        )
        if not summary:
            continue
        entry = {
            "rank": idx,
            "title": (item.get("Title") or "").strip(),
            "site": (item.get("SiteName") or "").strip(),
            "url": item.get("Url") or "",
            "published": item.get("PublishTime") or "",
            "authority": item.get("AuthInfoDes") or "",
            "summary": summary,
        }
        cost = len(summary) + len(entry["title"])
        if total + cost > MAX_TOTAL_CHARS and items:
            break
        total += cost
        items.append(entry)

    _LOGGER.debug(
        "Ark search '%s' -> %d results (%d chars)", query, len(items), total
    )
    return {"query": query, "result_count": len(items), "results": items}
