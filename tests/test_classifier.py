"""Unit tests for the request classifier."""

import json
import os
import socket
import tempfile
import unittest
from unittest import mock

import classifier


def _llm_cfg_file(extra):
    """Write a classification.json with method=llm and return its path."""
    base = {"enabled": True, "method": "llm"}
    base.update(extra)
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(base, f)
    f.close()
    return f.name


def _raw(content):
    """Build an OpenAI /chat/completions response body string."""
    return json.dumps({"choices": [{"message": {"content": content}}]})


class TestLoadConfig(unittest.TestCase):
    def test_loads_minimal_config(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"enabled": True}, f)
            path = f.name
        try:
            cfg = classifier.load_config(path)
            self.assertTrue(cfg["enabled"])
            self.assertEqual(cfg["default"], "CODING")
            self.assertEqual(cfg["min_confidence"], 1.0)
            self.assertEqual(cfg["max_scan_bytes_per_message"], 65536)
            self.assertEqual(cfg["budget_warn_ms"], 1000)
            self.assertIn("code_fence", cfg["weights"])
        finally:
            os.unlink(path)

    def test_loads_full_config_with_overrides(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "enabled": True,
                "default": "REASONING",
                "min_confidence": 2.5,
                "max_scan_bytes_per_message": 1024,
                "budget_warn_ms": 500,
                "weights": {"code_fence": 10.0},
            }, f)
            path = f.name
        try:
            cfg = classifier.load_config(path)
            self.assertEqual(cfg["default"], "REASONING")
            self.assertEqual(cfg["min_confidence"], 2.5)
            self.assertEqual(cfg["max_scan_bytes_per_message"], 1024)
            self.assertEqual(cfg["weights"]["code_fence"], 10.0)
            self.assertEqual(cfg["weights"]["tool_use"], 2.0)
        finally:
            os.unlink(path)

    def test_invalid_default_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"enabled": True, "default": "BOGUS"}, f)
            path = f.name
        try:
            with self.assertRaises(ValueError):
                classifier.load_config(path)
        finally:
            os.unlink(path)

    def test_default_config_has_strip_tags(self):
        self.assertIn("strip_tags", classifier.DEFAULT_CONFIG)
        self.assertEqual(
            classifier.DEFAULT_CONFIG["strip_tags"],
            classifier.DEFAULT_STRIP_TAGS,
        )
        self.assertIn("system-reminder", classifier.DEFAULT_STRIP_TAGS)
        self.assertIn("command-name", classifier.DEFAULT_STRIP_TAGS)
        self.assertIn("local-command-caveat", classifier.DEFAULT_STRIP_TAGS)

    def test_creates_default_file_when_missing(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "classification.json")
            cfg = classifier.load_config(path)
            self.assertFalse(cfg["enabled"])
            self.assertTrue(os.path.exists(path))
            with open(path) as f:
                on_disk = json.load(f)
            self.assertFalse(on_disk["enabled"])
            self.assertEqual(on_disk["default"], "CODING")


class TestExtractInspectionText(unittest.TestCase):
    def test_returns_empty_on_empty_messages(self):
        text, counts = classifier.extract_inspection_text({"messages": []}, 65536)
        self.assertEqual(text, "")
        self.assertEqual(counts["tool_use"], 0)
        self.assertEqual(counts["tool_result"], 0)

    def test_returns_empty_on_missing_messages(self):
        text, counts = classifier.extract_inspection_text({}, 65536)
        self.assertEqual(text, "")

    def test_picks_last_user_and_last_assistant(self):
        body = {
            "messages": [
                {"role": "user", "content": "old user msg"},
                {"role": "assistant", "content": "old assistant msg"},
                {"role": "user", "content": "LATEST_USER"},
                {"role": "assistant", "content": "LATEST_ASSISTANT"},
            ]
        }
        text, _ = classifier.extract_inspection_text(body, 65536)
        self.assertIn("LATEST_USER", text)
        self.assertIn("LATEST_ASSISTANT", text)
        self.assertNotIn("old user msg", text)
        self.assertNotIn("old assistant msg", text)

    def test_flattens_content_blocks(self):
        body = {
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "tool_use", "name": "Read", "input": {"path": "/x.py"}},
                ]},
                {"role": "assistant", "content": [
                    {"type": "tool_result", "content": "file contents here"},
                    {"type": "text", "text": "done"},
                ]},
            ]
        }
        text, counts = classifier.extract_inspection_text(body, 65536)
        self.assertIn("hello", text)
        self.assertIn("/x.py", text)
        self.assertIn("file contents here", text)
        self.assertIn("done", text)
        self.assertEqual(counts["tool_use"], 1)
        self.assertEqual(counts["tool_result"], 1)

    def test_truncates_per_message_tail(self):
        big = "X" * 1000 + "TAIL_MARKER"
        body = {"messages": [{"role": "user", "content": big}]}
        text, _ = classifier.extract_inspection_text(body, max_bytes=20)
        self.assertIn("TAIL_MARKER", text)
        self.assertLess(len(text), 100)

    def test_skips_system_field(self):
        body = {
            "system": "SYSTEM_PROMPT_MARKER " * 1000,
            "messages": [{"role": "user", "content": "user_only"}],
        }
        text, _ = classifier.extract_inspection_text(body, 65536)
        self.assertNotIn("SYSTEM_PROMPT_MARKER", text)
        self.assertIn("user_only", text)


