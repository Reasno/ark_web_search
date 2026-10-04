"""Safe Markdown parsing for the local children's content libraries.

Pure standard-library code with no Home Assistant dependencies so that the
offline catalog build tool can reuse the exact same sectioning logic as the
runtime tools (section_count in catalog.json must match fetch results).
"""
from __future__ import annotations

import re

# Maximum accepted size of a single content file (bytes).
MAX_FILE_SIZE = 1_048_576

_H1 = re.compile(r"^#\s+(.+?)\s*#*$")
_ATX_HEADING = re.compile(r"^(#{2,6})\s+(.+?)\s*#*$")
_BARE_HEADING_MARK = re.compile(r"^#{1,3}\s*$")
_HRULE = re.compile(r"^(-{3,}|\*{3,}|_{3,})\s*$")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_HEADER_BULLET = re.compile(r"^-\s+([A-Za-z][A-Za-z ]*?):\s*(.*?)\s*$")
_ATTRIBUTION = re.compile(
    r"^\s*\*{1,2}\s*"
    r"(License|Text|Illustration|Illustrations|Translated By|Translation|"
    r"Language|Adapted|Adapted From|Based on)\s*:"
)
_PG_START = re.compile(r"^\*\*\*\s*START OF (THE|THIS) PROJECT GUTENBERG")
_PG_END = re.compile(r"^\*\*\*\s*END OF (THE|THIS) PROJECT GUTENBERG")
_NUMBERED = re.compile(r"^\d{1,3}[.)]\s+\S")
_ALL_CAPS_WORDS = re.compile(r"^[A-Z0-9'\u2019 ,\-:?!&]+$")
_WORD = re.compile(r"[A-Za-z\u2019']+")
_CJK_CHAR = re.compile(r"[\u4e00-\u9fff]")


def _clean_inline(line: str) -> str:
    """Replace images with their alt text and tidy whitespace."""
    def repl(match: re.Match[str]) -> str:
        alt = match.group(1).strip()
        # Template placeholders are not real captions.
        if not alt or "{{" in alt:
            return ""
        return alt

    line = _IMAGE.sub(repl, line)
    line = re.sub(r"\s+", " ", line)
    return line.strip()


def split_header(raw: str) -> tuple[dict[str, str], list[str]]:
    """Split a content file into header fields and remaining raw lines.

    The header consists of an optional H1 title, ``- Key: value`` bullet
    lines, and an optional YAML-ish front-matter block (bounded by ``---``
    lines). Project Gutenberg START/END markers are also honoured.
    """
    lines = raw.splitlines()
    fields: dict[str, str] = {}
    i = 0
    n = len(lines)

    while i < n and not lines[i].strip():
        i += 1
    if i < n:
        m = _H1.match(lines[i])
        if m:
            fields["title"] = m.group(1).strip()
            i += 1

    # Header bullet fields, terminated by the first horizontal rule.
    rule_seen = False
    while i < n:
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if _HRULE.match(line):
            rule_seen = True
            i += 1
            break
        m = _HEADER_BULLET.match(lines[i])
        if m:
            fields[m.group(1).strip().lower()] = m.group(2).strip()
            i += 1
            continue
        # Body content started without a rule (defensive).
        break

    if rule_seen:
        # Skip an optional YAML front-matter block ("---\nkey: value\n---").
        j = i
        while j < n and not lines[j].strip():
            j += 1
        if j < n and _HRULE.match(lines[j].strip()):
            i = j + 1
            while i < n and not _HRULE.match(lines[i].strip()):
                i += 1
            if i < n:
                i += 1

    body = lines[i:]

    # Project Gutenberg: keep only the text between the standard markers.
    start_idx = next(
        (k for k, line in enumerate(body) if _PG_START.match(line)), None
    )
    if start_idx is not None:
        body = body[start_idx + 1 :]
    end_idx = next(
        (k for k, line in enumerate(body) if _PG_END.match(line)), None
    )
    if end_idx is not None:
        body = body[:end_idx]

    return fields, body


