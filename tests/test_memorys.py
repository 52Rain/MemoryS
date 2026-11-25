"""Cross-platform unit & integration tests for MemoryS."""
from __future__ import annotations

import contextlib
from datetime import date
import io
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import sqlite3
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[1]
from runtime import config, mcp_server, memory_cli, web_server
from runtime import memory_reader as mr, memory_store as ms, service_backend as b
from runtime.recall_hook import recall


@contextlib.contextmanager
def skip_if_bind_denied():
    """Keep listener tests live everywhere the OS permits a socket bind."""
    bind = socket.socket.bind
    def bind_loopback(sock, address):
        try:
            return bind(sock, address)
        except PermissionError as exc:
            raise unittest.SkipTest(f"Loopback bind denied: {exc}") from exc
    with patch.object(socket.socket, "bind", bind_loopback):
        yield


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

        self.env = patch.dict(os.environ, {"MEMORYS_CONFIG_DIR": str(self.base / "config"),
                                           "MEMORYS_APP_DATA": str(self.data_dir)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
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

    def test_canonical_shared_scope_search(self):
        demo = ms.init_vault(self.base / "demo", with_demo=True)
        (demo / "notes/domain/glossary.md").write_text("Domain glossary.", encoding="utf-8")
        reader = mr.VaultReader(demo)
        for scope, card_id in (("shared/preferences", "p-concise-responses"),
                               ("shared/methods", "m-adr-before-refactor"),
                               ("shared/domain", "glossary")):
            with self.subTest(scope=scope):
                hits = reader.search(scope=scope)
                self.assertEqual([(card["scope"], card["id"]) for card in hits], [(scope, card_id)])

        result = mcp_server.handle_tool_call("memory_search", {"scope": "shared/preferences"}, vault_path=demo)
        self.assertEqual(result, {"count": 1, "cards": reader.search(scope="shared/preferences")})

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

    def test_stdio_mcp(self):
        p1 = self.call(
            "createProposal",
            scope="acme-core",
            title="Customer ID prefix",
            body="All customer IDs start with CUST-.",
            effective_from=date.today().isoformat(),
        )["id"]
        self.call("publish", ids=[p1])

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

    def test_live_ai_conflict_check(self):
        with skip_if_bind_denied():
            server = HTTPServer(("127.0.0.1", 0), ConflictCheckFixture)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            cfg = dict(b.DEFAULT, url=f"http://127.0.0.1:{server.server_port}")
            self.call("saveConfig", **cfg)
            self.assertTrue(self.call("test", **cfg)["ok"])
            proposal = self.draft()
            self.assertIn("superseding", self.call("reviewProposal", id=proposal)["opinion"])
        finally:
            server.shutdown()
            server.server_close()

    def test_local_web_server_csrf_and_rpc(self):
        fake_ui = self.base / "ui"
        fake_ui.mkdir()
        (fake_ui / "app.js").write_text("console.log('ok');", encoding="utf-8")
        (fake_ui / "app.css").write_text("body{}", encoding="utf-8")

        with skip_if_bind_denied():
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


    def draft(self, vault=None, **updates):
        args = dict(scope="shared/preferences", title="Response format", body="Lead with the conclusion.")
        args.update(updates)
        return ms.create_proposal(self.db, vault or self.vault, **args)["id"]

    def contents(self):
        return {p.relative_to(self.vault): p.read_bytes() if p.is_file() else None
                for p in self.vault.rglob("*") if ".git" not in p.parts}

    def test_init_tracks_only_new_files(self):
        target = self.base / "mixed"
        subprocess.run([ms.find_git(), "init", "-q", str(target)], check=True)
        for name in ("staged", "untracked", "START.md"):
            (target / name).write_text(name)
        ms._git(target, "add", "staged")
        ms.init_vault(target, with_demo=True)
        files = ms._git(target, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
        self.assertIn("SOURCES.md", files)
        self.assertTrue(set(files).isdisjoint({"staged", "untracked", "START.md"}))
        self.assertEqual(ms._git(target, "diff", "--cached", "--name-only"), "staged")
        self.assertEqual((target / "START.md").read_text(), "START.md")

    def test_source_related_and_unrelated_parent_refused_before_creation(self):
        for path in (REPO_ROOT, REPO_ROOT.parent, REPO_ROOT / "runtime" / "temp-vault"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "memorys-vault"):
                ms.init_vault(path)
        unrelated = self.base / "other-repo"
        subprocess.run([ms.find_git(), "init", "-q", str(unrelated)], check=True)
        child = unrelated / "new" / "vault"
        with self.assertRaisesRegex(ValueError, "unrelated Git"):
            ms.init_vault(child)
        self.assertFalse(child.parent.exists())

    def test_set_path_and_demo_resources(self):
        with patch.object(ms, "init_vault", side_effect=AssertionError):
            self.assertEqual(self.call("setVaultPath", path=str(self.vault), with_demo=True)["vault_path"],
                             str(self.vault))
        other = self.base / "other"
        other.mkdir()
        (other / "personal.txt").touch()
        with self.assertRaises(ValueError):
            self.call("setVaultPath", path=str(other))
        self.assertEqual(len(list(other.iterdir())), 1)
        demo = self.base / "new-vault"
        with patch.object(ms, "__file__", str(self.base / "runtime" / "memory_store.py")):
            with self.assertRaises(FileNotFoundError):
                ms.init_vault(demo, with_demo=True)
        self.assertFalse(demo.exists())
        self.call("setVaultPath", path=str(demo), with_demo=True)
        self.assertEqual(len(mr.VaultReader(demo).cards), 4)
        template = REPO_ROOT / "examples" / "demo-vault"
        resources = REPO_ROOT / "runtime" / "demo_vault"
        def files(root):
            return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        self.assertEqual(len(files(resources)), 9)
        self.assertEqual(files(resources), files(template))
        self.assertEqual((REPO_ROOT / "runtime/ui_index.html").read_bytes(),
                         (REPO_ROOT / "desktop-app/public/index.html").read_bytes())

    def test_default_and_legacy_vault_resolution(self):
        with patch.object(Path, "home", return_value=self.home), patch.dict(os.environ, {
            "MEMORYS_VAULT": "", "MEMORYS_PROJECT_ROOT": ""}):
            self.assertEqual(config.resolve_vault(read_fallback=False), self.home / "memorys-vault")
            self.assertEqual(config.resolve_vault(), REPO_ROOT / "examples/demo-vault")
            (self.home / "MemoryS" / "notes").mkdir(parents=True)
            self.assertEqual(config.resolve_vault(), self.home / "MemoryS")
            (self.home / "memorys-vault" / "projects").mkdir(parents=True)
            self.assertEqual(config.resolve_vault(), self.home / "memorys-vault")

    def test_vault_ownership_and_legacy_states(self):
        other = ms.init_vault(self.base / "second")
        pid = self.draft()
        self.assertEqual(self.db.execute("SELECT vault_path FROM proposals").fetchone()[0], str(self.vault))
        self.assertEqual(ms.list_proposals(self.db, mr.VaultReader(other)), [])
        with patch.object(b, "model_call", side_effect=AssertionError):
            for action, args in (("edit", dict(id=pid)), ("ignore", dict(id=pid)),
                                 ("review", dict(id=pid)), ("publish", dict(ids=[pid]))):
                with self.subTest(action=action), self.assertRaisesRegex(ValueError, "different vault"):
                    b.dispatch(self.db, action, args, vault=other)
        with self.db:
            self.db.execute("UPDATE proposals SET vault_path='', state='待确认'")
        self.assertEqual(ms.list_proposals(self.db, mr.VaultReader(other))[0]["state"], "pending")
        for state in ("已发布", "已忽略", "受限"):
            with self.db:
                self.db.execute("UPDATE proposals SET state=?", (state,))
            self.assertEqual(ms.list_proposals(self.db, mr.VaultReader(other)), [])
            with self.assertRaises(ValueError):
                ms.publish_proposals(self.db, other, [pid])

    def test_legacy_database_migrates(self):
        old = self.base / "legacy-db"
        old.mkdir()
        with contextlib.closing(sqlite3.connect(old / "workspace.sqlite3")) as db, db:
            db.execute("CREATE TABLE proposals (id TEXT PRIMARY KEY, state TEXT)")
            db.execute("INSERT INTO proposals VALUES ('legacy','待确认')")
        with contextlib.closing(ms.connect_db(old)) as db:
            self.assertEqual(db.execute("SELECT vault_path FROM proposals").fetchone()[0], "")

    def test_batch_validation_and_revision_conflicts(self):
        target = self.call("publish", ids=[self.draft()])["cards"][0]["id"]
        revision = self.draft(supersedes=target)
        competitor = self.draft(supersedes=target)
        new = self.draft(scope="project/new:Example", quote="Evidence")
        before = self.contents()
        for ids, error in (([new, "missing"], "state"), ([revision, competitor], "same target")):
            with self.assertRaisesRegex(ValueError, error):
                self.call("publish", ids=ids)
            self.assertEqual(self.contents(), before)
        path = mr.VaultReader(self.vault).cards[target].path
        path.write_text(path.read_text(encoding="utf-8") + "Amended body.", encoding="utf-8")
        before = self.contents()
        with self.assertRaisesRegex(ValueError, "Target card changed since draft creation"):
            self.call("publish", ids=[new, revision])
        self.assertEqual(self.contents(), before)
        self.call("archiveCard", id=target)
        with self.assertRaisesRegex(ValueError, "active card"):
            self.call("publish", ids=[revision])

    def test_batch_rollback_on_write_and_git_failure(self):
        ids = [self.draft(scope="project/new:New project", quote="First evidence"),
               self.draft(scope="shared/methods", quote="Second evidence")]
        sources = self.vault / "SOURCES.md"
        sources.write_bytes(sources.read_bytes() + b"staged\r\n")
        ms._git(self.vault, "add", "--", "SOURCES.md")
        sources.write_bytes(sources.read_bytes() + b"unstaged\r\n")
        before = self.contents()
        index = ms._git(self.vault, "ls-files", "--stage")
        rev = ms._git(self.vault, "rev-parse", "HEAD")
        write = Path.write_text
        git = ms._git
        def fail_write(path, *args, **kwargs):
            if path.parent.name == "methods":
                path.write_bytes(b"partial")
                raise OSError("write rejected")
            return write(path, *args, **kwargs)
        def fail_commit(repo, *args, **kwargs):
            if "commit" in args:
                raise OSError("commit rejected")
            return git(repo, *args, **kwargs)
        for failure in (patch.object(Path, "write_text", fail_write), patch.object(ms, "_git", fail_commit)):
            with failure, self.assertRaisesRegex(OSError, "rejected"):
                self.call("publish", ids=ids)
            self.assertEqual(self.contents(), before)
            self.assertEqual(ms._git(self.vault, "ls-files", "--stage"), index)
            self.assertEqual(ms._git(self.vault, "rev-parse", "HEAD"), rev)
            self.assertEqual({r["id"] for r in self.call("snapshot")["proposals"]}, set(ids))
        self.assertEqual(self.call("publish", ids=ids)["count"], 2)

    def test_fields_checked_on_create_edit_publish(self):
        pid = self.draft()
        for field in ("title", "body", "quote", "scope", "use_when", "client"):
            for value in ("Please do not record", "sk-" + "X" * 20):
                with self.subTest(field=field), self.assertRaises(ValueError):
                    self.draft(**{field: value})
        for field, value in (("use_when", ["x" * 101]), ("client", "x" * 81)):
            with self.assertRaises(ValueError):
                self.draft(**{field: value})
        with self.assertRaises(ValueError):
            self.call("edit", id=pid, scope="shared/preferences", title="Updated", body="Body", use_when=["禁止记录"])
        with self.db:
            self.db.execute("UPDATE proposals SET client='不要记录'")
        with self.assertRaises(ValueError):
            self.call("publish", ids=[pid])

    def test_strict_date_and_hook_cli(self):
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            mr.VaultReader(self.vault).search(as_of="20260501")
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            self.draft(effective_from="20260501")
        demo = ms.init_vault(self.base / "demo", with_demo=True)
        proc = subprocess.run([sys.executable, "-B", str(REPO_ROOT / "runtime/recall_hook.py"),
                               "--vault", str(demo)], input='{"prompt":"Billing"}', text=True,
                              env=dict(os.environ, MEMORYS_VAULT=str(self.vault)), capture_output=True, check=True)
        self.assertIn("Lead with conclusions", json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_project_ids_and_symlink_boundaries(self):
        for value in ("project/../bad", r"project/a\b", "project/a/b"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.draft(scope=value)
        start = self.vault / "projects" / "sample" / "START.md"
        start.parent.mkdir()
        start.write_text("---\nproject_id: ../bad\n---")
        with self.assertRaisesRegex(ValueError, "project ID"):
            mr.VaultReader(self.vault)
        start.unlink()
        outside = self.base / "outside"
        outside.mkdir()
        folder = self.vault / "notes" / "methods"
        folder.rmdir()
        try:
            folder.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Symlink unavailable: {exc}")
        with self.assertRaisesRegex(ValueError, "inside the vault"):
            self.call("publish", ids=[self.draft(scope="shared/methods")])
        self.assertEqual(list(outside.iterdir()), [])

    def test_codex_windows_paths_and_json_integrity(self):
        codex = self.base / "codex-home"
        win = r"C:\Users\alice\MemoryS Tools"
        paths = (win + r"\python.exe", win + r"\mcp_server.py", win + r"\recall_hook.py")
        with patch.object(memory_cli, "_runtime_paths", return_value=paths), patch.dict(os.environ, {"CODEX_HOME":str(codex)}):
            with patch.object(memory_cli.os, "name", "nt"):
                _, hook = memory_cli._codex_snippets(win)
                self.assertEqual(hook["hooks"][0]["command"], subprocess.list2cmdline(
                    [paths[0], "-B", paths[2], "--vault", win]))
            for _ in range(2):
                memory_cli._connect_client("codex", win, True)
            toml = (codex / "config.toml").read_text()
            self.assertEqual(toml.count("[mcp_servers.memorys]"), 1)
            if sys.version_info >= (3, 11):
                import tomllib
                entry = tomllib.loads(toml)["mcp_servers"]["memorys"]
                self.assertEqual(entry["env"]["MEMORYS_VAULT"], win)
                self.assertEqual(entry["command"], paths[0])
        for client, filename in (("claude", ".claude/settings.json"), ("claude", ".claude.json"),
                                 ("cursor", ".cursor/mcp.json"), ("codex", ".codex/hooks.json")):
            bad = self.home / filename
            bad.parent.mkdir(exist_ok=True)
            bad.write_bytes(b"{broken")
            with self.assertRaises(ValueError):
                memory_cli._connect_client(client, self.vault, True, home=self.home)
            self.assertEqual(bad.read_bytes(), b"{broken")
            bad.unlink()
        hook_path = self.home / "hooks.json"
        foreign = {"command": "python /other/recall_hook.py"}
        hook_path.write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [foreign]}]}}))
        entry = memory_cli._claude_snippets(self.vault)[1]
        for _ in range(2):
            memory_cli._merge_hook_json(hook_path, entry)
        self.assertEqual(json.loads(hook_path.read_text())["hooks"]["UserPromptSubmit"], [{"hooks":[foreign]}, entry])

    def test_web_rpc_security_without_listener(self):
        # Exercise the real HTTP parser and RPC dispatcher with an in-memory connection.
        with self.assertRaisesRegex(ValueError, "loopback"):
            web_server.create_server(host="0.0.0.0")
        ui = self.base / "static"
        ui.mkdir()
        (ui / "app.js").write_text("fixture")
        (ui / "app.css").touch()
        with patch.dict(os.environ, {"MEMORYS_UI_DIR": str(ui)}):
            self.assertEqual(web_server._find_ui_dir(), ui)
            self.assertEqual(web_server._find_ui_dir(REPO_ROOT / "runtime/ui"), REPO_ROOT / "runtime/ui")
        with patch.object(web_server, "_REPO_ROOT", self.base), patch.object(web_server.ThreadingHTTPServer, "server_bind"), patch.object(
                web_server.ThreadingHTTPServer, "server_activate"):
            server, token = web_server.create_server(vault_path=self.vault, data_dir=self.data_dir, ui_dir=ui)
        self.addCleanup(server.server_close)
        server.server_port = 8765
        def request(action=None, args=None, host="localhost:8765", origin=None, path="/"):
            stream = io.BytesIO()
            connection = Mock()
            method = "POST" if action else "GET"
            payload = json.dumps({"action": action, "args": args or {}}).encode() if action else b""
            route = "/api/rpc" if action else path
            headers = [f"{method} {route} HTTP/1.0", f"Host: {host}", f"X-MemoryS-Token: {token}",
                       f"Content-Length: {len(payload)}"]
            if origin is not None:
                headers.append("Origin: " + origin)
            connection.makefile.return_value = io.BytesIO(("\r\n".join(headers) + "\r\n\r\n").encode() + payload)
            connection.sendall.side_effect = stream.write
            server.RequestHandlerClass(connection, ("127.0.0.1", 9000), server)
            header, data = stream.getvalue().split(b"\r\n\r\n", 1)
            return int(header.split()[1]), data
        for action in (None, "snapshot"):
            self.assertEqual(request(action, host="example.org")[0], 403)
        for origin in ("http://localhost:8766", "https://localhost:8765", "http://example.org:8765", ""):
            self.assertEqual(request("snapshot", origin=origin)[0], 403)
        self.assertEqual(request("snapshot", origin="http://localhost:8765")[0], 200)
        self.assertIn(token.encode(), request()[1])
        self.assertEqual(request(path="/app.js"), (200, b"fixture"))
        self.assertIn(b"outside", request("openExternal", {"path": str(self.base)})[1])
        saved = dict(b.DEFAULT, url="https://saved.example/v1")
        other = dict(saved, url="https://new.example/v1")
        self.call("saveConfig", **saved)
        with patch.dict(os.environ, {"MEMORYS_API_KEY":"fixture-key", "MEMORYS_API_URL":""}), patch.object(
                b, "model_call", return_value="OK") as model:
            request("test", other)
            self.assertEqual(model.call_args.args[1], "")
            request("test", saved)
            self.assertEqual(model.call_args.args[1], "fixture-key")
            with patch.dict(os.environ, {"MEMORYS_API_URL":other["url"]}):
                request("test", saved)
                self.assertEqual(model.call_args.args[1], "")
                request("test", other)
                self.assertEqual(model.call_args.args[1], "fixture-key")

    def test_review_proposal_key_precedence(self):
        pid = self.draft()
        lookup = Mock(return_value="fallback-key")
        with patch.object(b, "model_call", return_value="Reviewed") as model:
            b.dispatch(self.db, "reviewProposal", {"id": pid}, key="explicit-key",
                       vault=self.vault, key_for_url=lookup)
            self.assertEqual(model.call_args.args[1], "explicit-key")
            lookup.assert_not_called()

            b.dispatch(self.db, "reviewProposal", {"id": pid}, key="",
                       vault=self.vault, key_for_url=lookup)
            self.assertEqual(model.call_args.args[1], "fallback-key")
            lookup.assert_called_once_with(self.db, b.DEFAULT["url"])

    def test_model_call_unlocked_and_stale_opinion_discarded(self):
        pid = self.draft()
        held = False
        @contextlib.contextmanager
        def lock():
            nonlocal held
            self.assertFalse(held)
            held = True
            try:
                yield
            finally:
                held = False
        def respond(*_):
            self.assertFalse(held)
            return "Reviewed"
        with patch.object(b, "model_call", side_effect=respond):
            for action, args in (("test", b.DEFAULT), ("reviewProposal", {"id":pid})):
                b.dispatch(self.db, action, args, vault=self.vault, lock=lock)
        def edit_during_request(*_):
            self.call("edit", id=pid, scope="shared/preferences", title="Changed", body="New text")
            return "Old opinion"
        with patch.object(b, "model_call", side_effect=edit_during_request), self.assertRaisesRegex(ValueError, "changed"):
            b.dispatch(self.db, "reviewProposal", {"id":pid}, vault=self.vault, lock=lock)
        self.assertEqual(self.db.execute("SELECT opinion FROM proposals").fetchone()[0], "")


if __name__ == "__main__":
    unittest.main()
