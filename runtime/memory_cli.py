"""CLI entrypoint for MemoryS (init, connect, ui, status, context, search, read, trace, propose)."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
import re
import shutil
import sys

_RUNTIME = Path(__file__).resolve().parent
if str(_RUNTIME) not in sys.path:
    sys.path.insert(0, str(_RUNTIME))

try:
    from runtime.config import app_data_dir, config_file, is_memorys_hook, load_user_config, resolve_vault, save_user_config
    from runtime.memory_reader import VaultReader
    from runtime import memory_store as store
except ImportError:
    from config import app_data_dir, config_file, is_memorys_hook, load_user_config, resolve_vault, save_user_config
    from memory_reader import VaultReader
    import memory_store as store


def _runtime_paths() -> tuple[str, str, str]:
    py = sys.executable or "python3"
    mcp_script = str((_RUNTIME / "mcp_server.py").resolve())
    hook_script = str((_RUNTIME / "recall_hook.py").resolve())
    return py, mcp_script, hook_script


def _hook_command(py: str, script: str, vault: Path) -> str:
    args = [py, "-B", script, "--vault", str(vault)]
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


def _claude_snippets(vault: Path) -> tuple[dict, dict]:
    py, mcp_script, hook_script = _runtime_paths()
    mcp_entry = {
        "command": py,
        "args": ["-B", mcp_script],
        "env": {"MEMORYS_VAULT": str(vault)},
    }
    hook_cmd = _hook_command(py, hook_script, vault)
    hook_entry = {
        "hooks": [
            {
                "type": "command",
                "command": hook_cmd,
                "timeout": 5,
            }
        ]
    }
    return mcp_entry, hook_entry


def _codex_snippets(vault: Path) -> tuple[str, dict]:
    py, mcp_script, hook_script = _runtime_paths()
    toml_block = (
        "\n[mcp_servers.memorys]\n"
        f'command = {json.dumps(py, ensure_ascii=False)}\n'
        f'args = ["-B", {json.dumps(mcp_script, ensure_ascii=False)}]\n'
        f'env = {{ MEMORYS_VAULT = {json.dumps(str(vault), ensure_ascii=False)} }}\n'
    )
    hook_cmd = _hook_command(py, hook_script, vault)
    hook_entry = {
        "hooks": [
            {
                "type": "command",
                "command": hook_cmd,
                "timeout": 5,
            }
        ]
    }
    return toml_block, hook_entry


def _cursor_snippet(vault: Path) -> dict:
    py, mcp_script, _hook = _runtime_paths()
    return {
        "mcpServers": {
            "memorys": {
                "command": py,
                "args": ["-B", mcp_script],
                "env": {"MEMORYS_VAULT": str(vault)},
            }
        }
    }


def _backup_if_exists(path: Path) -> None:
    if path.is_file():
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        temp_path = Path(tmp.name)
    try:
        temp_path.write_text(text, encoding="utf-8")
        _backup_if_exists(path)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _json_object(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    return data


def _json_text(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _merged_hooks(path: Path, hook_entry: dict) -> str:
    data = _json_object(path)
    hooks = data.setdefault("hooks", {})
    entries = hooks.setdefault("UserPromptSubmit", [])
    # Remove any existing memorys recall_hook entry to stay idempotent
    filtered = []
    for group in entries:
        sub = [
            h
            for h in group.get("hooks", [])
            if not is_memorys_hook(str(h.get("command", "")))
        ]
        if len(sub) == len(group.get("hooks", [])):
            filtered.append(group)
        elif sub:
            new_group = dict(group)
            new_group["hooks"] = sub
            filtered.append(new_group)
    filtered.append(hook_entry)
    hooks["UserPromptSubmit"] = filtered
    return _json_text(data)


def _merge_hook_json(path: Path, hook_entry: dict) -> None:
    _atomic_write(path, _merged_hooks(path, hook_entry))


def _connect_client(client: str, vault: Path, apply: bool, home: Path | None = None) -> int:
    h = home or Path.home()
    if client == "claude":
        mcp_entry, hook_entry = _claude_snippets(vault)
        if not apply:
            print("# Add to ~/.claude.json under mcpServers.memorys:")
            print(json.dumps({"mcpServers": {"memorys": mcp_entry}}, ensure_ascii=False, indent=2))
            print("\n# Add to ~/.claude/settings.json under hooks.UserPromptSubmit:")
            print(json.dumps({"hooks": {"UserPromptSubmit": [hook_entry]}}, ensure_ascii=False, indent=2))
            print("\nRun with --apply to write these files automatically (with .bak backups).")
            return 0
        claude_json = h / ".claude.json"
        data = _json_object(claude_json)
        data.setdefault("mcpServers", {})["memorys"] = mcp_entry
        hook_path = h / ".claude" / "settings.json"
        hook_text = _merged_hooks(hook_path, hook_entry)
        _atomic_write(claude_json, _json_text(data))
        _atomic_write(hook_path, hook_text)
        print(f"Connected Claude Code -> {vault}")
        print(f"  MCP:  {claude_json}")
        print(f"  Hook: {h / '.claude' / 'settings.json'}")
        return 0

    if client == "codex":
        toml_block, hook_entry = _codex_snippets(vault)
        if not apply:
            print("# Add to ~/.codex/config.toml:")
            print(toml_block.strip())
            print("\n# Add to ~/.codex/hooks.json under hooks.UserPromptSubmit:")
            print(json.dumps({"hooks": {"UserPromptSubmit": [hook_entry]}}, ensure_ascii=False, indent=2))
            print("\nRun with --apply to write these files automatically (with .bak backups).")
            return 0
        codex_dir = h / ".codex"
        if home is None and os.environ.get("CODEX_HOME"):
            codex_dir = Path(os.environ["CODEX_HOME"]).expanduser()
        codex_toml = codex_dir / "config.toml"
        codex_toml.parent.mkdir(parents=True, exist_ok=True)
        existing = ""
        if codex_toml.is_file():
            existing = codex_toml.read_text(encoding="utf-8")
        if re.search(r"^\[mcp_servers\.memorys\]", existing, re.M):
            updated = re.sub(
                r"^\[mcp_servers\.memorys\][\s\S]*?(?=^\[|\Z)",
                lambda _m: toml_block.lstrip() + "\n",
                existing,
                flags=re.M,
            )
        else:
            suffix = "" if (not existing or existing.endswith("\n")) else "\n"
            updated = existing + suffix + toml_block
        hook_path = codex_dir / "hooks.json"
        hook_text = _merged_hooks(hook_path, hook_entry)
        _atomic_write(codex_toml, updated)
        _atomic_write(hook_path, hook_text)
        print(f"Connected Codex -> {vault}")
        print(f"  MCP:  {codex_toml}")
        print(f"  Hook: {hook_path}")
        return 0

    if client == "cursor":
        snippet = _cursor_snippet(vault)
        if not apply:
            print("# Add to ~/.cursor/mcp.json:")
            print(json.dumps(snippet, ensure_ascii=False, indent=2))
            print("\nRun with --apply to write ~/.cursor/mcp.json automatically.")
            return 0
        cursor_json = h / ".cursor" / "mcp.json"
        cursor_json.parent.mkdir(parents=True, exist_ok=True)
        data = _json_object(cursor_json)
        data.setdefault("mcpServers", {})["memorys"] = snippet["mcpServers"]["memorys"]
        _atomic_write(cursor_json, _json_text(data))
        print(f"Connected Cursor -> {vault}")
        print(f"  MCP: {cursor_json}")
        return 0

    raise ValueError(f"Unsupported client: {client}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mem",
        description="MemoryS — Local-first, human-governed memory system for AI agents",
    )
    parser.add_argument("--vault", default="", help="Override Vault path (defaults to config/MEMORYS_VAULT)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Initialize a new Git-backed MemoryS Vault")
    p_init.add_argument("path", nargs="?", default="~/memorys-vault", help="Target Vault directory (default: ~/memorys-vault)")
    p_init.add_argument("--demo", action="store_true", help="Populate with sample cards demonstrating temporal supersession")

    p_connect = sub.add_parser("connect", help="Print or apply MCP & Hook config for an AI client")
    p_connect.add_argument("client", choices=["claude", "codex", "cursor"], help="Target client")
    p_connect.add_argument("--apply", action="store_true", help="Write configuration to client config files (creates .bak backup)")

    p_ui = sub.add_parser("ui", help="Launch local web review & library console (cross-platform)")
    p_ui.add_argument("--ui-dir", type=Path, help="Directory with built app.js and app.css")
    p_ui.add_argument("--port", type=int, default=8765, help="Localhost port (default: 8765)")
    p_ui.add_argument("--no-open", action="store_true", help="Do not open browser automatically")

    sub.add_parser("status", help="Show active vault path, card counts, and pending proposals")

    p_ctx = sub.add_parser("context", help="Load project handoff and active memories for a task")
    p_ctx.add_argument("--project", default="")
    p_ctx.add_argument("--task", default="")
    p_ctx.add_argument("--as-of", dest="as_of", default="")

    p_search = sub.add_parser("search", help="Search memories by project, scope, keyword, or date")
    p_search.add_argument("--project", default="")
    p_search.add_argument("--scope", default="")
    p_search.add_argument("--as-of", dest="as_of", default="")
    p_search.add_argument("--include-history", action="store_true")
    p_search.add_argument("query", nargs="*")

    p_read = sub.add_parser("read", help="Read a memory card or project handoff")
    p_read.add_argument("--project", default="")
    p_read.add_argument("card_id")

    p_trace = sub.add_parser("trace", help="Trace provenance quotes for a memory card")
    p_trace.add_argument("--project", default="")
    p_trace.add_argument("card_id")

    p_propose = sub.add_parser("propose", help="Queue a draft memory proposal for human review")
    p_propose.add_argument("--scope", required=True)
    p_propose.add_argument("--title", required=True)
    p_propose.add_argument("--body", required=True)
    p_propose.add_argument("--effective-from", dest="effective_from", default="")
    p_propose.add_argument("--supersedes", default="")
    p_propose.add_argument("--quote", default="")
    p_propose.add_argument("--use-when", dest="use_when", default="")
    p_propose.add_argument("--client", default="CLI")

    args = parser.parse_args(argv)

    if args.command == "init":
        vault = store.init_vault(args.path, with_demo=args.demo)
        cfg_path = save_user_config({"vault_path": str(vault)})
        reader = VaultReader(vault)
        print(f"Initialized MemoryS Vault at: {vault}")
        print(f"Saved config to: {cfg_path}")
        print(f"Cards: {len(reader.cards)} | Projects: {len(reader.projects)}")
        print("\nNext steps:")
        print("  1. Connect your agent:  mem connect claude --apply   (or codex / cursor)")
        print("  2. Open review UI:      mem ui")
        return 0

    vault = resolve_vault(args.vault or None, read_fallback=args.command in {"status", "search", "context", "read", "trace"})

    if args.command == "connect":
        return _connect_client(args.client, vault, apply=args.apply)

    if args.command == "ui":
        if not __package__:
            sys.path.insert(0, str(_RUNTIME.parent))
        from runtime.web_server import serve_web_ui
        return serve_web_ui(port=args.port, vault_path=vault, open_browser=not args.no_open, ui_dir=args.ui_dir)

    if args.command == "status":
        reader = VaultReader(vault) if vault.is_dir() else None
        data_dir = app_data_dir()
        pending = 0
        if (data_dir / "workspace.sqlite3").is_file() and reader is not None:
            with contextlib.closing(store.connect_db(data_dir)) as db:
                pending = len(store.list_proposals(db, reader))
        out = {
            "vault_path": str(vault),
            "vault_exists": vault.is_dir(),
            "config_file": str(config_file()),
            "app_data_dir": str(data_dir),
            "cards_total": len(reader.cards) if reader else 0,
            "cards_active": sum(1 for c in reader.cards.values() if c.status == "active") if reader else 0,
            "projects": len(reader.projects) if reader else 0,
            "pending_proposals": pending,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if args.command == "context":
        reader = VaultReader(vault)
        info = reader.resolve_project(args.project) if args.project else None
        handoff = reader.handoff(info.project_id) if info else None
        candidates = reader.search(
            query=args.task,
            project=args.project,
            as_of=args.as_of or None,
        )
        out = {"project": info.project_id if info else args.project, "handoff": handoff, "candidates": candidates}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if args.command == "search":
        reader = VaultReader(vault)
        query = " ".join(args.query).strip()
        items = reader.search(
            query=query,
            project=args.project,
            scope=args.scope,
            as_of=args.as_of or None,
            include_history=args.include_history,
        )
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 0

    if args.command == "read":
        reader = VaultReader(vault)
        data = reader.read_card(args.card_id, project=args.project)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if args.command == "trace":
        reader = VaultReader(vault)
        data = reader.trace_card(args.card_id)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if args.command == "propose":
        data_dir = store.app_data_dir()
        with contextlib.closing(store.connect_db(data_dir)) as db, store.workspace_lock(data_dir):
            res = store.create_proposal(
                db,
                vault,
                scope=args.scope,
                title=args.title,
                body=args.body,
                effective_from=args.effective_from,
                supersedes=args.supersedes,
                quote=args.quote,
                use_when=args.use_when,
                client=args.client,
            )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