class TestCountSignals(unittest.TestCase):
    def test_counts_code_fences(self):
        text = "look:\n```python\nprint(1)\n```\nand\n```\nx\n```"
        signals = classifier.count_signals(text, {"tool_use": 0, "tool_result": 0})
        self.assertEqual(signals["code_fence"], 2)
        self.assertGreater(signals["code_fence_bytes"], 0)

    def test_counts_tool_activity_from_structural(self):
        signals = classifier.count_signals("", {"tool_use": 3, "tool_result": 2})
        self.assertEqual(signals["tool_use"], 3)
        self.assertEqual(signals["tool_result"], 2)

    def test_counts_code_tokens(self):
        text = "def foo(): return 1\nclass Bar: import x\nconst y => {}"
        signals = classifier.count_signals(text, {"tool_use": 0, "tool_result": 0})
        self.assertGreater(signals["code_token"], 0)

    def test_counts_failure_markers(self):
        text = "Traceback (most recent call last):\n  at foo (file.js:10)\nSyntaxError: bad"
        signals = classifier.count_signals(text, {"tool_use": 0, "tool_result": 0})
        self.assertGreaterEqual(signals["failure_marker"], 2)

    def test_counts_file_paths(self):
        text = "see src/proxy.py and ./tests/test.py and config.json"
        signals = classifier.count_signals(text, {"tool_use": 0, "tool_result": 0})
        self.assertGreaterEqual(signals["file_path"], 3)

    def test_counts_reasoning_verbs(self):
        text = "Can you explain why this is better? Compare and analyze the trade-offs."
        signals = classifier.count_signals(text, {"tool_use": 0, "tool_result": 0})
        self.assertGreaterEqual(signals["reasoning_verb"], 3)

    def test_counts_question_marks_raw(self):
        text = "?" * 10 + "x" * 990
        signals = classifier.count_signals(text, {"tool_use": 0, "tool_result": 0})
        self.assertEqual(signals["question_mark"], 10)
        self.assertEqual(signals["text_bytes"], 1000)

    def test_structural_counts_saturated(self):
        signals = classifier.count_signals("", {"tool_use": 99, "tool_result": 50})
        self.assertEqual(signals["tool_use"], classifier.TOOL_SATURATION_CAP)
        self.assertEqual(signals["tool_result"], classifier.TOOL_SATURATION_CAP)

    def test_empty_text_returns_zeros(self):
        signals = classifier.count_signals("", {"tool_use": 0, "tool_result": 0})
        for v in signals.values():
            self.assertEqual(v, 0)


