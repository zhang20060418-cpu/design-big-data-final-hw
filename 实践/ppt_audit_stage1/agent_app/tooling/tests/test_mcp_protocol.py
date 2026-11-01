from __future__ import annotations

import unittest

from ppt_audit_tool.mcp_server import MCPServer, TOOLS


class FakeClient:
    base_url = "http://fake"

    def health(self, timeout: float = 3.0):
        return {"ok": True}

    def capabilities(self):
        return {
            "app": {"name": "SlideSentinel", "version": "test"},
            "modules": {
                "six_detector": {"state": "ready", "name": "六类"},
                "mllm": {"state": "not_configured", "name": "复核"},
            },
            "limits": {"max_upload_mb": 80},
            "demo": {"task_count": 4},
            "task_count": 7,
        }


class MCPProtocolTests(unittest.TestCase):
    def setUp(self):
        self.server = MCPServer(client_factory=lambda: FakeClient())

    def test_initialize_negotiates_protocol(self):
        response = self.server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        })
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(response["result"]["serverInfo"]["name"], "slidesentinel")

    def test_unknown_protocol_falls_back(self):
        response = self.server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "1900-01-01"},
        })
        self.assertEqual(response["result"]["protocolVersion"], "2024-11-05")

    def test_tools_list(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [item["name"] for item in response["result"]["tools"]]
        self.assertEqual(names, [item["name"] for item in TOOLS])
        self.assertIn("ppt_audit", names)
        self.assertIn("ppt_repair", names)

    def test_capabilities_call(self):
        response = self.server.handle({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "ppt_capabilities", "arguments": {}},
        })
        result = response["result"]
        self.assertFalse(result["isError"])
        self.assertTrue(result["structuredContent"]["ok"])
        self.assertIn("profiles", result["content"][0]["text"])
        self.assertIn("six_detector", result["content"][0]["text"])

    def test_unknown_method(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 4, "method": "no/such"})
        self.assertEqual(response["error"]["code"], -32601)

    def test_notification_has_no_response(self):
        self.assertIsNone(self.server.handle({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }))

    def test_missing_argument_is_tool_error(self):
        response = self.server.handle({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "ppt_status", "arguments": {}},
        })
        result = response["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "missing_argument")

    def test_unknown_tool_is_tool_error(self):
        response = self.server.handle({
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "ppt_destroy", "arguments": {}},
        })
        result = response["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "unknown_tool")


if __name__ == "__main__":
    unittest.main()
