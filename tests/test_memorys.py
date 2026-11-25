"""Cross-platform unit & integration tests for MemoryS."""
from __future__ import annotations

from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "desktop-app" / "service"))
sys.path.insert(0, str(REPO_ROOT / "runtime"))

import backend as b  # noqa: E402
import mcp_server  # noqa: E402
import memory_cli  # noqa: E402
import memory_reader as mr  # noqa: E402
import memory_store as ms  # noqa: E402
from recall_hook import recall  # noqa: E402
import web_server  # noqa: E402


class ConflictCheckFixture(BaseHTTPRequestHandler):
    last_payload = None

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        ConflictCheckFixture.last_payload = data
        content = (
            "OK"
            if len(data["messages"]) == 1
            else "1. Updates existing rule; 2. Recommend superseding active card; 3. Clear wording."
        )
        self.send_response(200)
        self.end_headers()
        self.wfile.write(
            json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")
        )

    def log_message(self, *args):
        pass


class MemorySTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.data_dir = self.base / "app-data"
        self.home = self.base / "home"
        self.home.mkdir()
        self.vault = ms.init_vault(self.base / "vault", with_demo=False)

        self.patch = patch.multiple(b, BASE=self.data_dir, HOME=self.home)
        self.patch.start()
        self.db = b.connect(self.data_dir)

        self.server = HTTPServer(("127.0.0.1", 0), ConflictCheckFixture)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.cfg = {
            "provider": "Local Fixture",
            "url": f"http://127.0.0.1:{self.server.server_port}",
            "model": "fixture-chat",
        }

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.db.close()
        self.patch.stop()
        self.tmp.cleanup()

    def call(self, action, **args):
        return b.dispatch(self.db, action, args, "", vault=self.vault)

    def test_demo_vault_temporal_recall_and_trace(self):
        demo = ms.init_vault(self.base / "demo-vault", with_demo=True)
        reader = mr.VaultReader(demo)

        # Historical date hits v1 (7 days)
        may_hits = reader.search(project="acme-billing", as_of="2026-05-01")
        self.assertEqual([c["id"] for c in may_hits], ["d-acme-billing-grace-v1"])

        # Current date after 2026-06-15 hits v2 (14 days)
        july_hits = reader.search(project="Billing", as_of="2026-07-01")
        self.assertEqual([c["id"] for c in july_hits], ["d-acme-billing-grace-v2"])

        # Provenance trace returns verbatim quote
        traced = reader.trace_card("d-acme-billing-grace-v2")
        self.assertEqual(len(traced["events"]), 1)
        self.assertIn("14 days", traced["events"][0]["text"])

        # Handoff & always-on preference
        ho = reader.read_card("handoff", project="acme-billing")
        self.assertIn("14 days", ho["body"])
        hook_out = recall({"prompt": "Check Billing invoice suspension", "cwd": "/tmp"}, demo)
        ctx = hook_out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Lead with conclusions", ctx)
        self.assertIn("14-day grace period", ctx)

    def test_mcp_propose_direct_publish_and_git_commit(self):
        res = mcp_server.handle_tool_call(
            "memory_propose",
            {
                "scope": "Payments API",
                "title": "Webhook retry schedule",
                "body": "Retry failed webhooks with exponential backoff up to 5 attempts.",
                "effective_from": "2026-11-24",
                "quote": "Confirmed: retry failed webhooks 5 times with exponential backoff.",
                "use_when": ["webhook", "retry"],
                "client": "Codex",
            },
            vault_path=self.vault,
            app_data_dir=self.data_dir,
        )
        self.assertEqual(res["state"], "pending")
        pid = res["proposal_id"]

        # Must NOT touch vault before publish
        self.assertEqual(mr.VaultReader(self.vault).cards, {})

        # Non-blocking publish without AI conflict check
        pub = self.call("publish", ids=[pid])
        self.assertEqual(pub["count"], 1)
        self.assertTrue(pub["revision"])
        self.assertEqual(self.call("snapshot")["proposals"], [])

        reader = mr.VaultReader(self.vault)
        proj = reader.resolve_project("Payments API")
        self.assertIsNotNone(proj)
        self.assertEqual(proj.project_id, "payments-api")
        card_id = pub["cards"][0]["id"]
        traced = reader.trace_card(card_id)
        self.assertIn("exponential backoff", traced["events"][0]["text"])

        # Clean git working tree after publish
        git_status = subprocess.run(
            [ms.find_git(), "-C", str(self.vault), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(git_status, "")

    def test_supersedes_cross_day_and_same_day_as_of(self):
        p1 = self.call(
            "createProposal",
            scope="project/new:Order Core",
            title="Order expiration",
            body="Unpaid orders expire after 30 minutes.",
            effective_from="2026-11-20",
        )["id"]
        v1_id = self.call("publish", ids=[p1])["cards"][0]["id"]

        p2 = self.call(
            "createProposal",
            scope="Order Core",
            title="Order expiration",
            body="Unpaid orders expire after 15 minutes.",
            effective_from="2026-11-24",
            target=v1_id,
        )["id"]
        v2_id = self.call("publish", ids=[p2])["cards"][0]["id"]

        reader = mr.VaultReader(self.vault)
        self.assertEqual(reader.cards[v1_id].status, "superseded")
        self.assertEqual(reader.cards[v1_id].superseded_by, v2_id)
        self.assertEqual(reader.cards[v1_id].effective_until, "2026-11-23")

        self.assertEqual([c["id"] for c in reader.search(project="Order Core", as_of="2026-11-21")], [v1_id])
        self.assertEqual([c["id"] for c in reader.search(project="Order Core", as_of="2026-11-24")], [v2_id])

        # Same-day revision v3
        p3 = self.call(
            "createProposal",
            scope="Order Core",
            title="Order expiration",
            body="VIP orders expire after 60 minutes; standard after 15 minutes.",
            effective_from="2026-11-24",
            target=v2_id,
        )["id"]
        v3_id = self.call("publish", ids=[p3])["cards"][0]["id"]

        reader2 = mr.VaultReader(self.vault)
        self.assertEqual(reader2.cards[v2_id].effective_until, "2026-11-24")
        self.assertEqual([c["id"] for c in reader2.search(project="Order Core", as_of="2026-11-24")], [v3_id])
        self.assertEqual(
            {c["id"] for c in reader2.search(project="Order Core", include_history=True)},
            {v1_id, v2_id, v3_id},
        )

    def test_preferences_always_on_and_archive_stops_recall(self):
        p = self.call(
            "createProposal",
            scope="shared/preferences",
            title="Concise engineering replies",
            body="Always lead with the answer first and keep explanations terse.",
            effective_from=date.today().isoformat(),
        )["id"]
        pref_id = self.call("publish", ids=[p])["cards"][0]["id"]

        out1 = recall({"prompt": "Why did this build fail?", "cwd": "/tmp"}, self.vault)
        self.assertIn("lead with the answer", out1["hookSpecificOutput"]["additionalContext"])

        arch = self.call("archiveCard", id=pref_id)
        self.assertEqual(arch["status"], "archived")
        self.assertEqual(recall({"prompt": "Why did this build fail?", "cwd": "/tmp"}, self.vault), {})

    def test_project_cwd_and_alias_dual_recall(self):
        p = self.call(
            "createProposal",
            scope="project/new:AlphaPay",
            title="Callback signature",
            body="Verify HMAC-SHA256 signatures on all payment callbacks.",
            effective_from=date.today().isoformat(),
        )["id"]
        self.call("publish", ids=[p])
        proj_dir = self.base / "alphapay-repo"
        proj_dir.mkdir()
        self.call(
            "saveProjectMeta",
            project_id="alphapay",
            aliases=["AlphaPay", "APay"],
            paths=[str(proj_dir)],
        )

        hit_cwd = recall({"prompt": "How to verify callbacks?", "cwd": str(proj_dir)}, self.vault)
        self.assertIn("HMAC-SHA256", hit_cwd["hookSpecificOutput"]["additionalContext"])

        hit_alias = recall({"prompt": "What are the APay rules?", "cwd": "/tmp"}, self.vault)
        self.assertIn("HMAC-SHA256", hit_alias["hookSpecificOutput"]["additionalContext"])

        self.assertEqual(recall({"prompt": "Weather today?", "cwd": "/tmp"}, self.vault), {})

    def test_secret_and_forbidden_directives_blocked(self):
        with self.assertRaises(ValueError):
            mcp_server.handle_tool_call(
                "memory_propose",
                {
                    "scope": "shared/preferences",
                    "title": "Secret key test",
                    "body": "Use token sk-proj1234567890abcdef123456",
                },
                vault_path=self.vault,
                app_data_dir=self.data_dir,
            )
        for phrase in ("Please do not record this note.", "这段话不要记录到记忆库。"):
            with self.assertRaises(ValueError):
                mcp_server.handle_tool_call(
                    "memory_propose",
                    {
                        "scope": "shared/preferences",
                        "title": "Private note",
                        "body": phrase,
                    },
                    vault_path=self.vault,
                    app_data_dir=self.data_dir,
                )

    def test_optional_ai_conflict_check_and_stdio_mcp(self):
        p1 = self.call(
            "createProposal",
            scope="acme-core",
            title="Customer ID prefix",
            body="All customer IDs start with CUST-.",
            effective_from=date.today().isoformat(),
        )["id"]
        self.call("publish", ids=[p1])

        p2 = self.call(
            "createProposal",
            scope="acme-core",
            title="Customer ID suffix",
            body="Enterprise customer IDs also end with -ENT.",
            effective_from=date.today().isoformat(),
        )["id"]

        self.call("saveConfig", **self.cfg)
        self.assertTrue(self.call("test", **self.cfg)["ok"])
        res = self.call("reviewProposal", id=p2, lang="en")
        self.assertIn("superseding", res["opinion"])

        # Verify stdio MCP server end-to-end
        env = dict(
            os.environ,
            MEMORYS_VAULT=str(self.vault),
            MEMORYS_APP_DATA=str(self.data_dir),
        )
        requests = "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "memory_search", "arguments": {"project": "acme-core"}},
                    }
                ),
            ]
        ) + "\n"
        proc = subprocess.run(
            [sys.executable, "-B", str(REPO_ROOT / "runtime" / "mcp_server.py")],
            input=requests,
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        replies = [json.loads(line) for line in proc.stdout.strip().splitlines()]
        self.assertEqual(len(replies), 3)
        self.assertEqual(
            [t["name"] for t in replies[1]["result"]["tools"]],
            ["memory_search", "memory_read", "memory_trace", "memory_propose"],
        )
        search_res = json.loads(replies[2]["result"]["content"][0]["text"])
        self.assertEqual(search_res["count"], 1)

    def test_connect_apply_and_legacy_chinese_vault_compat(self):
        # Connect apply writes Claude, Codex, and Cursor configs cleanly
        memory_cli._connect_client("claude", self.vault, apply=True, home=self.home)
        memory_cli._connect_client("codex", self.vault, apply=True, home=self.home)
        memory_cli._connect_client("cursor", self.vault, apply=True, home=self.home)
        status = b.client_integration_status(self.home)
        self.assertTrue(status["claude"]["mcp"])
        self.assertTrue(status["claude"]["hook"])
        self.assertTrue(status["codex"]["mcp"])
        self.assertTrue(status["codex"]["hook"])
        self.assertTrue(status["cursor"]["mcp"])

        # Legacy Chinese vault (项目索引.md + 来源记录.md) stays 100% readable & writable
        zh_vault = self.base / "zh-vault"
        for sub in ("projects", "notes/preferences", "notes/methods", "notes/domain"):
            (zh_vault / sub).mkdir(parents=True, exist_ok=True)
        (zh_vault / "项目索引.md").write_text("# 项目索引\n", encoding="utf-8")
        (zh_vault / "来源记录.md").write_text("# 来源记录\n\n```jsonl\n```\n", encoding="utf-8")
        subprocess.run([ms.find_git(), "init", "-q", "-b", "main", str(zh_vault)], check=True)
        ms._git(zh_vault, "add", "-A")
        ms._git(zh_vault, "commit", "-m", "init zh vault", check_identity=True)

        prop = ms.create_proposal(
            self.db,
            zh_vault,
            scope="个人偏好",
            title="中文回复",
            body="默认使用简体中文。",
            quote="以后都用中文回复。",
        )
        pub = ms.publish_proposals(self.db, zh_vault, [prop["id"]])
        traced = mr.VaultReader(zh_vault).trace_card(pub["cards"][0]["id"])
        self.assertEqual(traced["events"][0]["path"], "来源记录.md")

    def test_local_web_server_csrf_and_rpc(self):
        fake_ui = self.base / "ui"
        fake_ui.mkdir()
        (fake_ui / "app.js").write_text("console.log('ok');", encoding="utf-8")
        (fake_ui / "app.css").write_text("body{}", encoding="utf-8")

        srv, token = web_server.create_server(
            host="127.0.0.1",
            port=0,
            vault_path=self.vault,
            data_dir=self.data_dir,
            ui_dir=fake_ui,
        )
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            base_url = f"http://127.0.0.1:{srv.server_address[1]}"
            html = urllib.request.urlopen(base_url + "/").read().decode("utf-8")
            self.assertIn('name="memorys-web"', html)
            self.assertIn(token, html)

            # Reject missing token
            bad_req = urllib.request.Request(
                base_url + "/api/rpc",
                data=json.dumps({"action": "snapshot", "args": {}}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(bad_req)
            self.assertEqual(ctx.exception.code, 403)

            # Accept valid token
            good_req = urllib.request.Request(
                base_url + "/api/rpc",
                data=json.dumps({"action": "snapshot", "args": {}}).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-MemoryS-Token": token,
                },
            )
            reply = json.loads(urllib.request.urlopen(good_req).read().decode("utf-8"))
            self.assertTrue(reply["ok"])
            self.assertEqual(reply["data"]["runtime_mode"], "web")
            self.assertEqual(reply["data"]["vault_path"], str(self.vault))
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
