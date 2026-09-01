import unittest

from runner.providers import consume_sse


class TestConsumeSse(unittest.TestCase):
    def test_folds_content_deltas(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"sand"}}]}',
            'data: {"choices":[{"delta":{"content":"box-ok"}}]}',
            "data: [DONE]",
        ]
        text, tools, meta = consume_sse(lines)
        self.assertEqual(text, "sandbox-ok")
        self.assertIsNone(tools)
        self.assertEqual(meta["sse_chunks"], 2)

    def test_folds_tool_call_argument_pieces(self):
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"search_memory","arguments":"{"}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"}"}}]}}]}',
            "data: [DONE]",
        ]
        text, tools, _ = consume_sse(lines)
        self.assertEqual(text, "")
        self.assertEqual(tools[0]["function"]["name"], "search_memory")
        self.assertEqual(tools[0]["function"]["arguments"], "{}")

    def test_folds_message_tool_calls(self):
        lines = [
            'data: {"choices":[{"message":{"tool_calls":[{"id":"c1","function":{"name":"mcp_memory_search","arguments":"{\\"query\\":\\"canary\\"}"}}]}}]}',
            "data: [DONE]",
        ]
        text, tools, _ = consume_sse(lines)
        self.assertEqual(text, "")
        self.assertEqual(tools[0]["function"]["name"], "mcp_memory_search")
        self.assertIn("canary", tools[0]["function"]["arguments"])


if __name__ == "__main__":
    unittest.main()