class TestClassify(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(classifier.DEFAULT_CONFIG)
        self.cfg["enabled"] = True
        self.cfg["weights"] = dict(classifier.DEFAULT_WEIGHTS)

    def test_returns_coding_for_fenced_code_plus_traceback(self):
        body = {"messages": [{"role": "user", "content":
            "fix this:\n```python\ndef f(): return 1/0\n```\n"
            "Traceback (most recent call last):\n  File \"x.py\", line 1\n"
            "ZeroDivisionError: division by zero"
        }]}
        result, scores = classifier.classify(body, self.cfg)
        self.assertEqual(result, "CODING")
        self.assertGreater(scores["code"], scores["reasoning"])

    def test_returns_reasoning_for_explainer_prompt(self):
        body = {"messages": [{"role": "user", "content":
            "Can you explain why functional programming is better than OOP? "
            "Please compare the trade-offs and analyze the pros and cons. "
            "What do you think about immutability?"
        }]}
        result, scores = classifier.classify(body, self.cfg)
        self.assertEqual(result, "REASONING")
        self.assertGreater(scores["reasoning"], scores["code"])

    def test_low_confidence_returns_default(self):
        self.cfg["default"] = "REASONING"
        body = {"messages": [{"role": "user", "content": "hi"}]}
        result, _ = classifier.classify(body, self.cfg)
        self.assertEqual(result, "REASONING")

    def test_empty_messages_returns_default(self):
        self.cfg["default"] = "CODING"
        body = {"messages": []}
        result, _ = classifier.classify(body, self.cfg)
        self.assertEqual(result, "CODING")

    def test_tool_use_blocks_push_toward_coding(self):
        body = {"messages": [
            {"role": "user", "content": "do it"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/x.py"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "content": "file contents"},
            ]},
        ]}
        result, scores = classifier.classify(body, self.cfg)
        self.assertEqual(result, "CODING")
        self.assertGreater(scores["code"], 0)

    def test_scan_ms_present_in_scores(self):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        _, scores = classifier.classify(body, self.cfg)
        self.assertIn("scan_ms", scores)
        self.assertGreaterEqual(scores["scan_ms"], 0.0)

    def test_large_input_truncated_and_fast(self):
        self.cfg["max_scan_bytes_per_message"] = 1024
        body = {"messages": [{"role": "user", "content": "X" * 5_000_000}]}
        result, scores = classifier.classify(body, self.cfg)
        self.assertIn(result, ("CODING", "REASONING"))
        self.assertLess(scores["scan_ms"], 1000.0)

    def test_user_reasoning_beats_assistant_code_context(self):
        """User asking 'why' should classify REASONING even after a code-heavy assistant turn."""
        body = {"messages": [
            {"role": "user", "content": "implement the function"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "```python\ndef foo():\n    return bar()\n```"},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "/main.py"}},
            ]},
            {"role": "user", "content":
                "Wait — can you explain why this approach is better than recursion? "
                "What are the trade-offs? Should I consider memoization instead?"
            },
        ]}
        result, scores = classifier.classify(body, self.cfg)
        self.assertEqual(result, "REASONING")
        self.assertGreater(scores["reasoning"], scores["code"])

    def test_expanded_reasoning_verbs_match_design_questions(self):
        body = {"messages": [{"role": "user", "content":
            "How does this approach handle concurrent writes? "
            "What are the alternatives? Should I consider event sourcing? "
            "Thoughts on the design tradeoffs? Would you recommend a different approach?"
        }]}
        result, _ = classifier.classify(body, self.cfg)
        self.assertEqual(result, "REASONING")

    def test_tightened_file_path_ignores_generic_urls(self):
        """Bare paths like '/v1/users' or '/tmp/x' should NOT count as file paths."""
        signals = classifier.count_signals(
            "send POST to /v1/users and check /tmp/somewhere",
            {"tool_use": 0, "tool_result": 0},
        )
        self.assertEqual(signals["file_path"], 0)


class TestLoadConfigLLM(unittest.TestCase):
    def test_method_defaults_to_internal(self):
        path = _llm_cfg_file({})
        # overwrite: no method key at all
        with open(path, "w") as f:
            json.dump({"enabled": True}, f)
        try:
            cfg = classifier.load_config(path)
            self.assertEqual(cfg["method"], "internal")
        finally:
            os.unlink(path)

    def test_derives_word_list_and_inverted_map_in_order(self):
        path = _llm_cfg_file({
            "llm_url": "http://h/v1/chat/completions",
            "llm_class_map": {
                "REASONING": ["planning", "analysis"],
                "CODING": ["coding", "testing"],
            },
        })
        try:
            cfg = classifier.load_config(path)
            # map-key order then word order within each key
            self.assertEqual(
                cfg["_llm_words"], ["planning", "analysis", "coding", "testing"]
            )
            self.assertEqual(cfg["_llm_inverted"]["coding"], "CODING")
            self.assertEqual(cfg["_llm_inverted"]["planning"], "REASONING")
        finally:
            os.unlink(path)

    def test_absent_llm_keys_default_cleanly(self):
        path = _llm_cfg_file({})  # only enabled + method=llm
        try:
            cfg = classifier.load_config(path)
            self.assertTrue(cfg["_llm_words"])
            self.assertEqual(cfg["_llm_inverted"]["coding"], "CODING")
            self.assertTrue(cfg["llm_url"])
        finally:
            os.unlink(path)

    def test_bad_class_map_key_raises(self):
        path = _llm_cfg_file({
            "llm_url": "http://h/v1",
            "llm_class_map": {"BOGUS": ["coding"]},
        })
        try:
            with self.assertRaises(ValueError):
                classifier.load_config(path)
        finally:
            os.unlink(path)

    def test_duplicate_word_across_buckets_raises(self):
        path = _llm_cfg_file({
            "llm_url": "http://h/v1",
            "llm_class_map": {
                "CODING": ["coding", "shared"],
                "REASONING": ["shared", "planning"],
            },
        })
        try:
            with self.assertRaisesRegex(ValueError, "shared"):
                classifier.load_config(path)
        finally:
            os.unlink(path)

    def test_empty_llm_url_raises_when_method_llm(self):
        path = _llm_cfg_file({"llm_url": ""})
        try:
            with self.assertRaises(ValueError):
                classifier.load_config(path)
        finally:
            os.unlink(path)


