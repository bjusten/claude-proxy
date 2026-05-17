"""Tests for journal.usage.extract_usage."""

import json
import unittest

from journal.usage import extract_usage


class TestJsonAnthropic(unittest.TestCase):
    def test_anthropic_json_extracts_input_output(self):
        body = json.dumps({
            "id": "msg_01",
            "type": "message",
            "usage": {"input_tokens": 1234, "output_tokens": 567},
        }).encode("utf-8")
        result = extract_usage(body, "application/json")
        self.assertEqual(result, {"input": 1234, "output": 567})


class TestJsonOpenAI(unittest.TestCase):
    def test_openai_json_maps_prompt_completion(self):
        body = json.dumps({
            "id": "chatcmpl-1",
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        }).encode("utf-8")
        result = extract_usage(body, "application/json")
        self.assertEqual(result, {"input": 100, "output": 50})


class TestSseAnthropic(unittest.TestCase):
    def test_sse_anthropic_message_start_and_delta(self):
        body = (
            b'event: message_start\n'
            b'data: {"type":"message_start","message":{"id":"msg_1","usage":{"input_tokens":42,"output_tokens":1}}}\n\n'
            b'event: content_block_delta\n'
            b'data: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n'
            b'event: message_delta\n'
            b'data: {"type":"message_delta","usage":{"output_tokens":99}}\n\n'
            b'event: message_stop\n'
            b'data: {"type":"message_stop"}\n\n'
        )
        result = extract_usage(body, "text/event-stream")
        self.assertEqual(result, {"input": 42, "output": 99})


class TestSseOpenAI(unittest.TestCase):
    def test_sse_openai_final_chunk_usage(self):
        body = (
            b'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"hi"}}]}\n\n'
            b'data: {"id":"chatcmpl-1","choices":[{"delta":{}}],"usage":{"prompt_tokens":42,"completion_tokens":7,"total_tokens":49}}\n\n'
            b'data: [DONE]\n\n'
        )
        result = extract_usage(body, "text/event-stream")
        self.assertEqual(result, {"input": 42, "output": 7})


class TestEdgeCases(unittest.TestCase):
    def test_empty_body_returns_none(self):
        self.assertIsNone(extract_usage(b"", "application/json"))

    def test_malformed_json_returns_none(self):
        self.assertIsNone(extract_usage(b"{not json", "application/json"))

    def test_non_json_non_sse_returns_none(self):
        self.assertIsNone(extract_usage(b"<html>oops</html>", "text/html"))

    def test_missing_content_type_sniffs_json(self):
        body = b'{"usage":{"input_tokens":3,"output_tokens":4}}'
        self.assertEqual(extract_usage(body, ""), {"input": 3, "output": 4})

    def test_no_usage_field_returns_none(self):
        self.assertIsNone(extract_usage(b'{"id":"x"}', "application/json"))

    def test_sse_with_no_usage_returns_none(self):
        body = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'
        self.assertIsNone(extract_usage(body, "text/event-stream"))


if __name__ == "__main__":
    unittest.main()
