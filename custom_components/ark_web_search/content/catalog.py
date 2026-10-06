"""Loading, validation and querying of local content catalogs.

Each content directory (``stories`` / ``english``) contains a generated
``catalog.json`` plus a ``content/`` subdirectory with the original Markdown
files. Catalogs are loaded once at integration setup (in an executor thread)
and kept in memory; list tools never scan the filesystem.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
from typing import Any

from .markdown import MAX_FILE_SIZE, parse_sections

_LOGGER = logging.getLogger(__name__)

CATALOG_VERSION = 1
ID_PATTERN = re.compile(r"^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff._'\-]*$")

STORIES = "stories"
ENGLISH = "english"
RIDDLES = "riddles"

DEFAULT_LIST_LIMIT = 8
MAX_LIST_LIMIT = 20
MAX_SECTION_COUNT = 30


class CatalogError(Exception):
    """Base class for catalog problems."""


class CatalogUnavailable(CatalogError):
    """The catalog directory or catalog.json is missing/unreadable."""


class ItemNotFound(CatalogError):
    """No catalog item matches the requested ID."""


class ContentReadError(CatalogError):
    """The content file cannot be read or decoded."""


def encode_cursor(offset: int) -> str:
    """Encode a pagination offset into an opaque cursor string."""
    import base64

    raw = json.dumps({"o": offset}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    """Decode a pagination cursor; invalid cursors start at offset 0."""
    if not cursor:
        return 0
    import base64

    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + padding)
        data = json.loads(raw)
        offset = int(data["o"])
    except (ValueError, TypeError, KeyError):
        return 0
    return max(offset, 0)


def _resolve_inside(base_dir: str, relative_path: str) -> str:
    """Resolve relative_path and ensure it stays inside base_dir."""
    base = os.path.realpath(base_dir)
    target = os.path.realpath(os.path.join(base, relative_path))
    if target != base and not target.startswith(base + os.sep):
        raise CatalogError(f"path escapes content directory: {relative_path}")
    return target


class ContentCatalog:
    """In-memory view of one content catalog."""

    def __init__(self, kind: str, base_dir: str, items: list[dict[str, Any]]):
        self.kind = kind
        self.base_dir = os.path.realpath(base_dir)
        self.items = items
        self._by_id = {item["id"]: item for item in items}

    # -- loading ---------------------------------------------------------

    @classmethod
    def load(cls, base_dir: str, kind: str) -> ContentCatalog:
        """Load and validate ``catalog.json`` from a content directory.

        Runs in an executor thread; only stdlib/blocking calls are used.
        """
        catalog_path = os.path.join(base_dir, "catalog.json")
        try:
            with open(catalog_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError as err:
            raise CatalogUnavailable(f"catalog missing: {catalog_path}") from err
        except (OSError, json.JSONDecodeError) as err:
            raise CatalogUnavailable(f"catalog unreadable: {catalog_path}") from err

        if not isinstance(data, dict) or data.get("version") != CATALOG_VERSION:
            raise CatalogUnavailable(f"unsupported catalog format: {catalog_path}")
        raw_items = data.get("items")
        if not isinstance(raw_items, list):
            raise CatalogUnavailable(f"catalog has no items: {catalog_path}")

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            item_id = raw.get("id")
            relative_path = raw.get("relative_path")
            if (
                not isinstance(item_id, str)
                or not ID_PATTERN.match(item_id)
                or item_id in seen
                or not isinstance(relative_path, str)
                or relative_path.startswith("/")
                or ".." in relative_path.split("/")
            ):
                _LOGGER.warning("Skipping invalid catalog entry in %s", kind)
                continue
            try:
                resolved = _resolve_inside(base_dir, relative_path)
            except CatalogError:
                _LOGGER.warning("Skipping %s item with unsafe path", item_id)
                continue
            item = dict(raw)
            item["_resolved_path"] = resolved
            items.append(item)
            seen.add(item_id)
            if not os.path.isfile(resolved):
                _LOGGER.warning("Content file missing for %s item %s", kind, item_id)
            elif os.path.getsize(resolved) > MAX_FILE_SIZE:
                _LOGGER.warning("Content file too large for %s item %s", kind, item_id)

        catalog = cls(kind, base_dir, items)
        _LOGGER.info("Loaded %s catalog with %d items", kind, len(items))
        return catalog

    # -- helpers ---------------------------------------------------------

    @property
    def available(self) -> bool:
        return bool(self.items)

    def get_item(self, item_id: str) -> dict[str, Any] | None:
        return self._by_id.get(item_id)

    @staticmethod
    def recommended_age(item: dict[str, Any]) -> str | None:
        lo = item.get("recommended_age_min")
        hi = item.get("recommended_age_max")
        if lo is None and hi is None:
            return None
        return f"{lo}-{hi}"

    # -- listing ---------------------------------------------------------

    @staticmethod
    def _haystacks(item: dict[str, Any]) -> list[tuple[str, int]]:
        tags = item.get("themes") or item.get("topics") or []
        return [
            ((item.get("title") or "").lower(), 6),
            (" ".join(tags).lower(), 3),
            ((item.get("summary") or "").lower(), 1),
        ]

    @staticmethod
    def _term_hits(term: str, text: str) -> bool:
        """Return whether one query term matches a haystack text.

        Handles English substring/word matches and Chinese queries, where a
        CJK label inside the text (e.g. ``动物``) should match a longer query
        term (e.g. ``小动物``), and vice versa.
        """
        if term in text:
            return True
        if re.search(r"[\u4e00-\u9fff]", term):
            for run in re.findall(r"[\u4e00-\u9fff]{1,8}", text):
                if run in term or term in run:
                    return True
        return False

    @staticmethod
    def _keyword_score(item: dict[str, Any], terms: list[str], phrase: str) -> int:
        score = 0
        for text, weight in ContentCatalog._haystacks(item):
            if phrase and (
                phrase in text
                or (
                    re.search(r"[\u4e00-\u9fff]", phrase)
                    and any(
                        run in phrase or phrase in run
                        for run in re.findall(r"[\u4e00-\u9fff]{1,8}", text)
                    )
                )
            ):
                score += 8 * weight
            for term in terms:
                if ContentCatalog._term_hits(term, text):
                    score += weight
                    # Exact word-match bonus for English terms.
                    if re.fullmatch(r"[a-z0-9']+", term) and re.search(
                        rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text
                    ):
                        score += weight
        return score

    def list_items(
        self,
        *,
        query: str | None = None,
        age: int | None = None,
        difficulty: int | None = None,
        language: str | None = None,
        category: str | None = None,
        grade: int | None = None,
        level: str | None = None,
        max_words: int | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        cursor: str | None = None,
        randomize: bool = False,
    ) -> dict[str, Any]:
        """Filter, sort and paginate catalog items (metadata only)."""
        terms: list[str] = []
        phrase = ""
        if query:
            phrase = query.strip().lower()[:100]
            terms = [t for t in re.split(r"[^a-z0-9\u4e00-\u9fff]+", phrase) if t]

        # Grade is expressed as a difficulty band (english only).
        grade_band: tuple[int, int] | None = None
        if grade is not None and self.kind == ENGLISH:
            grade_band = (max(1, 2 * grade - 1), min(10, 2 * grade))

        candidates = list(self.items)
        if difficulty is not None:
            candidates = [c for c in candidates if c.get("difficulty") == difficulty]
        if language:
            candidates = [
                c for c in candidates if (c.get("language") or "").lower() == language
            ]
        if category:
            candidates = [
                c
                for c in candidates
                if (c.get("category") or "").lower() == category.lower()
                or (c.get("riddle_kind") or "").lower() == category.lower()
            ]
        if level:
            candidates = [
                c for c in candidates if (c.get("level") or "").lower() == level.lower()
            ]
        if max_words is not None:
            candidates = [
                c for c in candidates if (c.get("word_count") or 0) <= max_words
            ]
        if grade_band is not None:
            lo, hi = grade_band
            candidates = [
                c for c in candidates if lo <= (c.get("difficulty") or 0) <= hi
            ]

        scored: list[tuple[tuple[int, int, int, int, str], dict[str, Any]]] = []
        for item in candidates:
            if terms:
                kw = self._keyword_score(item, terms, phrase)
                if kw == 0:
                    continue
            else:
                kw = 0
            age_fit = 0
            if age is not None:
                lo = item.get("recommended_age_min")
                hi = item.get("recommended_age_max")
                if lo is not None and hi is not None:
                    if age < lo:
                        age_fit = lo - age
                    elif age > hi:
                        age_fit = age - hi
                else:
                    age_fit = 8
            diff_fit = abs((item.get("difficulty") or 0) - (difficulty or 0))
            sort_key = (
                -kw,
                age_fit,
                diff_fit,
                item.get("word_count") or 0,
                (item.get("title") or "").lower(),
            )
            scored.append((sort_key, item))

        scored.sort(key=lambda pair: pair[0])
        ordered = [item for _, item in scored]
        if randomize:
            random.shuffle(ordered)

        offset = decode_cursor(cursor)
        page = ordered[offset : offset + limit]
        next_cursor = None
        if offset + limit < len(ordered):
            next_cursor = encode_cursor(offset + limit)
        return {
            "items": [self._list_entry(item) for item in page],
            "next_cursor": next_cursor,
        }

    def _list_entry(self, item: dict[str, Any]) -> dict[str, Any]:
        """Shape one item for a list response."""
        if self.kind == STORIES:
            return {
                "id": item["id"],
                "title": item.get("title"),
                "difficulty": item.get("difficulty"),
                "recommended_age": self.recommended_age(item),
                "themes": item.get("themes"),
                "summary": item.get("summary"),
                "word_count": item.get("word_count"),
            }
        if self.kind == RIDDLES:
            # The title is the riddle question. Never include answer content in
            # a list response; the model may fetch it only after the user guesses.
            return {
                "id": item["id"],
                "question": item.get("title"),
                "category": item.get("category"),
                "riddle_kind": item.get("riddle_kind"),
                "difficulty": item.get("difficulty"),
                "recommended_age": self.recommended_age(item),
                "topics": item.get("topics"),
            }
        return {
            "id": item["id"],
            "title": item.get("title"),
            "level": item.get("level"),
            "difficulty": item.get("difficulty"),
            "recommended_age": self.recommended_age(item),
            "topics": item.get("topics"),
            "summary": item.get("summary"),
            "word_count": item.get("word_count"),
        }

    # -- fetching --------------------------------------------------------

    def read_sections(
        self, item: dict[str, Any], section_start: int, section_count: int
    ) -> tuple[list[str], int]:
        """Read and parse one content file, returning (sections slice, total)."""
        # Re-validate at read time.
        resolved = _resolve_inside(self.base_dir, item["relative_path"])
        try:
            size = os.path.getsize(resolved)
        except OSError as err:
            raise ContentReadError("content file disappeared") from err
        if size > MAX_FILE_SIZE:
            raise ContentReadError("content file too large")
        try:
            with open(resolved, "rb") as fh:
                raw_bytes = fh.read()
            raw = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as err:
            raise ContentReadError("content is not valid UTF-8") from err
        except OSError as err:
            raise ContentReadError("content file cannot be read") from err

        _, sections = parse_sections(raw)
        total = len(sections)
        return sections[section_start : section_start + section_count], total