class TestBuildLLMInput(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(classifier.DEFAULT_CONFIG)

    def test_tool_calls_included_with_input(self):
        body = {"messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "do the thing"},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/secret.py"}},
                {"type": "tool_result", "content": "SECRET_RESULT_BODY"},
            ]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Grep", "input": {"q": "x"}},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/y"}},
            ]},
        ]}
        out = classifier._build_llm_input(body, self.cfg)
        self.assertIn("do the thing", out)
        self.assertIn("tool Read({", out)
        self.assertIn("/secret.py", out)
        self.assertIn("tool Grep({", out)
        self.assertNotIn("SECRET_RESULT_BODY", out)
        # all tool calls are included (dedup by name removed when full details added)
        self.assertIn('{"file_path": "/y"}', out)

    def test_user_text_truncated_to_max_bytes(self):
        self.cfg["max_scan_bytes_per_message"] = 20
        body = {"messages": [
            {"role": "user", "content": "X" * 1000 + "TAILMARK"},
        ]}
        out = classifier._build_llm_input(body, self.cfg)
        self.assertIn("TAILMARK", out)
        self.assertLess(len(out), 100)

    def test_no_tools_line_when_no_tool_use(self):
        body = {"messages": [{"role": "user", "content": "just text"}]}
        out = classifier._build_llm_input(body, self.cfg)
        self.assertEqual(out, "just text")


class TestParseLLMResponse(unittest.TestCase):
    INV = {"coding": "CODING", "reasoning": "REASONING"}

    def test_clean_word(self):
        self.assertEqual(
            classifier._parse_llm_response(_raw("coding"), self.INV),
            ("CODING", "coding"),
        )

    def test_trailing_punctuation(self):
        self.assertEqual(
            classifier._parse_llm_response(_raw("Coding."), self.INV),
            ("CODING", "coding"),
        )

    def test_sentence_wrapped_word(self):
        self.assertEqual(
            classifier._parse_llm_response(_raw("This is reasoning"), self.INV),
            ("REASONING", "reasoning"),
        )

    def test_unknown_word_raises(self):
        with self.assertRaises(ValueError):
            classifier._parse_llm_response(_raw("banana"), self.INV)

    def test_empty_content_raises(self):
        with self.assertRaises(ValueError):
            classifier._parse_llm_response(_raw(""), self.INV)

    def test_missing_content_raises(self):
        with self.assertRaises(ValueError):
            classifier._parse_llm_response(json.dumps({"choices": []}), self.INV)

    def test_error_response_returns_error_string(self):
        self.assertEqual(
            classifier._parse_llm_response(_raw("ERROR: input is too garbled"), self.INV),
            ("ERROR", None),
        )

    def test_error_response_with_case_variants(self):
        self.assertEqual(
            classifier._parse_llm_response(_raw("error: not clear"), self.INV),
            ("ERROR", None),
        )


