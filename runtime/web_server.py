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
import socket
import subprocess
import sys
from urllib.parse import quote, urlsplit
import webbrowser

_RUNTIME = Path(__file__).resolve().parent
_REPO_ROOT = _RUNTIME.parent
from runtime import service_backend as b
from runtime.config import app_data_dir, resolve_vault, workspace_lock


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json; charset=utf-8",
}


def _find_ui_dir(ui_dir: Path | None = None) -> Path:
    override = ui_dir or os.environ.get("MEMORYS_UI_DIR")
    candidates = [Path(override).expanduser()] if override else [
        _REPO_ROOT / "desktop-app" / "dist",
        _RUNTIME / "ui",
    ]
    for c in candidates:
        if (c / "app.js").is_file() and (c / "app.css").is_file():
            return c.resolve()
    raise FileNotFoundError(
        "Built UI assets not found (desktop-app/dist/app.js). Run `cd desktop-app && npm ci && npm run build`."
    )


def _open_external(path_str: str, mode: str, vault: Path) -> dict:
    target = Path(path_str).expanduser().resolve()
    if not target.is_relative_to(vault.resolve()):
        raise ValueError("Path is outside the active vault.")
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
    if host not in LOOPBACK_HOSTS:
        raise ValueError("MemoryS requires a loopback host.")
    ui_root = _find_ui_dir(ui_dir)
    public_index = _REPO_ROOT / "desktop-app" / "public" / "index.html"
    index_src = public_index
    if not index_src.is_file():
        index_src = (ui_root / "index.html").resolve()
        if not index_src.is_relative_to(ui_root) or not index_src.is_file():
            index_src = _RUNTIME / "ui_index.html"
    token = secrets.token_urlsafe(24)
    vault = resolve_vault(vault_path, read_fallback=False)
    base_data = data_dir or app_data_dir()
    session_keys: dict[str, str] = {}

    def get_key_for_url(db, url_str: str) -> str:
        norm = (url_str or "").strip().rstrip("/")
        if norm in session_keys:
            return session_keys[norm]
        saved = b.get_setting(db, "model", {})
        bound_url = os.environ.get("MEMORYS_API_URL") or saved.get("url", "")
        if norm and norm == bound_url.strip().rstrip("/"):
            return os.environ.get("MEMORYS_API_KEY", "").strip()
        return ""

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

        def _valid_host(self) -> bool:
            hosts = self.headers.get_all("Host") or []
            port = self.server.server_port
            allowed = {"localhost", "127.0.0.1", "[::1]"}
            if len(hosts) == 1 and hosts[0].lower() in (allowed | {f"{h}:{port}" for h in allowed}):
                return True
            self._send(403, b"Invalid Host", "text/plain")
            return False

        def do_GET(self) -> None:
            if not self._valid_host():
                return
            parsed = urlsplit(self.path)
            rel = parsed.path.lstrip("/")
            if rel in ("", "index.html"):
                self._send(200, build_index_html(), MIME_TYPES[".html"])
                return
            if ".." in rel or rel.startswith("/"):
                self._send(403, b"Forbidden", "text/plain; charset=utf-8")
                return
            target = (ui_root / rel).resolve()
            if not target.is_relative_to(ui_root.resolve()) or not target.is_file():
                self._send(404, b"Not Found", "text/plain; charset=utf-8")
                return
            mime = MIME_TYPES.get(target.suffix.lower(), "application/octet-stream")
            self._send(200, target.read_bytes(), mime)

        def do_POST(self) -> None:
            nonlocal vault
            if not self._valid_host():
                return
            if urlsplit(self.path).path != "/api/rpc":
                self._send(404, b'{"ok":false,"error":"Not found"}', MIME_TYPES[".json"])
                return
            if self.headers.get("X-MemoryS-Token") != token:
                self._send(403, b'{"ok":false,"error":"Invalid session token"}', MIME_TYPES[".json"])
                return
            origins = self.headers.get_all("Origin")
            hostname = urlsplit("//" + self.headers["Host"]).hostname
            authority = "[::1]" if hostname == "::1" else hostname
            if origins is not None and origins != [f"http://{authority}:{self.server.server_port}"]:
                self._send(403, b'{"ok":false,"error":"Cross-origin request blocked"}', MIME_TYPES[".json"])
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 2_000_000:
                    raise ValueError("Request payload too large.")
                req = json.loads(self.rfile.read(length).decode("utf-8"))
                action = str(req.get("action") or "")
                args = dict(req.get("args") or {})
                entered_key = str(args.pop("key", "") or "").strip()

                with contextlib.closing(b.connect(base_data)) as db:
                    if action in b.MODEL_ACTIONS:
                        data = b.dispatch(db, action, args, entered_key, vault=vault,
                                          lock=lambda: workspace_lock(base_data), key_for_url=get_key_for_url)
                    else:
                        with workspace_lock(base_data):
                            if action == "openExternal":
                                data = _open_external(str(args.get("path") or ""), str(args.get("mode") or "obsidian"), vault)
                            elif action == "pickDirectory":
                                data = {"cancelled": True, "unsupported": True}
                            else:
                                data = b.dispatch(db, action, args, "", vault=vault)
                                if action == "saveConfig" and entered_key:
                                    session_keys[data["url"]] = entered_key
                                elif action == "snapshot":
                                    data["hasKey"] = bool(get_key_for_url(db, data["config"]["url"]))
                                    data["runtime_mode"] = "web"
                                elif action == "setVaultPath":
                                    vault = Path(data["vault_path"])

                body = json.dumps({"ok": True, "data": data}, ensure_ascii=False).encode("utf-8")
                self._send(200, body, MIME_TYPES[".json"])
            except Exception as exc:
                body = json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8")
                self._send(200, body, MIME_TYPES[".json"])

        def log_message(self, format: str, *args) -> None:
            pass

    class IPv6Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6

    server_class = IPv6Server if host == "::1" else ThreadingHTTPServer
    server = server_class((host, port), Handler)
    return server, token


def serve_web_ui(
    port: int = 8765,
    vault_path: str | Path | None = None,
    open_browser: bool = True,
    ui_dir: Path | None = None,
) -> int:
    server, _token = create_server(host="127.0.0.1", port=port, vault_path=vault_path, ui_dir=ui_dir)
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/"
    print(f"MemoryS Web UI running at {url}")
    print(f"Vault: {resolve_vault(vault_path, read_fallback=False)}")
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
