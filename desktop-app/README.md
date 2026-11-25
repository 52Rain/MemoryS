# MemoryS Console (`desktop-app`)

A lightweight Svelte 5 console that runs in two modes:

1. **Native macOS App (`MemoryS.app`)**: 235-line Swift `WKWebView` shell with macOS Keychain storage and native directory pickers (no Electron).
2. **Cross-platform Local Web UI (`mem ui`)**: Served over `127.0.0.1` by `runtime/web_server.py` with a per-process CSRF token for Linux, Windows, and macOS browsers.

## Build Web Assets

Use Node.js 22.12 or later. A regular Python install already includes the web assets.

```bash
npm ci
npm run build
```

From the checkout, pass the build directory to `mem ui --ui-dir desktop-app/dist`.

## Package macOS Native App

```bash
npm ci
npm run package
open build.noindex/MemoryS.app
```

To also copy the built bundle into `~/Applications/MemoryS.app`:

```bash
INSTALL_TO_APPLICATIONS=1 npm run package
```
