# Contributing to MemoryS

Thank you for your interest in contributing to MemoryS!

## Design Principles

MemoryS intentionally stays small, auditable, and zero-dependency at runtime:

1. **Zero external Python dependencies in `runtime/`**: Keep the core reader, store, CLI, MCP server, recall hook, and local web server strictly on the Python 3.10+ standard library.
2. **Markdown + Git as the single source of truth**: Every published card must remain readable and editable in plain text / Obsidian without proprietary binary formats.
3. **Human publish gate by default**: Agent tools (`mcp_server.py`) must never mutate published vault files directly.
4. **Cross-platform compatibility**: Avoid OS-specific paths or POSIX-only APIs without a Windows fallback.

## Local Setup

```bash
# Run Python test suite
python3 -B -m unittest discover -v -s tests

# Build Svelte UI
cd desktop-app
npm ci
npm run build
```

## Pull Requests

- Keep PRs focused and include unit tests in `tests/` for new behavior or bug fixes.
- Ensure `python3 -B -m unittest discover -v -s tests` and `cd desktop-app && npm run build` both pass before submitting.