def _is_graded_heading(line: str, following: str | None) -> bool:
    """Title-case heading immediately followed by a numbered list (graded readers)."""
    if following is None or not _NUMBERED.match(following):
        return False
    if len(line) > 50 or line.endswith((".", ",", ";", ":")):
        return False
    words = line.split()
    if len(words) < 2:
        return False
    letters = [c for c in line if c.isalpha()]
    if len(letters) < 3:
        return False
    capitalized = sum(1 for w in words if w[:1].isupper())
    return capitalized >= len(words) - 1


def _is_allcaps_heading(line: str) -> bool:
    """Standalone ALL-CAPS heading line (Project Gutenberg tale titles)."""
    if len(line) > 55 or len(line) < 3:
        return False
    letters = [c for c in line if c.isalpha()]
    if len(letters) < 3 or any(c.islower() for c in letters):
        return False
    if not _ALL_CAPS_WORDS.match(line):
        return False
    digits = sum(c.isdigit() for c in line)
    return digits <= 2


def parse_sections(raw: str) -> tuple[dict[str, str], list[str]]:
    """Parse content text into ordered sections.

    Returns ``(header_fields, sections)``. Section boundaries are:

    - bare ``##`` markers used by the Global ASP / StoryWeaver sources;
    - ATX headings (``##`` through ``######``);
    - title-case headings followed by a numbered sentence list (graded
      readers);
    - standalone ALL-CAPS headings (Project Gutenberg tale titles);
    - horizontal rules.

    Image markdown is replaced by its caption text and trailing attribution
    bullets are removed. Empty sections are dropped.
    """
    fields, body = split_header(raw)

    sections: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        text = "\n".join(buffer)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if text:
            sections.append(text)
        buffer.clear()

    # Drop a repeated H1 title at the start of the body, including a plain
    # (non-ATX) title line as used by the graded readers.
    idx = 0
    while idx < len(body) and not body[idx].strip():
        idx += 1
    title = fields.get("title")

    def _norm_title(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", text.lower())

    if idx < len(body):
        line = body[idx].strip()
        matched = bool(_H1.match(line)) or (
            title is not None and _norm_title(line) == _norm_title(title)
        )
        if matched:
            idx += 1

    total = len(body)
    while idx < total:
        line = body[idx]
        stripped = line.strip()

        if not stripped:
            if buffer and buffer[-1] != "":
                buffer.append("")
            idx += 1
            continue

        if _BARE_HEADING_MARK.match(stripped) or (
            _HRULE.match(stripped) and not stripped.startswith("#")
        ):
            flush()
            idx += 1
            continue

        m = _ATX_HEADING.match(stripped)
        if m:
            flush()
            cleaned = _clean_inline(m.group(2))
            if cleaned:
                sections.append(cleaned)
            idx += 1
            continue

        if _ATTRIBUTION.match(line):
            idx += 1
            continue

        is_heading = False
        nxt = None
        for k in range(idx + 1, total):
            if body[k].strip():
                nxt = body[k].strip()
                break
        # Graded-reader headings are boundaries whenever a numbered list
        # follows. ALL-CAPS headings (Project Gutenberg tale titles) are
        # boundaries when blank-separated from the surrounding text, so a
        # repeated title line at the start cannot block the whole file.
        blank_before = not buffer or buffer[-1] == ""
        blank_after = idx + 1 >= total or not body[idx + 1].strip()
        is_heading = _is_graded_heading(stripped, nxt) or (
            blank_before and blank_after and _is_allcaps_heading(stripped)
        )
        if is_heading:
            flush()
            buffer.append(stripped)
            idx += 1
            continue

        cleaned = _clean_inline(line)
        if cleaned:
            buffer.append(cleaned)
        idx += 1

    flush()
    return fields, sections


def word_count(sections: list[str]) -> int:
    """Count words in parsed sections.

    Latin words count as one each; each CJK character counts as one word.
    """
    total = 0
    for section in sections:
        total += len(_WORD.findall(section))
        total += len(_CJK_CHAR.findall(section))
    return total
