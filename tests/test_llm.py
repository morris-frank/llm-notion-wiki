from __future__ import annotations

import json
from unittest import mock
import unittest

from llmwiki_runtime.llm import OpenAICompatiblePlanner


class _Response:
    def __init__(self, body: dict) -> None:
        self.body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self.body


class PlannerTests(unittest.TestCase):
    @mock.patch("llmwiki_runtime.llm.request.urlopen")
    def test_planner_sends_expected_payload(self, mock_urlopen) -> None:
        captured = {}

        def fake_urlopen(req):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["data"] = json.loads(req.data.decode("utf-8"))
            return _Response({"choices": [{"message": {"content": "{\"ok\":true}"}}]})

        mock_urlopen.side_effect = fake_urlopen
        planner = OpenAICompatiblePlanner(
            api_key="key",
            api_base="https://api.example.com/v1",
            model="test-model",
            system_prompt="system",
        )
        output = planner.plan({"bundle": True})
        self.assertEqual(output, '{"ok":true}')
        self.assertEqual(captured["url"], "https://api.example.com/v1/chat/completions")
        self.assertEqual(captured["data"]["model"], "test-model")
        self.assertEqual(captured["data"]["temperature"], 0)
        self.assertEqual(captured["data"]["messages"][0]["content"], "system")
        self.assertIn("Authorization", captured["headers"])

    @mock.patch("llmwiki_runtime.llm.request.urlopen")
    def test_planner_flattens_list_content(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _Response(
            {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": "hello "},
                                {"type": "input_text", "text": "ignored"},
                                {"type": "text", "text": "world"},
                            ]
                        }
                    }
                ]
            }
        )
        planner = OpenAICompatiblePlanner(api_key="key", api_base="https://api.example.com/v1", model="test-model", system_prompt="system")
        self.assertEqual(planner.plan({"bundle": True}), "hello world")

    @mock.patch("llmwiki_runtime.llm.request.urlopen")
    def test_planner_raises_on_empty_choices(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _Response({"choices": []})
        planner = OpenAICompatiblePlanner(api_key="key", api_base="https://api.example.com/v1", model="test-model", system_prompt="system")
        with self.assertRaises(RuntimeError):
            planner.plan({"bundle": True})

    @mock.patch("llmwiki_runtime.llm.request.urlopen")
    def test_planner_raises_on_non_text_content(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _Response({"choices": [{"message": {"content": {"unexpected": True}}}]})
        planner = OpenAICompatiblePlanner(api_key="key", api_base="https://api.example.com/v1", model="test-model", system_prompt="system")
        with self.assertRaises(RuntimeError):
            planner.plan({"bundle": True})


if __name__ == "__main__":
    unittest.main()
