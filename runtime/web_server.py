"""Lightweight local HTTP server hosting the MemoryS Review & Library UI (Linux/Windows/macOS).

Security model:
- Binds strictly to 127.0.0.1 (loopback only).
- Generates a per-process random CSRF token injected into index.html and required via X-MemoryS-Token.
- Enforces same-origin checks on POST /api/rpc.
- API keys for optional AI conflict checks are kept in process memory (or read from MEMORYS_API_KEY),
  never written to disk in plaintext.
"""
from __future__ import annotations

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from urllib.parse import quote, urlsplit
import webbrowser

_RUNTIME = Path(__file__).resolve().parent
_REPO_ROOT = _RUNTIME.parent
_SERVICE = _REPO_ROOT / "desktop-app" / "service"
for p in (_RUNTIME, _SERVICE):
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

import backend as b  # noqa: E402
from config import app_data_dir, resolve_vault, workspace_lock  # noqa: E402


MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json; charset=utf-8",
}


def _find_ui_dir() -> Path:
    candidates = [
        _REPO_ROOT / "desktop-app" / "dist",
        _REPO_ROOT / "runtime" / "ui",
    ]
    for c in candidates:
        if (c / "app.js").is_file() and (c / "app.css").is_file():
            return c
    raise FileNotFoundError(
        "Built UI assets not found (desktop-app/dist/app.js). Run `cd desktop-app && npm ci && npm run build`."
    )


def _open_external(path_str: str, mode: str) -> dict:
    target = Path(path_str).expanduser().resolve()
    if not target.exists():
        raise ValueError(f"File not found: {target}")
    if mode == "obsidian":
        uri = f"obsidian://open?path={quote(str(target))}"
        if sys.platform == "darwin":
            subprocess.Popen(["open", uri])
        elif sys.platform == "win32":
            os.startfile(uri)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", uri])
        return {"ok": True}
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(target)])
    elif sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target.parent)])
    return {"ok": True}


def create_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    vault_path: str | Path | None = None,
    data_dir: Path | None = None,
    ui_dir: Path | None = None,
) -> tuple[ThreadingHTTPServer, str]:
    ui_root = ui_dir or _find_ui_dir()
    public_index = _REPO_ROOT / "desktop-app" / "public" / "index.html"
    index_src = public_index if public_index.is_file() else (ui_root / "index.html")
    token = secrets.token_urlsafe(24)
    vault = resolve_vault(vault_path)
    base_data = data_dir or app_data_dir()
    session_keys: dict[str, str] = {}

    def get_key_for_url(url_str: str) -> str:
        norm = (url_str or "").strip().rstrip("/")
        return session_keys.get(norm) or os.environ.get("MEMORYS_API_KEY", "").strip()

    def build_index_html() -> bytes:
        html = index_src.read_text(encoding="utf-8")
        # Allow same-origin fetch for web mode without relaxing script-src
        html = html.replace("connect-src 'none'", "connect-src 'self'")
        meta_payload = json.dumps({"token": token, "platform": sys.platform}, ensure_ascii=False)
        escaped = meta_payload.replace("&", "&amp;").replace('"', "&quot;")
        bootstrap = f'<meta name="memorys-web" content="{escaped}">'
        if "</head>" in html:
            html = html.replace("</head>", bootstrap + "</head>", 1)
        else:
            html = bootstrap + html
        return html.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            rel = parsed.path.lstrip("/")
            if rel in ("", "index.html"):
                self._send(200, build_index_html(), MIME_TYPES[".html"])
                return
            if ".." in rel or rel.startswith("/"):
                self._send(403, b"Forbidden", "text/plain; charset=utf-8")
                return
            target = (ui_root / rel).resolve()
            if not str(target).startswith(str(ui_root.resolve())) or not target.is_file():
                self._send(404, b"Not Found", "text/plain; charset=utf-8")
                return
            mime = MIME_TYPES.get(target.suffix.lower(), "application/octet-stream")
            self._send(200, target.read_bytes(), mime)

        def do_POST(self) -> None:
            if urlsplit(self.path).path != "/api/rpc":
                self._send(404, b'{"ok":false,"error":"Not found"}', MIME_TYPES[".json"])
                return
            if self.headers.get("X-MemoryS-Token") != token:
                self._send(403, b'{"ok":false,"error":"Invalid session token"}', MIME_TYPES[".json"])
                return
            origin = self.headers.get("Origin", "")
            if origin:
                orig_host = urlsplit(origin).hostname
                if orig_host not in {"127.0.0.1", "localhost", "::1"}:
                    self._send(403, b'{"ok":false,"error":"Cross-origin request blocked"}', MIME_TYPES[".json"])
                    return

            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 2_000_000:
                    raise ValueError("Request payload too large.")
                req = json.loads(self.rfile.read(length).decode("utf-8"))
                action = str(req.get("action") or "")
                args = dict(req.get("args") or {})
                entered_key = str(args.pop("key", "") or "").strip()

                if action == "openExternal":
                    data = _open_external(str(args.get("path") or ""), str(args.get("mode") or "obsidian"))
                elif action == "pickDirectory":
                    data = {"cancelled": True, "unsupported": True}
                else:
                    with contextlib.closing(b.connect(base_data)) as db, workspace_lock(base_data):
                        active_key = entered_key
                        if action == "saveConfig":
                            cfg = b.config(args)
                            if entered_key:
                                session_keys[cfg["url"]] = entered_key
                            data = b.dispatch(db, action, args, "", vault=vault)
                        elif action == "test":
                            cfg = b.config(args)
                            active_key = entered_key or get_key_for_url(cfg["url"])
                            data = b.dispatch(db, action, args, active_key, vault=vault)
                        elif action == "reviewProposal":
                            cfg = b.dispatch(db, "config", {}, "", vault=vault)
                            active_key = get_key_for_url(cfg["url"])
                            data = b.dispatch(db, action, args, active_key, vault=vault)
                        elif action == "snapshot":
                            data = b.dispatch(db, action, args, "", vault=vault)
                            cfg = data.get("config") or {}
                            data["hasKey"] = bool(get_key_for_url(cfg.get("url", "")))
                            data["runtime_mode"] = "web"
                        else:
                            data = b.dispatch(db, action, args, "", vault=vault)

                body = json.dumps({"ok": True, "data": data}, ensure_ascii=False).encode("utf-8")
                self._send(200, body, MIME_TYPES[".json"])
            except Exception as exc:
                body = json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8")
                self._send(200, body, MIME_TYPES[".json"])

        def log_message(self, format: str, *args) -> None:
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    return server, token


def serve_web_ui(
    port: int = 8765,
    vault_path: str | Path | None = None,
    open_browser: bool = True,
) -> int:
    server, _token = create_server(host="127.0.0.1", port=port, vault_path=vault_path)
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/"
    print(f"MemoryS Web UI running at {url}")
    print(f"Vault: {resolve_vault(vault_path)}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped MemoryS Web UI.")
    finally:
        server.server_close()
    return 0
