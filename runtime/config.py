"""Cross-platform configuration, path resolution, and file locking for MemoryS."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
from typing import Any


def config_dir() -> Path:
    custom = os.environ.get("MEMORYS_CONFIG_DIR", "").strip()
    if custom:
        return Path(custom).expanduser().resolve()
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            return (Path(appdata) / "MemoryS").resolve()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return (Path(xdg) / "memorys").resolve()
    return (Path.home() / ".config" / "memorys").resolve()


def config_file() -> Path:
    return config_dir() / "config.json"


def load_user_config() -> dict[str, Any]:
    path = config_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_user_config(updates: dict[str, Any]) -> Path:
    cfg = load_user_config()
    cfg.update(updates)
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def app_data_dir() -> Path:
    custom = os.environ.get("MEMORYS_APP_DATA", "").strip()
    if custom:
        return Path(custom).expanduser().resolve()
    cfg_dir = str(load_user_config().get("app_data_dir") or "").strip()
    if cfg_dir:
        return Path(cfg_dir).expanduser().resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "MemoryS").resolve()
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app:
            return (Path(local_app) / "MemoryS").resolve()
        return (Path.home() / ".memorys" / "data").resolve()
    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg_data:
        return (Path(xdg_data) / "memorys").resolve()
    return (Path.home() / ".local" / "share" / "memorys").resolve()


def _looks_like_vault(path: Path) -> bool:
    if not path.is_dir():
        return False
    return (
        (path / "projects").is_dir()
        or (path / "notes").is_dir()
        or (path / "SOURCES.md").is_file()
        or (path / "来源记录.md").is_file()
    )


def resolve_vault(vault_path: str | Path | None = None, *, read_fallback: bool = True) -> Path:
    """Resolve the active MemoryS vault directory in deterministic priority order."""
    if vault_path is not None and str(vault_path).strip():
        return Path(vault_path).expanduser().resolve()

    env_vault = os.environ.get("MEMORYS_VAULT", "").strip()
    if env_vault:
        return Path(env_vault).expanduser().resolve()

    cfg_vault = str(load_user_config().get("vault_path") or "").strip()
    if cfg_vault:
        return Path(cfg_vault).expanduser().resolve()

    env_root = os.environ.get("MEMORYS_PROJECT_ROOT", "").strip()
    if env_root:
        candidate = (Path(env_root).expanduser().resolve() / "MemoryS").resolve()
        if _looks_like_vault(candidate):
            return candidate

    home_default = (Path.home() / "memorys-vault").resolve()
    for candidate in (home_default, Path.home() / "MemoryS"):
        if _looks_like_vault(candidate):
            return candidate.resolve()

    repo_root = Path(__file__).resolve().parents[1]
    demo_vault = (repo_root / "examples" / "demo-vault").resolve()
    if read_fallback and _looks_like_vault(demo_vault):
        return demo_vault

    return home_default


def vault_child(vault: Path, path: Path) -> Path:
    root = vault.resolve()
    resolved = path.resolve()
    if resolved == root or not resolved.is_relative_to(root) or ".git" in resolved.relative_to(root).parts:
        raise ValueError(f"Path must be inside the vault content directory: {path}")
    return resolved


def is_memorys_hook(command: str) -> bool:
    """Recognize the hook executable or script path in existing client settings."""
    try:
        args = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return False
    current_script = str(Path(__file__).with_name("recall_hook.py")).casefold().replace("\\", "/")
    for arg in args:
        path = arg.strip('"').casefold().replace("\\", "/")
        name = path.rsplit("/", 1)[-1]
        if name in {"memorys-hook", "memorys-hook.exe"}:
            return True
        if name == "recall_hook.py" and (path == current_script or "memorys" in path):
            return True
    return False


def find_git() -> str:
    found = shutil.which("git")
    if found:
        return found
    for candidate in ("/usr/bin/git", "/opt/homebrew/bin/git", "/usr/local/bin/git"):
        if os.path.isfile(candidate):
            return candidate
    return "git"


@contextlib.contextmanager
def workspace_lock(data_dir: Path | None = None):
    """Cross-platform exclusive file lock around proposal queue and vault writes."""
    base = data_dir or app_data_dir()
    base.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        try:
            os.chmod(base, 0o700)
        except OSError:
            pass
    lock_file = base / "workspace.lock"
    with lock_file.open("a+b") as handle:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
