# Security Policy

## Trust & Isolation Model

MemoryS is designed for local-first personal and team memory governance:

1. **MCP tool-layer read-only isolation (MCP 工具层只读隔离)**: The MCP server (`runtime/mcp_server.py`) and recall hook (`runtime/recall_hook.py`) only read published Markdown cards. Agent writes are restricted to the pending SQLite proposal queue (`memory_propose`) and require explicit human approval to enter the vault. Processes running under the same OS account retain their existing filesystem permissions.
2. **Secret & Policy Guard**: `validate_proposal_text()` blocks common API keys (`sk-`, `ghp_`, `AKIA`, private key headers, Bearer tokens) and explicit `"do not record"` directives before writing to SQLite or publishing.
3. **Local Web Console Hardening (`mem ui`)**:
   - Binds exclusively to `127.0.0.1` (loopback).
   - Injects a per-process random CSRF token required via `X-MemoryS-Token` on `POST /api/rpc`.
   - Validates the loopback Host and requires a matching HTTP origin and port when an Origin header is present. Uses strict CSP (`script-src 'self'`).
4. **API Key Handling**:
   - In the macOS native app, optional model API keys are stored in macOS Keychain.
   - In local Web UI mode, API keys are held only in process memory (or read from `MEMORYS_API_KEY`) and apply only to the bound model base URL. Set `MEMORYS_API_URL` to bind the environment key explicitly. Otherwise MemoryS uses the model URL saved in SQLite.

## Reporting a Vulnerability

If you discover a security vulnerability, please avoid opening a public issue with exploit details. Contact the maintainers privately via GitHub Security Advisories on the repository.
