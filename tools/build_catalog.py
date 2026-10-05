#!/usr/bin/env python3
"""Offline builder for the local content catalogs.

Scans a source directory of children's Markdown files, parses them with the
same logic used at runtime, derives difficulty / recommended-age metadata,
optionally enriches summaries and themes through an LLM, copies the original
files into ``<out>/content/`` and writes ``<out>/catalog.json``.

LLM enrichment is optional: set ARK_API_KEY and ARK_MODEL to OpenAI-compatible
Chat Completions credentials (Volcengine Ark). Without them, deterministic
heuristics are used. Enrichment results are cached in ``<out>/`` so reruns do
not call the model twice.

Usage:
    python3 tools/build_catalog.py stories --src DIR --out DIR
    python3 tools/build_catalog.py english --src DIR --out DIR
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

# Load markdown.py directly: it is standalone stdlib code and importing the
# package would run the component's Home-Assistant-dependent __init__.
_md_path = ROOT / "custom_components/ark_web_search/content/markdown.py"
_spec = importlib.util.spec_from_file_location("reachy_content_markdown", _md_path)
markdown = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(markdown)
MAX_FILE_SIZE = markdown.MAX_FILE_SIZE
parse_sections = markdown.parse_sections
word_count = markdown.word_count

ARK_ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"

# -- readability ---------------------------------------------------------

_VOWEL_GROUPS = re.compile(r"[aeiouy]+")


def _syllables(word: str) -> int:
    word = word.lower()
    groups = _VOWEL_GROUPS.findall(word)
    count = len(groups) or 1
    if word.endswith("e") and count > 1 and not word.endswith(("le", "ee")):
        count -= 1
    return max(1, count)


def fk_grade(text: str) -> float:
    """Flesch-Kincaid grade level; 0 when no sentences can be counted."""
    words = re.findall(r"[A-Za-z']+", text)
    if not words:
        return 0.0
    sentences = max(1, len(re.findall(r"[.!?](?:\s|$)", text)))
    syllables = sum(_syllables(w) for w in words)
    return 0.39 * (len(words) / sentences) + 11.8 * (syllables / len(words)) - 15.59


# -- difficulty / age ----------------------------------------------------

GRADED_READER_LEVELS = {"VE": 1, "E": 2, "M": 3, "H": 4, "VH": 5}

# Stories difficulty 1-5 → recommended age (min, max).
STORIES_AGE: dict[int, tuple[int, int]] = {
    1: (4, 6),
    2: (5, 7),
    3: (6, 9),
    4: (8, 11),
    5: (10, 12),
}


def stories_difficulty(grade: float, words: int, explicit: str | None) -> int:
    if explicit:
        match = re.search(r"grade\s*(\d+)", explicit.lower())
        if match:
            return min(5, max(1, round(int(match.group(1)) / 2)))
    if words < 80 and grade < 3:
        grade = min(grade, 1.5)
    if grade < 1.0:
        return 1
    if grade < 2.2:
        return 2
    if grade < 4.0:
        return 3
    if grade < 6.0:
        return 4
    return 5


def english_difficulty(grade: float, explicit_code: str | None) -> int:
    if explicit_code:
        code = explicit_code.strip().upper()
        if code in GRADED_READER_LEVELS:
            return {1: 2, 2: 4, 3: 6, 4: 8, 5: 10}[GRADED_READER_LEVELS[code]]
        if code.isdigit():
            return {3: 5, 5: 8}.get(int(code), min(10, max(1, round(grade))))
    return min(10, max(1, round(grade + 0.5)))


def english_age(difficulty: int) -> tuple[int, int]:
    band = (difficulty - 1) // 2  # 0..4
    lo = min(10, 4 + band * 2)
    hi = 12 if band == 4 else lo + 2
    return lo, hi


# -- licenses ------------------------------------------------------------


def canonical_license(raw: str) -> str:
    text = raw.lower()
    if "public domain" in text:
        return "Public Domain"
    if "nc" in text.replace(" ", ""):
        return "CC BY-NC"
    if "4.0" in text:
        return "CC BY 4.0"
    if "cc" in text:
        return "CC BY"
    return raw.strip()


# -- heuristic enrichment ------------------------------------------------

TAXONOMY: list[tuple[str, list[str]]] = [
    (
        "animals",
        (
            "animal dog cat lion mouse bird bear fox rabbit elephant cow "
            "monkey horse chicken fish wolf goat duck owl ant frog snake bee crow "
            "parrot puppy squirrel tiger sheep pig hen donkey deer turtle whale"
        ),
    ),
    (
        "family",
        (
            "family mother father mum dad brother sister grandma grandpa "
            "parents grandmother grandfather parents"
        ),
    ),
    ("friendship", "friend friendship"),
    ("school", "school teacher class classroom student homework lesson"),
    ("kindness", "kind kindness help helpful caring"),
    ("sharing", "share sharing generous gift give"),
    ("courage", "brave courage afraid fear scary frightened"),
    ("honesty", "honest honesty lie truth"),
    (
        "nature",
        ("tree forest river mountain garden plant seed nature leaf flower grass"),
    ),
    ("food", "food eat cook bread fruit milk cake meal dinner market hungry"),
    ("adventure", "adventure journey travel trip explore"),
    ("magic", "magic witch wizard fairy spell magical giant"),
    ("music", "music sing song drum guitar"),
    ("sports", "football soccer ball game sport play"),
    ("water", "water sea ocean beach boat swim rain"),
    ("weather", "rain snow wind sun cloud weather"),
    ("bedtime", "bedtime sleep dream night goodnight"),
    ("royalty", "village town city king queen prince princess castle palace"),
    ("celebration", "christmas holiday festival birthday party"),
    ("art", "paint draw picture art color colour"),
]


def heuristic_tags(text: str, limit: int = 4) -> list[str] | None:
    lowered = re.findall(r"[a-z']+", text.lower())
    counts: dict[str, int] = {}
    for label, terms in TAXONOMY:
        terms_set = set(terms.split())
        counts[label] = sum(1 for word in lowered if word in terms_set)
    tags = [label for label, n in counts.items() if n > 0]
    tags.sort(key=lambda label: counts[label], reverse=True)
    return tags[:limit] or None


def heuristic_summary(sections: list[str]) -> str | None:
    for section in sections[:2]:
        sentences = re.split(r"(?<=[.!?])\s+", section)
        summary = ""
        for sentence in sentences:
            sentence = sentence.strip()
            if len(sentence) < 15 or "\n" in sentence and not summary:
                continue
            if not summary:
                summary = sentence
            elif len(summary) + len(sentence) <= 220:
                summary = f"{summary} {sentence}"
            if len(summary) >= 120:
                return summary[:220]
        if summary:
            return summary[:220]
    return None


# -- LLM enrichment ------------------------------------------------------

ENRICH_SYSTEM = (
    "You create catalog metadata for a children's reading item. Read the "
    "title and excerpt, then return strict JSON with four keys: 'summary' "
    "as one sentence of at most 30 words in the same language as the excerpt "
    "(English for English text, Simplified Chinese for Chinese text), "
    "describing only facts from the excerpt; 'tags' as an array of 4 to 8 "
    "short topic labels, each lowercase and without spaces, including both "
    "English labels and their Simplified Chinese equivalents, for example: "
    "animals, 动物, pets, 宠物, family, 家庭, friendship, 友谊, school, 学校; "
    "'age_min' and 'age_max' as integers giving the recommended reader age "
    "range in years. Use concrete subjects when present, for example dog, "
    "狗, lion, 狮子. If the excerpt is insufficient, use null for summary "
    "and an empty tags array, but always give a best-guess age range. Return "
    "JSON only."
)


def llm_enrich(item_id: str, title: str, text: str) -> dict[str, Any]:
    api_key = os.environ.get("ARK_API_KEY")
    model = os.environ.get("ARK_MODEL")
    if not api_key or not model:
        return {
            "summary": heuristic_summary_from(text),
            "tags": heuristic_tags(f"{title} {text}"),
            "age_min": None,
            "age_max": None,
            "source": "heuristic",
        }
    excerpt = text[:1200]
    payload = {
        "model": model,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": ENRICH_SYSTEM},
            {"role": "user", "content": f"Title: {title}\n\n{excerpt}"},
        ],
    }
    request = urllib.request.Request(
        ARK_ENDPOINT,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            data = json.load(response)
        content = data["choices"][0]["message"]["content"]
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise ValueError("no JSON in model response")
        result = json.loads(match.group(0))
        summary = result.get("summary")
        tags = result.get("tags")
        age_min = _safe_age(result.get("age_min"))
        age_max = _safe_age(result.get("age_max"))
        if not isinstance(summary, str) or not summary.strip():
            summary = heuristic_summary_from(text)
        if not isinstance(tags, list):
            tags = heuristic_tags(f"{title} {text}")
        else:
            tags = [str(t).lower().strip() for t in tags if str(t).strip()][:8]
        return {
            "summary": summary,
            "tags": tags or None,
            "age_min": age_min,
            "age_max": age_max,
            "source": "llm",
        }
    except (urllib.error.URLError, ValueError, KeyError, TimeoutError):
        return {
            "summary": heuristic_summary_from(text),
            "tags": heuristic_tags(f"{title} {text}"),
            "age_min": None,
            "age_max": None,
            "source": "heuristic",
        }


def heuristic_summary_from(text: str) -> str | None:
    sections = [block for block in re.split(r"\n\s*\n", text) if block.strip()]
    return heuristic_summary(sections)


# -- Chinese age / difficulty -------------------------------------------


def _safe_age(value: Any) -> int | None:
    """Clamp an LLM age value into 2-15; return None when not an int."""
    try:
        age = int(value)
    except (TypeError, ValueError):
        return None
    return min(15, max(2, age))


# Chinese school-stage labels used in source Difficulty headers.
_SCHOOL_STAGE_AGE = {
    "小学低年级": (6, 8),
    "小学中年级": (8, 10),
    "小学中高年级": (9, 12),
    "小学高年级": (10, 12),
}

# Series without any age marker.
_SERIES_AGE = {
    "伊索寓言": (4, 8),
}


def header_age_zh(header_difficulty: str | None, series: str | None):
    """Parse a Chinese source header into (age_min, age_max), or None."""
    if header_difficulty:
        match = re.search(r"(\d{1,2})\s*[-–]\s*(\d{1,2})\s*岁", header_difficulty)
        if match:
            return int(match.group(1)), int(match.group(2))
        for label, ages in _SCHOOL_STAGE_AGE.items():
            if label in header_difficulty:
                return ages
    if series:
        for label, ages in _SERIES_AGE.items():
            if label in series:
                return ages
    return None


def age_to_difficulty(age_min: int, age_max: int) -> int:
    """Map a recommended age range to the 1-5 story difficulty scale."""
    midpoint = (age_min + age_max) / 2
    if midpoint < 5.5:
        return 1
    if midpoint < 6.5:
        return 2
    if midpoint < 8.5:
        return 3
    if midpoint < 10.5:
        return 4
    return 5


# -- build ---------------------------------------------------------------


def build(
    kind: str, src: Path, out: Path, whitelist_path: Path | None = None
) -> dict[str, Any]:
    """Build one catalog and copy its content files."""
    if kind not in ("stories", "english"):
        raise ValueError(f"unknown kind: {kind}")

    whitelist: set[str] | None = None
    if whitelist_path is not None:
        whitelist_data = json.loads(whitelist_path.read_text(encoding="utf-8"))
        whitelist = set(whitelist_data.get("keep_ids", whitelist_data))

    out_content = out / "content"
    if out_content.exists():
        shutil.rmtree(out_content)
    out_content.mkdir(parents=True, exist_ok=True)

    cache_path = out / ".enrichment_cache.json"
    cache: dict[str, dict[str, Any]] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())

    raw_files = sorted(
        f
        for f in src.iterdir()
        if f.is_file() and f.name.endswith(".md") and f.name != "README.md"
    )
    not_whitelisted = 0
    if whitelist is not None:
        not_whitelisted = sum(path.stem not in whitelist for path in raw_files)
        raw_files = [path for path in raw_files if path.stem in whitelist]

    records: list[dict[str, Any]] = []
    parsed: dict[str, tuple[str, str, list[str]]] = {}
    skipped_size = 0
    skipped_empty: list[str] = []

    for path in raw_files:
        data_bytes = path.read_bytes()
        if len(data_bytes) > MAX_FILE_SIZE:
            skipped_size += 1
            continue
        raw = data_bytes.decode("utf-8")
        header, sections = parse_sections(raw)
        words = word_count(sections)
        if not sections or words == 0:
            skipped_empty.append(path.name)
            continue
        item_id = path.name[:-3]
        text = "\n\n".join(sections)
        title = header.get("title") or path.stem
        parsed[item_id] = (title, text, sections)
        records.append(
            {
                "id": item_id,
                "title": title,
                "source": header.get("source"),
                "license": canonical_license(header.get("license") or ""),
                "language": _detect_language(header, text),
                "word_count": words,
                "section_count": len(sections),
                "relative_path": f"content/{path.name}",
                "fk_grade": round(fk_grade(text), 2),
                "explicit_difficulty": header.get("difficulty"),
                "series": header.get("series"),
            }
        )

    # Enrichment (LLM when configured, with persistent cache).
    def enrich_one(record: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        item_id = record["id"]
        if item_id in cache:
            return item_id, cache[item_id]
        title, text, _ = parsed[item_id]
        return item_id, llm_enrich(item_id, title, text)

    def _save_cache() -> None:
        tmp = cache_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
        tmp.replace(cache_path)

    futures = []
    with ThreadPoolExecutor(
        max_workers=int(os.environ.get("ENRICH_WORKERS", "6"))
    ) as pool:
        for record in records:
            if record["id"] not in cache:
                futures.append(pool.submit(enrich_one, record))
        for future in as_completed(futures):
            item_id, enrichment = future.result()
            cache[item_id] = enrichment
            _save_cache()

    items: list[dict[str, Any]] = []
    for record in records:
        enrichment = cache[record["id"]]
        explicit = record.pop("explicit_difficulty")
        fk = record.pop("fk_grade")
        series = record.pop("series")
        if kind == "stories":
            ages = None
            if enrichment.get("age_min") and enrichment.get("age_max"):
                ages = (enrichment["age_min"], enrichment["age_max"])
            if ages is None and record["language"] == "zh":
                ages = header_age_zh(explicit, series)
            if ages is None:
                # English stories (and any unresolved Chinese item): readability.
                difficulty = stories_difficulty(fk, record["word_count"], explicit)
                ages = STORIES_AGE[difficulty]
            age_min, age_max = ages
            difficulty = age_to_difficulty(age_min, age_max)
            item = {
                "id": record["id"],
                "title": record["title"],
                "source": record["source"],
                "license": record["license"],
                "language": record["language"],
                "difficulty": difficulty,
                "recommended_age_min": age_min,
                "recommended_age_max": age_max,
                "themes": enrichment.get("tags"),
                "summary": enrichment.get("summary"),
                "word_count": record["word_count"],
                "section_count": record["section_count"],
                "relative_path": record["relative_path"],
            }
        else:
            difficulty = english_difficulty(fk, explicit)
            age_min, age_max = english_age(difficulty)
            level = _english_level(explicit)
            item = {
                "id": record["id"],
                "title": record["title"],
                "source": record["source"],
                "license": record["license"],
                "language": record["language"],
                "level": level,
                "difficulty": difficulty,
                "recommended_age_min": age_min,
                "recommended_age_max": age_max,
                "topics": enrichment.get("tags"),
                "summary": enrichment.get("summary"),
                "word_count": record["word_count"],
                "section_count": record["section_count"],
                "relative_path": record["relative_path"],
            }
        items.append(item)

    items.sort(key=lambda item: item["id"])
    catalog = {
        "version": 1,
        "kind": kind,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "item_count": len(items),
        "items": items,
    }
    (out / "catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2))

    # Copy the original Markdown files unchanged.
    for item in items:
        name = Path(item["relative_path"]).name
        shutil.copy2(src / name, out_content / name)

    active_ids = {record["id"] for record in records}
    llm_count = sum(
        1
        for key, value in cache.items()
        if key in active_ids and value.get("source") == "llm"
    )
    agent_count = sum(
        1
        for key, value in cache.items()
        if key in active_ids and value.get("source") == "agent"
    )
    return {
        "items": len(items),
        "not_whitelisted": not_whitelisted,
        "skipped_too_large": skipped_size,
        "skipped_empty": len(skipped_empty),
        "llm_enriched": llm_count,
        "agent_enriched": agent_count,
        "empty_files": skipped_empty,
    }


def _detect_language(header: dict[str, str], text: str) -> str:
    header_language = (header.get("language") or "").strip().lower()
    if header_language and "中文" in header_language:
        return "zh"
    match = re.search(r"language:\s*([a-z]{2,3})", text, re.IGNORECASE)
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text[:3000]))
    if cjk > 50 and not match:
        return "zh"
    if header_language and re.fullmatch(r"[a-z]{2,3}", header_language):
        return header_language
    return match.group(1).lower() if match else "en"


def _english_level(explicit: str | None) -> str | None:
    if not explicit:
        return None
    code = explicit.strip()
    if code.upper() in GRADED_READER_LEVELS or code.isdigit():
        return code.upper()
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["stories", "english"])
    parser.add_argument("--src", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--whitelist",
        type=Path,
        help="JSON list (or keep_ids object) produced by editorial review",
    )
    args = parser.parse_args()
    stats = build(args.kind, args.src, args.out, args.whitelist)
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
