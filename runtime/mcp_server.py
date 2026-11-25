"""Stdio MCP server for MemoryS (read + trace + human-gated draft proposal queue)."""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
import sys
from typing import Any

_RUNTIME = Path(__file__).resolve().parent
if str(_RUNTIME) not in sys.path:
    sys.path.insert(0, str(_RUNTIME))

try:
    from runtime.config import resolve_vault
    from runtime.memory_reader import VaultReader
    from runtime import memory_store as store
except ImportError:
    from config import resolve_vault
    from memory_reader import VaultReader
    import memory_store as store


SERVER_INFO = {"name": "memorys", "version": "0.1.0"}
PROTOCOL_VERSION = "2024-11-05"
INSTRUCTIONS = (
    "MemoryS governed knowledge service: search active or historical rules (memory_search), "
    "read full memory cards and supersession chains (memory_read), trace original quotes and sources "
    "(memory_trace), and submit draft proposals to the human review queue (memory_propose). "
    "Proposals never write directly to the published vault until confirmed by the user in the MemoryS UI."
)

TOOLS = [
    {
        "name": "memory_search",
        "description": (
            "Search published MemoryS cards by query, project name/ID/alias, or scope. "
            "Supports historical point-in-time lookups via as_of (YYYY-MM-DD)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keywords or subject_key; leave empty to list cards in scope",
                },
                "project": {
                    "type": "string",
                    "description": "Project title, alias, or project_id (e.g. acme-billing)",
                },
                "scope": {
                    "type": "string",
                    "description": "Scope filter (shared/preferences, shared/methods, shared/domain, or project/<id>)",
                },
                "as_of": {
                    "type": "string",
                    "description": "Optional business date (YYYY-MM-DD) for point-in-time rule lookup",
                },
                "include_history": {
                    "type": "boolean",
                    "description": "Include superseded and archived cards (default false)",
                },
            },
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "memory_read",
        "description": "Read a single memory card's full metadata, markdown body, path, and supersession chain.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "description": "Memory card ID"},
            },
            "required": ["card_id"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "memory_trace",
        "description": "Resolve a card's source_refs against SOURCES.md (or legacy 来源记录.md) to return provenance quotes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "description": "Memory card ID"},
            },
            "required": ["card_id"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "memory_propose",
        "description": (
            "Submit a draft memory card or revision to the MemoryS pending review queue (does NOT modify the published vault). "
            "Automatically blocks secrets, tokens, private keys, and 'do not record' directives."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "description": "Target scope or project name (shared/preferences, shared/methods, shared/domain, or project/<id>)",
                },
                "title": {"type": "string", "description": "Concise summary title (1-120 chars)"},
                "body": {"type": "string", "description": "Full rule, decision, or preference body"},
                "effective_from": {
                    "type": "string",
                    "description": "Effective date YYYY-MM-DD (defaults to today)",
                },
                "supersedes": {
                    "type": "string",
                    "description": "Optional existing card ID that this revision supersedes",
                },
                "quote": {
                    "type": "string",
                    "description": "Optional verbatim quote or source reference for provenance",
                },
                "use_when": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional retrieval trigger keywords",
                },
                "client": {
                    "type": "string",
                    "description": "Optional submitting client name (e.g. Claude Code, Codex, Cursor)",
                },
            },
            "required": ["scope", "title", "body"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
]


def handle_tool_call(
    name: str,
    arguments: dict[str, Any] | None,
    vault_path: str | Path | None = None,
    app_data_dir: Path | None = None,
) -> dict[str, Any]:
    args = arguments or {}
    vault = resolve_vault(vault_path)
    if name == "memory_search":
        reader = VaultReader(vault)
        items = reader.search(
            query=str(args.get("query") or ""),
            project=str(args.get("project") or ""),
            scope=str(args.get("scope") or ""),
            as_of=str(args.get("as_of") or "") or None,
            include_history=bool(args.get("include_history", False)),
        )
        return {"count": len(items), "cards": items}

    if name == "memory_read":
        reader = VaultReader(vault)
        return reader.read_card(str(args.get("card_id") or ""))

    if name == "memory_trace":
        reader = VaultReader(vault)
        return reader.trace_card(str(args.get("card_id") or ""))

    if name == "memory_propose":
        data_dir = app_data_dir or store.app_data_dir()
        with contextlib.closing(store.connect_db(data_dir)) as db, store.workspace_lock(data_dir):
            created = store.create_proposal(
                db,
                vault,
                scope=str(args.get("scope") or ""),
                title=str(args.get("title") or ""),
                body=str(args.get("body") or ""),
                effective_from=str(args.get("effective_from") or ""),
                supersedes=str(args.get("supersedes") or ""),
                quote=str(args.get("quote") or ""),
                use_when=args.get("use_when") or [],
                client=str(args.get("client") or "Agent MCP"),
            )
        return {
            "status": "queued",
            "proposal_id": created["id"],
            "state": created["state"],
            "scope": created["scope"],
            "title": created["title"],
            "effective_from": created["effective_from"],
            "message": "Queued in MemoryS review inbox (pending). Review and publish in MemoryS UI to commit to the vault.",
        }

    raise ValueError(f"Unknown tool: {name}")


def _read_message(stdin_buf) -> tuple[dict | None, str]:
    """Read one JSON-RPC message (supports newline-delimited JSON and Content-Length framing)."""
    while True:
        line = stdin_buf.readline()
        if not line:
            return None, "jsonl"
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(b"content-length:"):
            length = int(stripped.split(b":", 1)[1].strip())
            while True:
                header = stdin_buf.readline()
                if not header or header in (b"\r\n", b"\n"):
                    break
            body = stdin_buf.read(length)
            return json.loads(body.decode("utf-8")), "lsp"
        return json.loads(stripped.decode("utf-8")), "jsonl"


def _write_message(stdout_buf, payload: dict, framing: str) -> None:
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if framing == "lsp":
        header = f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii")
        stdout_buf.write(header + encoded)
    else:
        stdout_buf.write(encoded + b"\n")
    stdout_buf.flush()


def serve_stdio() -> int:
    stdin_buf = sys.stdin.buffer
    stdout_buf = sys.stdout.buffer
    while True:
        try:
            msg, framing = _read_message(stdin_buf)
        except Exception:
            break
        if msg is None:
            break

        req_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}

        if req_id is None:
            continue

        if method == "initialize":
            client_proto = params.get("protocolVersion") or PROTOCOL_VERSION
            result = {
                "protocolVersion": client_proto,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
                "instructions": INSTRUCTIONS,
            }
            _write_message(stdout_buf, {"jsonrpc": "2.0", "id": req_id, "result": result}, framing)
        elif method == "ping":
            _write_message(stdout_buf, {"jsonrpc": "2.0", "id": req_id, "result": {}}, framing)
        elif method == "tools/list":
            _write_message(
                stdout_buf,
                {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}},
                framing,
            )
        elif method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments") or {}
            try:
                out = handle_tool_call(tool_name, tool_args)
                result = {
                    "content": [
                        {"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=2)}
                    ],
                    "isError": False,
                }
            except Exception as exc:
                result = {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                }
            _write_message(stdout_buf, {"jsonrpc": "2.0", "id": req_id, "result": result}, framing)
        else:
            _write_message(
                stdout_buf,
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                },
                framing,
            )
    return 0


def main() -> int:
    return serve_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
