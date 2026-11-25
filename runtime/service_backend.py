"""Local Python backend shared by MemoryS.app (macOS) and the cross-platform Web UI."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

if __package__:
    from .config import app_data_dir, resolve_vault, save_user_config, workspace_lock
    from .service_library import client_integration_status, load_vault_snapshot
    from .memory_reader import VaultReader
    from . import memory_store as store
else:  # Flat service directory in the native app bundle
    from config import app_data_dir, resolve_vault, save_user_config, workspace_lock
    from service_library import client_integration_status, load_vault_snapshot
    from memory_reader import VaultReader
    import memory_store as store

HOME = Path.home()
BASE = app_data_dir()
DEFAULT = {
    "provider": "OpenAI Compatible",
    "url": "https://api.openai.com/v1",
    "model": "gpt-4o-mini",
}


def vault_dir() -> Path:
    return resolve_vault(read_fallback=False)


def connect(data_dir: Path | None = None) -> sqlite3.Connection:
    return store.connect_db(data_dir or BASE)


def get_setting(db: sqlite3.Connection, key: str, default):
    row = db.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_setting(db: sqlite3.Connection, key: str, value) -> None:
    db.execute(
        "INSERT OR REPLACE INTO settings VALUES(?,?)",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def config(value: dict) -> dict:
    c = {k: str(value.get(k, "")).strip() for k in DEFAULT}
    c["url"] = c["url"].rstrip("/")
    p = urlsplit(c["url"])
    if (
        not all(c.values())
        or not p.hostname
        or p.username
        or p.password
        or p.query
        or p.fragment
        or not (
            p.scheme == "https"
            or (p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1", "::1"))
        )
    ):
        raise ValueError(
            "Provide provider name, model ID, and a valid HTTPS API base URL (HTTP allowed for localhost)."
        )
    return c


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def model_call(c: dict, key: str, messages: list[dict]) -> str:
    c = config(c)
    url = c["url"] + "/chat/completions"
    data = json.dumps(
        dict(model=c["model"], messages=messages, stream=False), ensure_ascii=False
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=40) as res:
            raw = res.read(2_000_000)
        reply = json.loads(raw)["choices"][0]["message"]["content"]
        if not isinstance(reply, str) or not reply.strip():
            raise ValueError()
        return reply.strip()
    except urllib.error.HTTPError as err:
        raise ValueError(
            f"Model API returned HTTP {err.code}. Check base URL, model ID, and credentials."
        ) from None
    except (OSError, KeyError, IndexError, ValueError):
        raise ValueError(
            "Connection failed or response did not contain chat completion text."
        ) from None


def snapshot(db: sqlite3.Connection, vault: Path | None = None) -> dict:
    target_vault = vault or vault_dir()
    vault_data = load_vault_snapshot(target_vault)
    reader = VaultReader(target_vault)
    proposals = store.list_proposals(db, reader)
    return {
        "vault_path": vault_data["vault_path"],
        "cards": vault_data["cards"],
        "projects": vault_data["projects"],
        "stats": vault_data["stats"],
        "proposals": proposals,
        "integrations": client_integration_status(HOME),
        "config": get_setting(db, "model", DEFAULT),
        "runtime_mode": "native",
    }


def _review_messages(vault: Path, p: dict, lang: str) -> list[dict]:
    reader = VaultReader(vault)
    scope = p.get("scope") or ""
    if not scope and p.get("project"):
        info = reader.resolve_project(p["project"])
        scope = info.scope if info else ""

    same_scope_active = [
        {
            "id": card.card_id,
            "summary": card.summary,
            "effective_from": card.effective_from,
            "body": card.body,
        }
        for card in reader.cards.values()
        if card.scope == scope and card.status == "active"
    ]
    payload = {
        "scope": scope or p.get("project", ""),
        "active_rules": same_scope_active,
        "candidate": {
            "title": p.get("title") or p["body"][:40],
            "body": p["body"],
            "effective_from": p.get("effective_from") or p.get("date"),
            "supersedes": p.get("target") or None,
            "quote": p.get("quote") or "",
        },
    }
    if lang.startswith("zh"):
        system_prompt = (
            "你是 MemoryS 规则冲突检查助手。请对比同作用域现行规则与待确认候选，用简洁中文回答三点：\n"
            "1. 是否与现行规则冲突或重复；\n"
            "2. 建议新增还是替代哪张现行卡（列出卡片 ID）；\n"
            "3. 规则表述是否有歧义或缺失生效条件。\n"
            "控制在 150 字以内，仅供用户参考。"
        )
    else:
        system_prompt = (
            "You are a MemoryS rule conflict checker. Compare active_rules in the same scope against candidate and answer concisely in 3 bullet points:\n"
            "1. Any conflict or duplication with active rules;\n"
            "2. Whether to add as new or supersede an existing card (cite card ID);\n"
            "3. Any ambiguity or missing applicability conditions.\n"
            "Keep under 120 words. Advisory only."
        )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def ai_conflict_check(db, vault_path, proposal_id, key, lang="en", *,
                      lock=contextlib.nullcontext, key_for_url=None):
    with lock():
        proposal = dict(store.pending_proposal(db, proposal_id, vault_path))
        c = get_setting(db, "model", DEFAULT)
        messages = _review_messages(vault_path, proposal, lang)
        if key_for_url and not key:
            key = key_for_url(db, c["url"])
    reply = model_call(c, key, messages)
    reviewer = f"{c['provider']} / {c['model']}"
    with lock(), db:
        if dict(store.pending_proposal(db, proposal_id, vault_path)) != proposal:
            raise ValueError("Proposal changed during review; please review again.")
        db.execute(
            "UPDATE proposals SET opinion=?, reviewer=? WHERE id=?",
            (reply[:4000], reviewer, proposal_id),
        )
    return {"id": proposal_id, "opinion": reply, "reviewer": reviewer}


def dispatch(
    db: sqlite3.Connection,
    action: str,
    a: dict,
    key: str = "",
    vault: Path | None = None,
    *, lock=contextlib.nullcontext, key_for_url=None,
):
    target_vault = vault or vault_dir()
    if action == "snapshot":
        return snapshot(db, target_vault)
    if action == "setVaultPath":
        raw_path = str(a.get("path") or "").strip()
        if not raw_path:
            raise ValueError("Vault path cannot be empty.")
        chosen = store.open_or_init_vault(raw_path, with_demo=bool(a.get("with_demo", False)))
        save_user_config({"vault_path": str(chosen)})
        return {"vault_path": str(chosen)}
    if action == "config":
        return get_setting(db, "model", DEFAULT)
    if action == "validateConfig":
        return config(a)
    if action == "saveConfig":
        c = config(a)
        with db:
            set_setting(db, "model", c)
        return c
    if action == "test":
        with lock():
            c = config(a)
            if key_for_url and not key:
                key = key_for_url(db, c["url"])
        model_call(c, key, [{"role": "user", "content": "Reply with OK."}])
        return {"ok": True}
    if action == "createProposal":
        return store.create_proposal(
            db,
            target_vault,
            scope=a.get("scope", ""),
            title=a.get("title", ""),
            body=a.get("body", ""),
            effective_from=a.get("effective_from", ""),
            supersedes=a.get("target", "") or a.get("supersedes", ""),
            quote=a.get("quote", ""),
            use_when=a.get("use_when", []),
            client=a.get("client", "MemoryS UI"),
        )
    if action in {"edit", "editProposal"}:
        return store.edit_proposal(
            db,
            target_vault,
            proposal_id=a["id"],
            scope=a.get("scope", ""),
            title=a.get("title", ""),
            body=a.get("body", ""),
            effective_from=a.get("effective_from", ""),
            supersedes=a.get("target", "") or a.get("supersedes", ""),
            quote=a.get("quote", ""),
            use_when=a.get("use_when", []),
        )
    if action in {"ignore", "ignoreProposal"}:
        store.ignore_proposal(db, a["id"], target_vault)
        return {"id": a["id"], "state": "ignored"}
    if action in {"review", "reviewProposal"}:
        return ai_conflict_check(
            db, target_vault, a["id"], key, lang=str(a.get("lang") or "en"), lock=lock, key_for_url=key_for_url
        )
    if action == "publish":
        return store.publish_proposals(db, target_vault, a.get("ids", []))
    if action == "archiveCard":
        return store.archive_card(target_vault, a["id"])
    if action == "saveProjectMeta":
        return store.update_project_meta(
            target_vault,
            a["project_id"],
            a.get("aliases", []),
            a.get("paths", []),
        )
    raise ValueError(f"Unknown action: {action}")


def main():
    if sys.platform != "win32":
        os.umask(0o077)
    request = json.load(sys.stdin)
    action = request["action"]
    with contextlib.closing(connect()) as db:
        if action in MODEL_ACTIONS:
            return dispatch(db, action, request.get("args", {}), request.get("key", ""),
                            lock=lambda: workspace_lock(BASE))
        with workspace_lock(BASE):
            return dispatch(db, action, request.get("args", {}), request.get("key", ""))


MODEL_ACTIONS = {"test", "review", "reviewProposal"}


def run_stdio():
    try:
        print(json.dumps(dict(ok=True, data=main()), ensure_ascii=False))
    except Exception as exc:
        message = (
            str(exc)
            if isinstance(exc, (ValueError, FileNotFoundError, sqlite3.Error))
            else f"Local operation failed: {exc}"
        )
        print(json.dumps(dict(ok=False, error=message), ensure_ascii=False))


if __name__ == "__main__":
    run_stdio()