class TestCallLLM(unittest.TestCase):
    def _cfg(self, **over):
        cfg = dict(classifier.DEFAULT_CONFIG)
        cfg.update({
            "method": "llm",
            "llm_url": "http://host:9/v1/chat/completions",
            "llm_model": "test-model",
            "llm_api_key": "",
            "llm_timeout_ms": 1500,
            "_llm_words": ["coding", "reasoning"],
        })
        cfg.update(over)
        return cfg

    class _FakeResp:
        def __init__(self, status, body):
            self.status = status
            self._b = body.encode()

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def test_request_shape_and_no_auth_when_keyless(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["auth"] = req.get_header("Authorization")
            captured["timeout"] = timeout
            captured["body"] = json.loads(req.data.decode())
            return self._FakeResp(200, _raw("coding"))

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            classifier._call_llm("hello", self._cfg())
        self.assertEqual(captured["url"], "http://host:9/v1/chat/completions")
        self.assertIsNone(captured["auth"])
        self.assertEqual(captured["timeout"], 1.5)
        self.assertEqual(captured["body"]["model"], "test-model")
        self.assertEqual(captured["body"]["temperature"], 0)
        self.assertEqual(captured["body"]["max_tokens"], 1024)
        roles = [m["role"] for m in captured["body"]["messages"]]
        self.assertEqual(roles, ["system", "user"])

    def test_auth_header_present_when_key_set(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["auth"] = req.get_header("Authorization")
            return self._FakeResp(200, _raw("coding"))

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            classifier._call_llm("hi", self._cfg(llm_api_key="SEKRET"))
        self.assertEqual(captured["auth"], "Bearer SEKRET")

    def test_timeout_raises(self):
        def fake_urlopen(req, timeout=None):
            raise socket.timeout("timed out")

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(Exception):
                classifier._call_llm("hi", self._cfg())

    def test_non_200_raises(self):
        def fake_urlopen(req, timeout=None):
            return self._FakeResp(500, "boom")

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(Exception):
                classifier._call_llm("hi", self._cfg())


class TestClassifyRequestDispatch(unittest.TestCase):
    def test_internal_method_uses_heuristic_untouched(self):
        cfg = dict(classifier.DEFAULT_CONFIG)
        cfg["enabled"] = True
        cfg["weights"] = dict(classifier.DEFAULT_WEIGHTS)
        body = {"messages": [{"role": "user", "content":
            "fix this:\n```python\ndef f(): return 1/0\n```\n"
            "Traceback (most recent call last):\nZeroDivisionError"}]}
        expect_cls, _ = classifier.classify(body, cfg)
        cls, scores = classifier.classify_request(body, cfg)
        self.assertEqual(cls, expect_cls)
        self.assertEqual(scores["method"], "internal")

    def test_llm_success_returns_mapped_key(self):
        path = _llm_cfg_file({"llm_url": "http://h/v1"})
        try:
            cfg = classifier.load_config(path)
        finally:
            os.unlink(path)
        body = {"messages": [{"role": "user", "content": "write a function"}]}
        with mock.patch.object(classifier, "_call_llm",
                               return_value=(_raw("coding"), {"messages": []})):
            cls, scores = classifier.classify_request(body, cfg)
        self.assertEqual(cls, "CODING")
        self.assertEqual(scores["method"], "llm")
        # downstream proxy logging requires these keys on every path
        for k in ("code", "reasoning", "scan_ms"):
            self.assertIn(k, scores)

    def test_internal_path_has_no_llm_exchange(self):
        cfg = dict(classifier.DEFAULT_CONFIG)
        cfg["enabled"] = True
        cfg["weights"] = dict(classifier.DEFAULT_WEIGHTS)
        body = {"messages": [{"role": "user", "content": "hi"}]}
        _, scores = classifier.classify_request(body, cfg)
        self.assertNotIn("llm_exchange", scores)

    def test_llm_success_includes_exchange(self):
        path = _llm_cfg_file({"llm_url": "http://h/v1", "llm_model": "qmodel"})
        try:
            cfg = classifier.load_config(path)
        finally:
            os.unlink(path)
        body = {"messages": [{"role": "user", "content": "write a function"}]}
        with mock.patch.object(classifier, "_call_llm",
                               return_value=(_raw("coding"), {"messages": []})):
            _, scores = classifier.classify_request(body, cfg)
        ex = scores["llm_exchange"]
        self.assertEqual(ex["url"], "http://h/v1")
        self.assertEqual(ex["model"], "qmodel")
        self.assertIn("write a function", ex["input"])
        self.assertEqual(ex["status"], 200)
        self.assertEqual(ex["response"], _raw("coding"))
        self.assertEqual(ex["classified"], "CODING")
        self.assertEqual(ex["raw_class"], "coding")
        self.assertFalse(ex["fallback"])
        self.assertIsNone(ex["error"])

    def test_llm_fallback_includes_exchange(self):
        path = _llm_cfg_file({"llm_url": "http://h/v1"})
        try:
            cfg = classifier.load_config(path)
            cfg["weights"] = dict(classifier.DEFAULT_WEIGHTS)
        finally:
            os.unlink(path)
        body = {"messages": [{"role": "user", "content": "explain why, compare trade-offs"}]}
        with mock.patch.object(classifier, "_call_llm", side_effect=RuntimeError("boom")):
            _, scores = classifier.classify_request(body, cfg)
        ex = scores["llm_exchange"]
        self.assertTrue(ex["fallback"])
        self.assertIn("boom", ex["error"])
        self.assertIsNone(ex["status"])

    def test_llm_failure_falls_back_to_heuristic(self):
        path = _llm_cfg_file({"llm_url": "http://h/v1"})
        try:
            cfg = classifier.load_config(path)
            cfg["weights"] = dict(classifier.DEFAULT_WEIGHTS)
        finally:
            os.unlink(path)
        body = {"messages": [{"role": "user", "content":
            "Can you explain why this is better? Compare the trade-offs "
            "and analyze the pros and cons. What do you think?"}]}
        heur_cls, _ = classifier.classify(body, cfg)
        with mock.patch.object(classifier, "_call_llm", side_effect=RuntimeError("net")):
            cls, scores = classifier.classify_request(body, cfg)
        self.assertEqual(cls, heur_cls)
        self.assertEqual(scores["method"], "llm_fallback")


class TestStripContextTags(unittest.TestCase):
    def test_strips_simple_tag(self):
        text = "<system-reminder>hello world</system-reminder>Hello, world!"
        result = classifier._strip_context_tags(text, ["system-reminder"])
        self.assertEqual(result, "Hello, world!")

    def test_strips_self_closing_tag(self):
        text = "<command-name>clear</command-name>\nHello!"
        result = classifier._strip_context_tags(text, ["command-name"])
        self.assertEqual(result, "Hello!")

    def test_strips_multiple_different_tags(self):
        text = "<system-reminder>context</system-reminder>" \
               "<command-name>/foo</command-name>" \
               "<command-message>foo</command-message>" \
               "real content"
        result = classifier._strip_context_tags(text, [
            "system-reminder", "command-name", "command-message",
        ])
        self.assertEqual(result, "real content")

    def test_collapse_multiple_newlines(self):
        text = "a\n\n\n\nb\n\n\nc"
        result = classifier._strip_context_tags(text, [])
        self.assertEqual(result, "a\n\nb\n\nc")

    def test_strips_tag_and_collapse_newlines(self):
        text = "<system-reminder>ignore me</system-reminder>\n\n\nHello, world!"
        result = classifier._strip_context_tags(text, ["system-reminder"])
        self.assertEqual(result, "Hello, world!")

    def test_no_tag_no_change(self):
        text = "just plain text"
        result = classifier._strip_context_tags(text, [])
        self.assertEqual(result, "just plain text")

    def test_none_text_returns_none(self):
        result = classifier._strip_context_tags(None, ["x"])
        self.assertIsNone(result)

    def test_default_tags_strip_as_expected(self):
        """All default tags should be stripped by DEFAULT_STRIP_TAGS."""
        body = {
            "messages": [{
                "role": "user",
                "content": (
                    "<system-reminder>context-mode:ctx-purge</system-reminder>\n"
                    "<command-name>/clear</command-name>\n"
                    "<command-message>clear</command-message>\n"
                    "<command-args></command-args>\n"
                    "<local-command-caveat>Caveat</local-command-caveat>\n"
                    "<local-command-stdout></local-command-stdout>\n"
                    "real prompt here"
                ),
            }],
        }
        cfg = dict(classifier.DEFAULT_CONFIG)
        cfg["enabled"] = True
        cfg["weights"] = dict(classifier.DEFAULT_WEIGHTS)
        result, _ = classifier.classify(body, cfg)
        # Should be low-confidence, returning default CODING
        self.assertEqual(result, "CODING")


class TestStripTagsInLLMInput(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(classifier.DEFAULT_CONFIG)

    def test_llm_input_has_tags_stripped(self):
        body = {
            "messages": [{
                "role": "user",
                "content": (
                    "<system-reminder>hello</system-reminder>\n"
                    "<command-name>foo</command-name>\n"
                    "write a function"
                ),
            }],
        }
        out = classifier._build_llm_input(body, self.cfg)
        self.assertNotIn("system-reminder", out)
        self.assertNotIn("command-name", out)
        self.assertNotIn("hello", out)
        self.assertNotIn("foo", out)
        self.assertIn("write a function", out)

    def test_llm_input_no_tags_unchanged(self):
        body = {"messages": [{"role": "user", "content": "clean prompt"}]}
        out = classifier._build_llm_input(body, self.cfg)
        self.assertEqual(out, "clean prompt")


if __name__ == "__main__":
    unittest.main()
