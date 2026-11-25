# Security Policy

## Trust & Isolation Model

MemoryS is designed for local-first personal and team memory governance:

1. **Read-Only Agent Boundary**: The MCP server (`runtime/mcp_server.py`) and recall hook (`runtime/recall_hook.py`) only read published Markdown cards. Agent writes are restricted to the pending SQLite proposal queue (`memory_propose`) and require explicit human approval to enter the vault.
2. **Secret & Policy Guard**: `validate_proposal_text()` blocks common API keys (`sk-`, `ghp_`, `AKIA`, private key headers, Bearer tokens) and explicit `"do not record"` directives before writing to SQLite or publishing.
3. **Local Web Console Hardening (`mem ui`)**:
   - Binds exclusively to `127.0.0.1` (loopback).
   - Injects a per-process random CSRF token required via `X-MemoryS-Token` on `POST /api/rpc`.
   - Enforces loopback `Origin` validation and strict CSP (`script-src 'self'`).
4. **API Key Handling**:
   - In the macOS native app, optional model API keys are stored in macOS Keychain.
   - In local Web UI mode, API keys are held only in process memory (or read from `MEMORYS_API_KEY`) and are never persisted in plaintext.

## Reporting a Vulnerability

If you discover a security vulnerability, please avoid opening a public issue with exploit details. Contact the maintainers privately via GitHub Security Advisories on the repository.
