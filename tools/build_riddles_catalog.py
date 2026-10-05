#!/usr/bin/env python3
"""Build the local Reachy Mini riddle catalog from downloaded datasets.

Input directory files:
- brainteasers_chinese.json (BrainBench)
- BT_riddle.xlsx, character_riddle.xlsx, number_riddle.xlsx (MDRiddle)
- CC-Riddle.jsonl (CC-Riddle)

Outputs ``content/*.md`` and ``catalog.json``. Answers only exist in content
files, never in catalog metadata, so riddles_list cannot reveal them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

BLOCKED = {
    "色情",
    "做爱",
    "性交",
    "强奸",
    "迷奸",
    "猥亵",
    "卖淫",
    "嫖",
    "妓女",
    "避孕套",
    "精子",
    "射精",
    "手淫",
    "生殖器",
    "下体",
    "乳房",
    "月经",
    "一夜情",
    "开房",
    "处女",
    "情妇",
    "情人",
    "出轨",
    "离婚",
    "怀孕",
    "孕妇",
    "老婆",
    "老公",
    "自杀",
    "跳楼",
    "上吊",
    "割腕",
    "碎尸",
    "分尸",
    "尸体",
    "杀人",
    "流血",
    "吸毒",
    "喝酒",
    "白酒",
    "抽烟",
    "赌博",
    "赌场",
    "内裤",
    "屁股",
    "胸部",
    "厕所",
    "屎",
    "尿",
    "恐怖袭击",
    "共产党",
}


def clean(value: Any) -> str:
    text = str(value or "").strip()
    if text.lower() == "nan":
        return ""
    return re.sub(r"\s+", " ", text)


def normalized(text: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text.lower())


def safe(*parts: str) -> bool:
    text = " ".join(parts)
    return bool(text.strip()) and not any(term in text for term in BLOCKED)


def stable_id(prefix: str, question: str) -> str:
    digest = hashlib.sha1(normalized(question).encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def read_xlsx(path: Path) -> Iterable[dict[str, Any]]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    sheet = workbook.active
    rows = sheet.iter_rows(values_only=True)
    headers = [clean(value) for value in next(rows)]
    for values in rows:
        row = dict(zip(headers, values))
        question = clean(row.get("riddle"))
        try:
            label = int(row.get("label"))
        except (TypeError, ValueError):
            continue
        options = [clean(row.get(f"choice{i}")) for i in range(5)]
        if not 0 <= label < len(options) or not question or not options[label]:
            continue
        yield {"question": question, "answer": options[label], "options": options}


def load_records(source: Path) -> Iterable[dict[str, Any]]:
    brainbench = json.loads((source / "brainteasers_chinese.json").read_text("utf-8"))
    for row in brainbench:
        yield {
            "prefix": "brainbench",
            "question": clean(row.get("question")),
            "answer": clean(row.get("answer")),
            "options": [],
            "category": "逻辑脑筋急转弯",
            "kind": "brainteaser",
            "source": "BrainBench",
            "difficulty": 4,
            "age": (8, 12),
            "topics": ["脑筋急转弯", "逻辑", clean(row.get("category"))],
        }

    for filename, prefix, category, kind, difficulty, age in (
        ("BT_riddle.xlsx", "mdr-bt", "冷笑话脑筋急转弯", "cold_joke", 2, (6, 12)),
        ("character_riddle.xlsx", "mdr-char", "字谜", "character_riddle", 3, (7, 12)),
        ("number_riddle.xlsx", "mdr-number", "数字谜", "number_riddle", 3, (7, 12)),
    ):
        for row in read_xlsx(source / filename):
            yield {
                "prefix": prefix,
                **row,
                "category": category,
                "kind": kind,
                "source": "MDRiddle",
                "difficulty": difficulty,
                "age": age,
                "topics": [category, kind],
            }

    with (source / "CC-Riddle.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            yield {
                "prefix": "cc-char",
                "question": clean(row.get("question")),
                "answer": clean(row.get("answer")),
                "options": [],
                "category": "字谜",
                "kind": "character_riddle",
                "source": "CC-Riddle",
                "difficulty": 4,
                "age": (8, 12),
                "topics": ["字谜", "汉字", "文字游戏"],
            }


def markdown(record: dict[str, Any]) -> str:
    options = record.get("options") or []
    parts = [
        f"# {record['question']}",
        "",
        f"- Source: {record['source']}",
        f"- Category: {record['category']}",
        "",
        "---",
        "",
        "## 谜面",
        record["question"],
    ]
    if options:
        parts.extend(["", "## 选项", *[f"- {item}" for item in options]])
    parts.extend(["", "## 答案", record["answer"], ""])
    return "\n".join(parts)


def build(
    source: Path, output: Path, whitelist_path: Path | None = None
) -> dict[str, Any]:
    whitelist: set[str] | None = None
    if whitelist_path is not None:
        whitelist_data = json.loads(whitelist_path.read_text(encoding="utf-8"))
        whitelist = set(whitelist_data.get("keep_ids", whitelist_data))

    content = output / "content"
    if content.exists():
        shutil.rmtree(content)
    content.mkdir(parents=True)

    seen_questions: set[str] = set()
    items: list[dict[str, Any]] = []
    source_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    blocked = 0
    duplicates = 0
    not_whitelisted = 0

    for record in load_records(source):
        question = record["question"]
        answer = record["answer"]
        options = record.get("options") or []
        key = normalized(re.sub(r"（.*?）", "", question))
        # Very short stems in MDRiddle are usually damaged labels rather than
        # playable questions (for example a bare two-character term).
        if len(key) < 4 or key in seen_questions:
            duplicates += 1
            continue
        if not safe(question, answer, *options):
            blocked += 1
            continue
        item_id = stable_id(record["prefix"], question)
        if whitelist is not None and item_id not in whitelist:
            not_whitelisted += 1
            continue
        seen_questions.add(key)
        filename = f"{item_id}.md"
        (content / filename).write_text(markdown(record), encoding="utf-8")
        lo, hi = record["age"]
        topics = [topic for topic in record["topics"] if topic]
        items.append(
            {
                "id": item_id,
                "title": question,
                "summary": f"一则{record['category']}，答案需在用户作答后揭晓。",
                "source": record["source"],
                "language": "zh",
                "category": record["category"],
                "riddle_kind": record["kind"],
                "difficulty": record["difficulty"],
                "recommended_age_min": lo,
                "recommended_age_max": hi,
                "topics": topics,
                "word_count": len(question),
                "section_count": 3 if options else 2,
                "relative_path": f"content/{filename}",
            }
        )
        source_counts[record["source"]] = source_counts.get(record["source"], 0) + 1
        category_counts[record["category"]] = (
            category_counts.get(record["category"], 0) + 1
        )

    # Mix sources/categories deterministically so an unfiltered first page is varied.
    items.sort(key=lambda item: hashlib.sha1(item["id"].encode()).hexdigest())
    catalog = {"version": 1, "kind": "riddles", "items": items}
    (output / "catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "items": len(items),
        "content_files": len(list(content.glob("*.md"))),
        "duplicates_removed": duplicates,
        "blocked_removed": blocked,
        "not_whitelisted": not_whitelisted,
        "sources": source_counts,
        "categories": category_counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--whitelist",
        type=Path,
        help="JSON list (or keep_ids object) produced by editorial review",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            build(args.src, args.out, args.whitelist), ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
