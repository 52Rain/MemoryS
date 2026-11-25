"""Read cards, project configs, and client integration status from a MemoryS Vault."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

if __package__:
    from .config import find_git, is_memorys_hook, resolve_vault
    from .memory_reader import SCOPE_LABELS, VaultReader
else:  # Flat service directory in the native app bundle
    from config import find_git, is_memorys_hook, resolve_vault
    from memory_reader import SCOPE_LABELS, VaultReader


KIND_LABELS = {
    "decision": "Decision",
    "preference": "Preference",
    "method": "Method",
    "domain": "Domain",
}

STATUS_LABELS = {
    "active": "Active",
    "superseded": "Superseded",
    "archived": "Archived",
    "disputed": "Disputed",
}


def _git_short_versions(vault: Path) -> dict[str, str]:
    """Fetch last commit short hash for files under the vault in one git call."""
    versions: dict[str, str] = {}
    git_bin = find_git()
    proc = subprocess.run(
        [git_bin, "-C", str(vault), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return versions
    repo_root = Path(proc.stdout.strip()).resolve()
    try:
        rel_vault = vault.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return versions

    cmd = [
        git_bin,
        "-C",
        str(repo_root),
        "log",
        "--name-only",
        "--format=COMMIT:%h",
        "-n",
        "80",
    ]
    if rel_vault and rel_vault != ".":
        cmd.extend(["--", rel_vault])

    log_proc = subprocess.run(cmd, capture_output=True, text=True)
    if log_proc.returncode != 0:
        return versions
    current_hash = ""
    for line in log_proc.stdout.splitlines():
        if line.startswith("COMMIT:"):
            current_hash = line.split(":", 1)[1].strip()
        elif line.strip() and current_hash:
            versions.setdefault(line.strip(), current_hash)
    return versions


def load_vault_snapshot(vault_path: Path | None = None) -> dict:
    vault = resolve_vault(vault_path)
    reader = VaultReader(vault)
    events = reader.load_events()
    versions = _git_short_versions(vault)

    repo_prefix = vault.name + "/"
    cards = []
    for card in reader.cards.values():
        quotes = []
        for ref in card.source_refs:
            eid = ref.split(":", 1)[1] if ref.startswith("event:") else ref
            ev = events.get(eid)
            if ev and ev.get("text"):
                quotes.append(
                    {
                        "event_id": eid,
                        "actor": ev.get("actor", ""),
                        "at": ev.get("at", ""),
                        "location": ev.get("location", ""),
                        "text": ev.get("text", ""),
                    }
                )
        applies_today = card.applies(None, reader.cards)
        rel_from_repo = repo_prefix + card.relpath
        version = versions.get(card.relpath) or versions.get(rel_from_repo) or "working-tree"
        cards.append(
            {
                "id": card.card_id,
                "title": card.summary,
                "summary": card.summary,
                "kind": card.kind,
                "kind_label": KIND_LABELS.get(card.kind, card.kind),
                "scope": card.scope,
                "scope_label": reader.scope_label(card.scope),
                "project": card.project_title or SCOPE_LABELS.get(card.scope, "Shared"),
                "project_id": (
                    card.scope.split("/", 1)[1] if card.scope.startswith("project/") else ""
                ),
                "status": card.status,
                "status_label": STATUS_LABELS.get(card.status, card.status),
                "applies_today": applies_today,
                "effective_from": card.effective_from,
                "effective_until": card.effective_until,
                "supersedes": card.supersedes,
                "superseded_by": card.superseded_by,
                "use_when": card.use_when,
                "source_refs": card.source_refs,
                "quote": "\n".join(q["text"] for q in quotes),
                "events": quotes,
                "body": card.body,
                "date": card.effective_from,
                "source": card.relpath,
                "abs_path": str(card.path),
                "version": version,
            }
        )

    cards.sort(key=lambda c: (c["status"] == "active", c["effective_from"], c["title"]), reverse=True)

    projects = []
    for info in sorted(reader.projects.values(), key=lambda p: (p.title.casefold(), p.project_id)):
        projects.append(
            {
                "project_id": info.project_id,
                "title": info.title,
                "scope": info.scope,
                "aliases": info.aliases,
                "paths": info.paths,
                "start_path": str(info.start_path),
            }
        )

    active_count = sum(1 for c in cards if c["status"] == "active")
    return {
        "vault_path": str(vault),
        "cards": cards,
        "projects": projects,
        "stats": {
            "total": len(cards),
            "active": active_count,
            "history": len(cards) - active_count,
            "projects": len(projects),
        },
    }


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _has_recall_hook(path: Path) -> bool:
    raw = _read_text(path)
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    entries = data.get("hooks", {}).get("UserPromptSubmit", [])
    for group in entries:
        for h in group.get("hooks", []):
            cmd = str(h.get("command", ""))
            if is_memorys_hook(cmd):
                return True
    return False


def _json_has_mcp(path: Path) -> bool:
    raw = _read_text(path)
    if not raw:
        return False
    try:
        return "memorys" in (json.loads(raw).get("mcpServers") or {})
    except json.JSONDecodeError:
        return False


def client_integration_status(home: Path | None = None) -> dict:
    """Read-only inspection of Claude Code, Codex, and Cursor MCP/Hook status."""
    h = home or Path.home()
    codex_dir = h / ".codex"
    if h == Path.home() and os.environ.get("CODEX_HOME"):
        codex_dir = Path(os.environ["CODEX_HOME"]).expanduser()
    codex_toml = _read_text(codex_dir / "config.toml")

    claude_mcp = _json_has_mcp(h / ".claude.json")
    codex_mcp = bool(re.search(r"^\[mcp_servers\.memorys\]", codex_toml, re.M))
    cursor_mcp = _json_has_mcp(h / ".cursor" / "mcp.json")
    codex_hook = _has_recall_hook(codex_dir / "hooks.json")
    claude_hook = _has_recall_hook(h / ".claude" / "settings.json")

    return {
        "claude": {"mcp": claude_mcp, "hook": claude_hook},
        "codex": {"mcp": codex_mcp, "hook": codex_hook},
        "cursor": {"mcp": cursor_mcp, "hook": False},
    }
