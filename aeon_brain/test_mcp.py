"""Synthetic MCP contract tests for the Aeon Brain server (no Hermes runtime)."""
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from brain_service.gateway import Gateway
from brain_service.policy import Principal

import aeon_brain.mcp as brain_mcp

ROOT = Path(__file__).resolve().parent.parent
CAPS = frozenset({"read", "capture", "revise_own", "retract_own", "propose"})


def frame(value):
    return (json.dumps(value) + "\n").encode()


class BrainMcpBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "memory.sqlite"
        self.db = sqlite3.connect(self.path, isolation_level=None)
        for name in ["aeon_sharedwrites/base_schema_fixture.sql",
                     "aeon_sharedwrites/migration.sql", "brain_service/migration.sql"]:
            self.db.executescript((ROOT / name).read_text())

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def run_requests(self, store, requests):
        output = io.StringIO()
        payload = b"".join(frame(r) for r in requests)
        brain_mcp.serve(store, io.BytesIO(payload), output)
        return [json.loads(line) for line in output.getvalue().splitlines()]


class TestMcpContract(BrainMcpBase):
    def setUp(self):
        super().setUp()
        self.principal = Principal("planner", "local", CAPS, "chat:planner")
        self.gateway = Gateway(self.db, principals={"planner": self.principal},
                               peer_bindings={os.getuid(): "planner"})
        self.store = brain_mcp.DirectStore(self.db, self.gateway, os.getuid())

    def test_initialize_tools_list_and_call_shape(self):
        responses = self.run_requests(self.store, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "status", "arguments": {}}},
        ])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "aeon-brain")
        names = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertEqual(names, {"aeon_capture", "aeon_correct", "aeon_get", "aeon_search",
                                 "aeon_recent", "revise", "propose", "retract", "status",
                                 "interests", "consolidate_preview", "consolidate_stage",
                                 "review_stage", "review_pending", "review_history",
                                 "review_decide"})
        status = responses[2]["result"]
        self.assertFalse(status["isError"])
        self.assertEqual(status["structuredContent"]["result"]["api_version"], "brain.general.v1")
        self.assertEqual(status["content"][0]["text"], json.dumps(status["structuredContent"]))

    def test_capture_receipt_then_stale_revision_is_logical_error(self):
        responses = self.run_requests(self.store, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "aeon_capture", "arguments": dict(
                 request_id="one", domain="work", kind="idea",
                 attribution_basis="user_explicit", statement="Synthetic compiler project")}},
        ])
        capture = responses[1]["result"]
        self.assertFalse(capture["isError"])
        mid = capture["structuredContent"]["result"]["memory_id"]

        stale_response, = self.run_requests(self.store, [
            {"jsonrpc": "2.0", "id": 8, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
             "params": {"name": "revise", "arguments": dict(
                 request_id="two", memory_id=mid, expected_revision=7,
                 reason="Synthetic stale edit", patch={"statement": "Nope"})}},
        ])[1:]
        conflict = stale_response["result"]
        self.assertTrue(conflict["isError"])
        self.assertEqual(conflict["structuredContent"]["error"]["code"], "revision_conflict")
        self.assertEqual(conflict["structuredContent"]["error"]["current_revision"], 1)

    def test_unknown_tool_uninitialized_and_methods(self):
        responses = self.run_requests(self.store, [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "delete_everything", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "resources/list", "params": {}},
        ])
        self.assertEqual(responses[0]["error"]["code"], -32602)
        # The gateway is the authority on operations: unknown names come back
        # as logical tool errors, not protocol errors.
        unknown = responses[2]["result"]
        self.assertTrue(unknown["isError"])
        self.assertEqual(unknown["structuredContent"]["error"]["code"], "invalid_request")
        self.assertEqual(responses[3]["error"]["code"], -32601)

    def test_oversized_frame_terminates_without_partial_response(self):
        output = io.StringIO()
        brain_mcp.serve(self.store, io.BytesIO(b'{"jsonrpc":"2.0"}' + b"x" * 70000 + b"\n"), output)
        response = json.loads(output.getvalue().splitlines()[0])
        self.assertEqual(response["error"]["code"], -32600)

    def test_socket_mode_maps_client_failures(self):
        store = brain_mcp.SocketStore("/missing-synthetic.sock")
        with self.assertRaises(brain_mcp.Unavailable):
            store.call("status", {})
        with patch.object(store.client, "call", side_effect=brain_mcp.Invalid("bad")):
            with self.assertRaises(brain_mcp.Invalid):
                store.call("status", {})


class TestDirectModeStartup(BrainMcpBase):
    def entry(self, *args):
        return subprocess.run([sys.executable, "-m", "aeon_brain.mcp", *args],
                              cwd=ROOT, capture_output=True, text=True, timeout=15,
                              input="", env=dict(os.environ, PYTHONPATH=str(ROOT)))

    def test_refuses_database_without_general_schema(self):
        plain = Path(self.temp.name) / "plain.sqlite"
        sqlite3.connect(plain).close()
        result = self.entry("--db", str(plain))
        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing to start", result.stderr)

    def test_inline_principal_startup_is_clean_and_neutral(self):
        result = self.entry("--db", str(self.path))
        self.assertEqual(result.returncode, 0, result.stderr)
        check = subprocess.run([sys.executable, "-c",
            "import sys; from aeon_brain.mcp import SocketStore, DirectStore; "
            "from brain_service.client import Client; "
            "assert 'brain_service.hermes' not in sys.modules; "
            "assert not any(name.startswith(('hermes_agent','hermes_cli')) for name in sys.modules)"],
            cwd=ROOT, capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=str(ROOT)))
        self.assertEqual(check.returncode, 0, check.stderr)


if __name__ == "__main__":
    unittest.main()
