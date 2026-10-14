"""Session-scoped selection and immutable reading positions, without HA dependencies."""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
import random
import re
import secrets
import threading
import time
from typing import Any

from .catalog import ContentCatalog, CatalogError

KINDS = {"riddle": "riddles", "story": "stories", "english": "english"}
BANDS = {
    "riddle": {"easy": (1, 2), "medium": (3, 3), "hard": (4, 5)},
    "story": {"easy": (1, 1), "medium": (2, 2), "hard": (3, 5)},
    "english": {"easy": (1, 3), "medium": (4, 6), "hard": (7, 10)},
}
GRADE_BANDS = ((1, 2), (2, 4), (3, 5), (5, 7), (7, 9), (9, 10))
RIDDLE_KINDS = {
    "brainteaser": "brainteaser", "brain_teaser": "brainteaser",
    "逻辑脑筋急转弯": "brainteaser", "cold_joke": "cold_joke",
    "冷笑话脑筋急转弯": "cold_joke", "number_riddle": "number_riddle", "数字谜": "number_riddle",
}


def error(code: str, message: str) -> dict[str, Any]:
    return {"error_code": code, "message": message}


def chunk_text(text: str, limit: int) -> tuple[str, ...]:
    """Split at sentence/line/word boundaries; joining chunks recovers all text."""
    chunks = []
    while len(text) > limit:
        window = text[:limit]
        boundaries = list(re.finditer(r'[。！？.!?][”"\']?(?:\s|$)|\n', window))
        end = next((m.end() for m in reversed(boundaries) if m.end() >= limit // 2), 0)
        if not end:
            end = window.rfind(" ", limit // 2)
            end = end + 1 if end >= 0 else limit
        chunks.append(text[:end])
        text = text[end:]
    if text:
        chunks.append(text)
    return tuple(chunks)


def parse_riddle(sections: list[str]) -> dict[str, Any]:
    """Only explicitly labelled question and options may enter the first result."""
    fields: dict[str, list[str]] = {}
    current = None
    labels = {"谜面": "question", "选项": "options", "答案": "answer", "解析": "explanation"}
    for section in sections:
        if section in labels:
            current = labels[section]
            fields.setdefault(current, [])
        elif current:
            fields[current].append(section)
    question = "\n\n".join(fields.get("question", [])).strip()
    answer = "\n\n".join(fields.get("answer", [])).strip()
    if not question or not answer:
        raise CatalogError("riddle requires labelled question and answer")
    result: dict[str, Any] = {"question": question, "answer": answer}
    options = [
        re.sub(r"^[-*]\s+", "", line).strip()
        for section in fields.get("options", []) for line in section.splitlines() if line.strip()
    ]
    if options:
        result["options"] = options
    if fields.get("explanation"):
        result["explanation"] = "\n\n".join(fields["explanation"])
    return result


@dataclass
class Reading:
    owner: tuple[str, str]
    kind: str
    expires: float
    title: str
    chunks: tuple[str, ...] = ()
    riddle: dict[str, Any] = field(default_factory=dict)


@dataclass
class Session:
    expires: float
    seen: dict[str, deque] = field(default_factory=dict)
    riddle_id: str | None = None


class ContentService:
    """One bounded store per integration; callers run in the HA executor."""

    def __init__(
        self, catalogs: dict[str, ContentCatalog | None], *, max_chars: int = 12000,
        ttl: float = 1800, max_readings: int = 128, max_sessions: int = 256,
        clock=time.monotonic, choose=random.choice,
    ) -> None:
        self.catalogs = catalogs
        self.max_chars = max_chars
        self.ttl = ttl
        self.max_readings = max_readings
        self.max_sessions = max_sessions
        self.clock, self.choose = clock, choose
        self.readings: OrderedDict[str, Reading] = OrderedDict()
        self.sessions: OrderedDict[tuple[str, str], Session] = OrderedDict()
        self.lock = threading.Lock()

    def call(self, action: str, args: dict[str, Any], principal: str) -> dict[str, Any]:
        """Authenticate ownership, expire state and execute atomically."""
        if not principal:
            return error("missing_identity", "An authenticated user is required.")
        args = dict(args)
        session_id = args.pop("session_id", "") or ""
        owner = (principal, session_id)
        with self.lock:
            now = self.clock()
            for store in (self.readings, self.sessions):
                for key in list(store):
                    if store[key].expires <= now:
                        del store[key]
            if action == "riddle_answer":
                return self._answer(args.get("id"), owner)
            if action in {"story_continue", "english_continue"}:
                return self._continue(action.split("_")[0], args.get("cursor"), owner)
            if action not in {"riddle_next", "story_start", "english_start"}:
                return error("invalid_parameters", "Unknown content action.")
            kind = action.split("_")[0]
            return self._start(kind, args, owner, now)

    @staticmethod
    def _age_distance(item: dict, age: int | None) -> int:
        if age is None:
            return 0
        lo, hi = item.get("recommended_age_min"), item.get("recommended_age_max")
        if lo is None or hi is None:
            return 100
        return max(lo - age, age - hi, 0)

    @staticmethod
    def _topic_score(item: dict, topic: str) -> int:
        phrase = topic.strip().lower()[:100]
        terms = re.findall(r"[a-z0-9']+|[\u4e00-\u9fff]+", phrase)
        if not terms:
            return 0
        title = (item.get("title") or "").lower()
        tags = [str(t).lower() for t in (item.get("themes") or item.get("topics") or [])]
        summary = (item.get("summary") or "").lower()
        return sum(
            6 * (term in title) + 3 * any(
                term in tag or (len(tag) >= 2 and tag in term) for tag in tags
            ) + (term in summary)
            for term in terms
        )

    def _start(self, kind: str, args: dict, owner: tuple[str, str], now: float) -> dict:
        catalog = self.catalogs.get(KINDS[kind])
        if catalog is None or not catalog.available:
            return error("library_unavailable", "This content library is unavailable.")
        candidates = list(catalog.items)
        difficulty = args.get("difficulty")
        if difficulty:
            lo, hi = BANDS[kind][difficulty]
            candidates = [i for i in candidates if lo <= (i.get("difficulty") or 0) <= hi]
        if kind == "english" and args.get("grade"):
            lo, hi = GRADE_BANDS[args["grade"] - 1]
            candidates = [i for i in candidates if lo <= (i.get("difficulty") or 0) <= hi]
        if kind == "riddle" and args.get("kind"):
            candidates = [i for i in candidates if
                          RIDDLE_KINDS.get(i.get("riddle_kind") or i.get("category")) == args["kind"]]
        if not candidates:
            return error("no_match", "No item fits these filters. Relax difficulty, grade or kind.")
        topic = args.get("topic", "").strip()
        topic_matched = None
        if topic:
            scores = {i["id"]: self._topic_score(i, topic) for i in candidates}
            matched = [i for i in candidates if scores[i["id"]] > 0]
            topic_matched = bool(matched)
            if matched:
                best = max(scores[i["id"]] for i in matched)
                candidates = [i for i in matched if scores[i["id"]] == best]

        session = self.sessions.get(owner) if owner[1] else None
        seen = session.seen.get(kind, ()) if session else ()
        fresh = [i for i in candidates if i["id"] not in seen]
        if fresh:
            candidates = fresh
        best_age = min(self._age_distance(i, args.get("age")) for i in candidates)
        candidates = [i for i in candidates if self._age_distance(i, args.get("age")) == best_age]
        # Unspecified reading level begins with approachable material.
        if not difficulty and not args.get("grade"):
            distance = min(abs((i.get("difficulty") or 2) - 2) for i in candidates)
            candidates = [i for i in candidates if abs((i.get("difficulty") or 2) - 2) == distance]
        item = self.choose(candidates)
        try:
            sections, _ = catalog.read_sections(item, 0, 1_048_576)
            if not sections:
                raise CatalogError("empty content")
            reading = Reading(owner, kind, now + self.ttl, item.get("title") or "")
            if kind == "riddle":
                reading.riddle = parse_riddle(sections)
                if sum(len(str(v)) for v in reading.riddle.values()) > self.max_chars:
                    raise CatalogError("riddle too long")
            else:
                limit = min(self.max_chars, 700 if kind == "story" else 240)
                reading.chunks = chunk_text("\n\n".join(sections), max(1, limit))
        except (CatalogError, OSError):
            return error("read_failed", "The selected material could not be read. Request another item.")

        content_id = secrets.token_hex(12)
        self.readings[content_id] = reading
        while len(self.readings) > self.max_readings:
            self.readings.popitem(last=False)
        if owner[1]:
            if session is None:
                session = Session(now + self.ttl)
                self.sessions[owner] = session
            self.sessions.move_to_end(owner)
            session.expires = now + self.ttl
            session.seen.setdefault(kind, deque(maxlen=64)).append(item["id"])
            if kind == "riddle":
                session.riddle_id = content_id
            while len(self.sessions) > self.max_sessions:
                self.sessions.popitem(last=False)
        if kind == "riddle":
            result = {"id": content_id, "question": reading.riddle["question"]}
            if "options" in reading.riddle:
                result["options"] = reading.riddle["options"]
        else:
            result = self._page(content_id, reading, 0)
            result["title"] = reading.title
            if kind == "english":
                level = item.get("difficulty") or 2
                result["practice"] = "repeat" if level <= 3 else "read_aloud" if level <= 6 else "comprehension"
        if topic_matched is not None:
            result["topic_matched"] = topic_matched
        if args.get("age") is not None:
            result["age_matched"] = best_age == 0
        return result

    def _lookup(self, content_id: str | None, kind: str, owner: tuple[str, str]) -> Reading | None:
        reading = self.readings.get(content_id)
        if reading and reading.kind == kind and reading.owner == owner:
            return reading
        return None

    def _answer(self, content_id: str | None, owner: tuple[str, str]) -> dict:
        if not content_id and owner[1]:
            session = self.sessions.get(owner)
            content_id = session.riddle_id if session else None
        reading = self._lookup(content_id, "riddle", owner)
        if reading is None:
            return error("invalid_reference", "No active riddle with this ID in this session. Ask a new riddle.")
        result = {"answer": reading.riddle["answer"]}
        if "explanation" in reading.riddle:
            result["explanation"] = reading.riddle["explanation"]
        return result

    def _continue(self, kind: str, cursor: str | None, owner: tuple[str, str]) -> dict:
        match = re.fullmatch(r"([0-9a-f]{24}):([0-9]{1,6})", cursor or "")
        if match:
            content_id, index = match[1], int(match[2])
            reading = self._lookup(content_id, kind, owner)
            if reading and 0 <= index < len(reading.chunks):
                return self._page(content_id, reading, index)
        return error("invalid_reference", "Cursor is invalid or expired for this session. Start a new reading.")

    @staticmethod
    def _page(content_id: str, reading: Reading, index: int) -> dict:
        more = index + 1 < len(reading.chunks)
        return {
            "text": reading.chunks[index],
            "next_cursor": f"{content_id}:{index + 1}" if more else None,
        }
