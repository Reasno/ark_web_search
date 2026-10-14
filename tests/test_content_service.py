"""Executable without Home Assistant: python -m unittest discover -s tests."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components/ark_web_search"))

from content.catalog import ContentCatalog
from content.service import ContentService, chunk_text, parse_riddle


class ContentServiceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 10
        self.catalogs = {}
        self.canonical = ("这是一个完整的故事。The cat runs home! " * 100) + "结尾。"
        for kind in ("stories", "english", "riddles"):
            root = Path(self.directory.name) / kind
            root.mkdir()
            items = []
            for index in range(1, 11):
                text = self.canonical
                if kind == "riddles":
                    text = f"## 谜面\n问题{index}？\n\n## 选项\n- 甲\n- 乙\n\n## 答案\n秘密{index}"
                (root / f"{index}.md").write_text(f"# Title {index}\n\n---\n\n{text}", encoding="utf-8")
                items.append({
                    "id": str(index), "title": f"Title {index}", "relative_path": f"{index}.md",
                    "difficulty": index, "topics": ["动物"], "recommended_age_min": 6,
                    "recommended_age_max": 10, "riddle_kind": "brain_teaser",
                })
            (root / "catalog.json").write_text(json.dumps({"version": 1, "items": items}))
            self.catalogs[kind] = ContentCatalog.load(str(root), kind)
        self.service = ContentService(self.catalogs, clock=lambda: self.now, choose=lambda items: items[0])

    def call(self, action, session="a", principal="user", **args):
        if session is not None:
            args["session_id"] = session
        return self.service.call(action, args, principal)

    def test_riddle_question_options_answer_and_normalized_kind(self):
        first = self.call("riddle_next", kind="brainteaser")
        self.assertEqual(first["options"], ["甲", "乙"])
        self.assertNotIn("秘密", json.dumps(first, ensure_ascii=False))
        self.assertEqual(self.call("riddle_answer")["answer"], "秘密2")
        self.assertEqual(self.call("riddle_answer", id=first["id"]), self.call("riddle_answer"))

    def test_scopes_and_explicit_ids_cannot_cross_users_or_sessions(self):
        first = self.call("riddle_next")
        second = self.call("riddle_next", session="b", difficulty="medium")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.call("riddle_answer", session="b")["answer"], "秘密3")
        for kwargs in ({"session": "b"}, {"principal": "other"}, {"session": None}):
            self.assertEqual(self.call("riddle_answer", id=first["id"], **kwargs)["error_code"], "invalid_reference")
        unscoped = self.call("riddle_next", session=None)
        self.assertIn("answer", self.call("riddle_answer", session=None, id=unscoped["id"]))
        self.assertIn("error_code", self.call("riddle_answer", session=None))
        self.assertIn("error_code", self.call("riddle_next", principal=""))

    def test_continuation_is_immutable_and_lossless_even_after_other_starts(self):
        for kind in ("story", "english"):
            first = self.call(f"{kind}_start")
            cursor = first["next_cursor"]
            self.call(f"{kind}_start")
            second = self.call(f"{kind}_continue", cursor=cursor)
            self.assertEqual(second, self.call(f"{kind}_continue", cursor=cursor))
            self.assertIn("error_code", self.call(f"{kind}_continue", session="b", cursor=cursor))
            wrong = "english" if kind == "story" else "story"
            self.assertIn("error_code", self.call(f"{wrong}_continue", cursor=cursor))
            collected = first["text"]
            while cursor:
                page = self.call(f"{kind}_continue", cursor=cursor)
                collected += page["text"]
                self.assertLessEqual(len(page["text"]), 700 if kind == "story" else 240)
                cursor = page["next_cursor"]
            self.assertEqual(collected, self.canonical)

    def test_invalid_expired_and_evicted_references_do_not_advance(self):
        first = self.call("story_start")
        cursor = first["next_cursor"]
        expected = self.call("story_continue", cursor=cursor)
        for bad in ("", "../../file", cursor.split(":")[0] + ":999999"):
            self.assertIn("error_code", self.call("story_continue", cursor=bad))
        self.assertEqual(expected, self.call("story_continue", cursor=cursor))
        self.now += 1801
        self.assertIn("error_code", self.call("story_continue", cursor=cursor))
        self.service.max_readings = 1
        first = self.call("riddle_next")
        self.call("riddle_next")
        self.assertIn("error_code", self.call("riddle_answer", id=first["id"]))
        self.assertEqual(len(self.service.readings), 1)

    def test_selection_dedup_filters_and_explicit_topic_mismatch(self):
        first = self.call("riddle_next", difficulty="easy")
        second = self.call("riddle_next", difficulty="easy")
        self.assertNotEqual(first["question"], second["question"])
        self.assertTrue(self.call("english_start", topic="小动物")["topic_matched"])
        self.assertFalse(self.call("english_start", topic="不存在的主题")["topic_matched"])
        self.assertFalse(self.call("english_start", topic="!!!")["topic_matched"])
        self.assertNotIn("topic_matched", self.call("english_start", topic=" "))
        self.assertTrue(self.call("english_start", age=8)["age_matched"])
        self.assertFalse(self.call("english_start", age=15)["age_matched"])
        sixth = self.call("english_start", grade=6)
        self.assertIn(sixth["title"], {"Title 9", "Title 10"})
        self.assertEqual(sixth["practice"], "comprehension")
        for grade in range(1, 7):
            for difficulty in ("easy", "medium", "hard"):
                result = self.call("english_start", grade=grade, difficulty=difficulty)
                self.assertIn("text", result)
        conflict = self.call("english_start", grade=6, difficulty="easy", topic="Title 10")
        self.assertFalse(conflict["difficulty_matched"])
        self.assertEqual(conflict["level"], 10)
        self.assertTrue(conflict["topic_matched"])

    def test_concurrent_starts_have_unique_refs_and_do_not_repeat_in_available_pool(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.call("riddle_next"), range(8)))
        self.assertEqual(len({r["id"] for r in results}), 8)
        self.assertEqual(len({r["question"] for r in results}), 8)
        for result in results:
            self.assertIn("answer", self.call("riddle_answer", id=result["id"]))

    def test_saved_english_reference_survives_service_restart_and_accepts_catalog_id(self):
        first = self.call("english_start", grade=6)
        self.service = ContentService(self.catalogs)
        resumed = self.call("english_start", reading_id=first["reading_id"], grade=1, topic="unrelated")
        self.assertEqual(resumed["reading_id"], first["reading_id"])
        self.assertEqual(resumed["text"], first["text"])
        self.assertEqual(resumed["level"], first["level"])
        self.assertEqual(self.call("english_start", reading_id="10")["title"], "Title 10")
        self.assertEqual(self.call("english_start", reading_id="missing")["error_code"], "no_match")

    def test_failures_do_not_leak_paths_or_spoil_questions(self):
        self.service.catalogs["stories"] = None
        self.assertEqual(self.call("story_start")["error_code"], "library_unavailable")
        with self.assertRaises(Exception):
            parse_riddle(["question without labels", "answer"])
        for path in (Path(self.directory.name) / "riddles").glob("*.md"):
            path.write_text("# title\n\n---\n\nunlabelled secret")
        result = self.call("riddle_next")
        self.assertEqual(result["error_code"], "read_failed")
        self.assertNotIn(self.directory.name, json.dumps(result))
        self.assertNotIn("secret", json.dumps(result))

    def test_chunking_preserves_every_character_and_limits(self):
        for text in ("x" * 4001, "甲" * 4001, "one two.\n" * 400, "a" * 49 + "。b " * 100):
            for limit in (1, 50, 240, 700):
                chunks = chunk_text(text, limit)
                self.assertEqual("".join(chunks), text)
                self.assertTrue(all(0 < len(c) <= limit for c in chunks))


if __name__ == "__main__":
    unittest.main()
