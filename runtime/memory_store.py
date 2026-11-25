"""Manage MemoryS draft proposals (SQLite), Vault initialization, and Git-backed publishing."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid

try:
    from runtime.config import app_data_dir, find_git, resolve_vault, workspace_lock
    from runtime.memory_reader import (
        SCOPE_LABELS,
        VaultReader,
        _parse_date,
        dump_frontmatter,
        parse_frontmatter,
        subject_key,
    )
except ImportError:
    from config import app_data_dir, find_git, resolve_vault, workspace_lock
    from memory_reader import (
        SCOPE_LABELS,
        VaultReader,
        _parse_date,
        dump_frontmatter,
        parse_frontmatter,
        subject_key,
    )


SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{24,}"),
    re.compile(
        r"(?:api[_-]?key|secret[_-]?key|access[_-]?token|password|passwd)\s*[:=]\s*['\"]?[A-Za-z0-9/_+=.-]{16,}",
        re.I,
    ),
]

FORBIDDEN_RECORD_PATTERN = re.compile(
    r"do not record|don't record|do not save to memory|never store this|secret do not save|"
    r"禁止外发|不要外发|禁止记录|不要记录|别记录|别记|不要保存|不可记录|私密勿存|禁止写入记忆",
    re.I,
)

# Canonical English states + legacy Chinese states for seamless compatibility
CLOSED_STATES = {"published", "ignored", "restricted", "已发布", "已忽略", "受限"}
PENDING_STATES = {"pending", "待确认", "待审阅", "待修改", "待发布"}


def normalize_state(raw: str) -> str:
    val = (raw or "").strip()
    if val in PENDING_STATES:
        return "pending"
    if val in {"published", "已发布"}:
        return "published"
    if val in {"ignored", "已忽略"}:
        return "ignored"
    if val in {"restricted", "受限"}:
        return "restricted"
    return val or "pending"


def validate_proposal_text(title: str, body: str, quote: str = "") -> None:
    """Reject secrets/credentials and explicit 'do not record' directives."""
    combined = "\n".join(part for part in (title, body, quote) if part)
    for pattern in SECRET_PATTERNS:
        if pattern.search(combined):
            raise ValueError(
                "Blocked: content appears to contain an API key, token, or private key."
            )
    if FORBIDDEN_RECORD_PATTERN.search(combined):
        raise ValueError(
            "Blocked: content contains a 'do not record / do not share' directive."
        )


def connect_db(data_dir: Path | None = None) -> sqlite3.Connection:
    base = data_dir or app_data_dir()
    base.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        try:
            os.chmod(base, 0o700)
        except OSError:
            pass
    db = sqlite3.connect(base / "workspace.sqlite3", timeout=20)
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings(
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS proposals(
            id TEXT PRIMARY KEY,
            project TEXT,
            path TEXT DEFAULT '',
            client TEXT DEFAULT '',
            offset INTEGER DEFAULT 0,
            quote TEXT DEFAULT '',
            body TEXT NOT NULL,
            date TEXT NOT NULL,
            state TEXT NOT NULL,
            opinion TEXT DEFAULT '',
            reviewer TEXT DEFAULT '',
            target TEXT DEFAULT '',
            base TEXT DEFAULT '',
            title TEXT DEFAULT '',
            scope TEXT DEFAULT '',
            kind TEXT DEFAULT 'decision',
            effective_from TEXT DEFAULT '',
            use_when TEXT DEFAULT '[]',
            created_at TEXT DEFAULT ''
        );
        """
    )
    existing = {row[1] for row in db.execute("PRAGMA table_info(proposals)").fetchall()}
    migrations = {
        "scope": "TEXT DEFAULT ''",
        "kind": "TEXT DEFAULT 'decision'",
        "effective_from": "TEXT DEFAULT ''",
        "use_when": "TEXT DEFAULT '[]'",
        "created_at": "TEXT DEFAULT ''",
    }
    for col, col_def in migrations.items():
        if col not in existing:
            db.execute(f"ALTER TABLE proposals ADD COLUMN {col} {col_def}")
    return db


def _git_root(vault: Path) -> Path:
    git_bin = find_git()
    proc = subprocess.run(
        [git_bin, "-C", str(vault), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise ValueError(f"Vault directory is not inside a Git repository: {vault}")
    return Path(proc.stdout.strip()).resolve()


def _git(repo_root: Path, *args: str, check_identity: bool = False) -> str:
    git_bin = find_git()
    cmd = [git_bin, "-C", str(repo_root)]
    if check_identity:
        name_check = subprocess.run(
            [git_bin, "-C", str(repo_root), "config", "user.name"],
            capture_output=True,
            text=True,
        )
        email_check = subprocess.run(
            [git_bin, "-C", str(repo_root), "config", "user.email"],
            capture_output=True,
            text=True,
        )
        if not name_check.stdout.strip() or not email_check.stdout.strip():
            cmd.extend(["-c", "user.name=MemoryS", "-c", "user.email=local@memorys.dev"])
    cmd.extend(args)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise ValueError(f"Git operation failed: {detail or 'unknown error'}")
    return proc.stdout.strip()


def commit_vault_files(vault: Path, touched_paths: list[Path], message: str) -> str:
    repo_root = _git_root(vault)
    rels = []
    seen = set()
    for p in touched_paths:
        rel = p.resolve().relative_to(repo_root).as_posix()
        if rel not in seen:
            seen.add(rel)
            rels.append(rel)
    if not rels:
        return _git(repo_root, "rev-parse", "--short", "HEAD")
    _git(repo_root, "add", "--", *rels)
    status = _git(repo_root, "status", "--porcelain", "--", *rels)
    if not status.strip():
        return _git(repo_root, "rev-parse", "--short", "HEAD")
    _git(repo_root, "commit", "--only", "-m", message, "--", *rels, check_identity=True)
    return _git(repo_root, "rev-parse", "HEAD")


def _vault_index_file(vault: Path) -> Path:
    legacy = vault / "项目索引.md"
    if legacy.is_file() and not (vault / "PROJECTS.md").is_file():
        return legacy
    return vault / "PROJECTS.md"


def _vault_sources_file(vault: Path) -> Path:
    legacy = vault / "来源记录.md"
    if legacy.is_file() and not (vault / "SOURCES.md").is_file():
        return legacy
    return vault / "SOURCES.md"


def init_vault(target: str | Path, *, with_demo: bool = False) -> Path:
    """Initialize a fresh Git-backed MemoryS Obsidian vault."""
    vault = Path(target).expanduser().resolve()
    vault.mkdir(parents=True, exist_ok=True)
    for sub in ("projects", "notes/preferences", "notes/methods", "notes/domain", "templates"):
        (vault / sub).mkdir(parents=True, exist_ok=True)

    if with_demo:
        demo_src = Path(__file__).resolve().parents[1] / "examples" / "demo-vault"
        if demo_src.is_dir():
            for src_file in demo_src.rglob("*"):
                if src_file.is_file():
                    rel = src_file.relative_to(demo_src)
                    dest = vault / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if not dest.exists():
                        shutil.copy2(src_file, dest)

    start_md = vault / "START.md"
    if not start_md.exists():
        start_md.write_text(
            "# MemoryS Vault\n\n"
            "Local-first, human-governed memory vault for AI agents.\n\n"
            "- [Projects](PROJECTS.md)\n"
            "- [Sources](SOURCES.md)\n",
            encoding="utf-8",
        )
    projects_md = _vault_index_file(vault)
    if not projects_md.exists():
        projects_md.write_text(
            "# Projects\n\nAuto-updated index of projects in this vault.\n",
            encoding="utf-8",
        )
    sources_md = _vault_sources_file(vault)
    if not sources_md.exists():
        sources_md.write_text(
            "# Sources\n\nTrace published memory cards back to original quotes and events.\n\n```jsonl\n```\n",
            encoding="utf-8",
        )
    tpl_md = vault / "templates" / "memory-card.md"
    if not tpl_md.exists():
        tpl_md.write_text(
            "---\n"
            "id: card-id\n"
            "kind: decision\n"
            "scope: project/example\n"
            "status: active\n"
            "basis: owner_confirmed\n"
            "summary: Short one-line rule summary\n"
            "use_when:\n"
            "- keyword\n"
            "effective_from: '2026-01-01'\n"
            "---\n\n"
            "Describe the rule, constraints, and exceptions here.\n",
            encoding="utf-8",
        )
    gitignore = vault / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(".obsidian/workspace*\n.DS_Store\n", encoding="utf-8")

    # Initialize git if not already inside a git repository
    git_bin = find_git()
    check = subprocess.run(
        [git_bin, "-C", str(vault), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    if check.returncode != 0:
        subprocess.run([git_bin, "init", "-q", "-b", "main", str(vault)], check=True)
    _git(vault, "add", "-A")
    status = _git(vault, "status", "--porcelain")
    if status.strip():
        _git(vault, "commit", "-m", "Initialize MemoryS vault", check_identity=True)
    return vault


def normalize_scope(scope_input: str, reader: VaultReader) -> tuple[str, str, str]:
    """Normalize user/agent scope input into (scope, kind, display_project_name)."""
    raw = (scope_input or "").strip()
    if not raw:
        raise ValueError("Scope is required (e.g., shared/preferences or project/<name>).")
    shared_map = {
        "shared/preferences": ("shared/preferences", "preference", SCOPE_LABELS["shared/preferences"]),
        "preferences": ("shared/preferences", "preference", SCOPE_LABELS["shared/preferences"]),
        "preference": ("shared/preferences", "preference", SCOPE_LABELS["shared/preferences"]),
        "个人偏好": ("shared/preferences", "preference", SCOPE_LABELS["shared/preferences"]),
        "shared/methods": ("shared/methods", "method", SCOPE_LABELS["shared/methods"]),
        "methods": ("shared/methods", "method", SCOPE_LABELS["shared/methods"]),
        "method": ("shared/methods", "method", SCOPE_LABELS["shared/methods"]),
        "通用方法": ("shared/methods", "method", SCOPE_LABELS["shared/methods"]),
        "shared/domain": ("shared/domain", "domain", SCOPE_LABELS["shared/domain"]),
        "domain": ("shared/domain", "domain", SCOPE_LABELS["shared/domain"]),
        "领域知识": ("shared/domain", "domain", SCOPE_LABELS["shared/domain"]),
    }
    if raw.casefold() in shared_map:
        return shared_map[raw.casefold()]
    if raw in shared_map:
        return shared_map[raw]

    if raw.startswith("project/new:"):
        title = raw.split("project/new:", 1)[1].strip()
        if not title:
            raise ValueError("New project name cannot be empty.")
        existing = reader.resolve_project(title)
        if existing:
            return existing.scope, "decision", existing.title
        return f"project/new:{title}", "decision", title

    existing = reader.resolve_project(raw)
    if existing:
        return existing.scope, "decision", existing.title

    name = raw.split("/", 1)[1].strip() if raw.startswith("project/") else raw
    if not name:
        raise ValueError("Project name cannot be empty.")
    return f"project/new:{name}", "decision", name


def _parse_use_when(raw) -> list[str]:
    if isinstance(raw, list):
        items = [str(x).strip() for x in raw if str(x).strip()]
    elif isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            try:
                loaded = json.loads(text)
                if isinstance(loaded, list):
                    return [str(x).strip() for x in loaded if str(x).strip()]
            except json.JSONDecodeError:
                pass
        items = [x.strip() for x in re.split(r"[,，\n]+", text) if x.strip()]
    else:
        items = []
    deduped = []
    for item in items:
        if item not in deduped:
            deduped.append(item)
    return deduped


def create_proposal(
    db: sqlite3.Connection,
    vault_path: str | Path | None,
    *,
    scope: str,
    title: str,
    body: str,
    effective_from: str = "",
    supersedes: str = "",
    quote: str = "",
    use_when: list[str] | str | None = None,
    client: str = "",
) -> dict:
    """Create a pending proposal in workspace.sqlite3."""
    clean_title = (title or "").strip()
    clean_body = (body or "").strip()
    clean_quote = (quote or "").strip()
    if not clean_title or len(clean_title) > 120:
        raise ValueError("Title (summary) must be between 1 and 120 characters.")
    if not clean_body or len(clean_body) > 8000:
        raise ValueError("Body must be between 1 and 8000 characters.")
    validate_proposal_text(clean_title, clean_body, clean_quote)

    eff_date = (effective_from or "").strip() or date.today().isoformat()
    if _parse_date(eff_date) is None:
        raise ValueError(f"Invalid effective_from date, expected YYYY-MM-DD: {eff_date}")

    reader = VaultReader(vault_path)
    norm_scope, kind, project_label = normalize_scope(scope, reader)

    target_id = (supersedes or "").strip()
    base_hash = ""
    if target_id:
        old_card = reader.cards.get(target_id)
        if not old_card:
            raise ValueError(f"Card to supersede not found: {target_id}")
        if old_card.scope != norm_scope:
            raise ValueError("Superseded card belongs to a different scope.")
        base_hash = hashlib.sha256(old_card.body.encode("utf-8")).hexdigest()[:10]

    triggers = _parse_use_when(use_when or [])
    if not triggers:
        triggers = [clean_title]

    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    pid = "prop-" + uuid.uuid4().hex[:12]
    clean_client = (client or "Agent").strip() or "Agent"

    with db:
        db.execute(
            """
            INSERT INTO proposals(
                id, project, path, client, offset, quote, body, date, state,
                opinion, reviewer, target, base, title, scope, kind, effective_from, use_when, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                pid,
                project_label,
                "",
                clean_client,
                0,
                clean_quote,
                clean_body,
                eff_date,
                "pending",
                "",
                "",
                target_id,
                base_hash,
                clean_title,
                norm_scope,
                kind,
                eff_date,
                json.dumps(triggers, ensure_ascii=False),
                now_iso,
            ),
        )
    return {
        "id": pid,
        "scope": norm_scope,
        "kind": kind,
        "project": project_label,
        "title": clean_title,
        "effective_from": eff_date,
        "target": target_id,
        "state": "pending",
        "client": clean_client,
    }


def edit_proposal(
    db: sqlite3.Connection,
    vault_path: str | Path | None,
    *,
    proposal_id: str,
    scope: str,
    title: str,
    body: str,
    effective_from: str = "",
    supersedes: str = "",
    quote: str = "",
    use_when: list[str] | str | None = None,
) -> dict:
    """Update a pending proposal in workspace.sqlite3."""
    row = db.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    if not row or row["state"] in CLOSED_STATES:
        raise ValueError("Proposal state has changed; please refresh.")
    clean_title = (title or "").strip()
    clean_body = (body or "").strip()
    clean_quote = (quote or "").strip()
    if not clean_title or len(clean_title) > 120:
        raise ValueError("Title (summary) must be between 1 and 120 characters.")
    if not clean_body or len(clean_body) > 8000:
        raise ValueError("Body must be between 1 and 8000 characters.")
    validate_proposal_text(clean_title, clean_body, clean_quote)

    eff_date = (effective_from or "").strip() or row["effective_from"] or date.today().isoformat()
    if _parse_date(eff_date) is None:
        raise ValueError(f"Invalid effective_from date, expected YYYY-MM-DD: {eff_date}")

    reader = VaultReader(vault_path)
    norm_scope, kind, project_label = normalize_scope(scope, reader)

    target_id = (supersedes or "").strip()
    base_hash = ""
    if target_id:
        old_card = reader.cards.get(target_id)
        if not old_card:
            raise ValueError(f"Card to supersede not found: {target_id}")
        if old_card.scope != norm_scope:
            raise ValueError("Superseded card belongs to a different scope.")
        base_hash = hashlib.sha256(old_card.body.encode("utf-8")).hexdigest()[:10]

    triggers = _parse_use_when(use_when or [])
    if not triggers:
        triggers = [clean_title]

    with db:
        db.execute(
            """
            UPDATE proposals
            SET project=?, title=?, body=?, quote=?, target=?, base=?, scope=?, kind=?,
                effective_from=?, date=?, use_when=?, state='pending', opinion='', reviewer=''
            WHERE id=?
            """,
            (
                project_label,
                clean_title,
                clean_body,
                clean_quote,
                target_id,
                base_hash,
                norm_scope,
                kind,
                eff_date,
                eff_date,
                json.dumps(triggers, ensure_ascii=False),
                proposal_id,
            ),
        )
    return {"id": proposal_id, "state": "pending"}


def ignore_proposal(db: sqlite3.Connection, proposal_id: str) -> None:
    row = db.execute("SELECT state FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    if not row or row["state"] in CLOSED_STATES:
        raise ValueError("Proposal state has changed; please refresh.")
    with db:
        db.execute("UPDATE proposals SET state='ignored' WHERE id=?", (proposal_id,))


def list_proposals(db: sqlite3.Connection, reader: VaultReader) -> list[dict]:
    placeholders = ",".join("?" for _ in CLOSED_STATES)
    rows = db.execute(
        f"SELECT * FROM proposals WHERE state NOT IN ({placeholders}) "
        "ORDER BY created_at DESC, date DESC, rowid DESC",
        tuple(CLOSED_STATES),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        target_id = item.get("target") or ""
        old_card = reader.cards.get(target_id) if target_id else None
        scope = item.get("scope") or ""
        if not scope and item.get("project"):
            info = reader.resolve_project(item["project"])
            scope = info.scope if info else f"project/new:{item['project']}"
        item["scope"] = scope
        item["scope_label"] = (
            item["project"] if scope.startswith("project/new:") else reader.scope_label(scope)
        )
        item["effective_from"] = item.get("effective_from") or item.get("date") or date.today().isoformat()
        item["use_when"] = _parse_use_when(item.get("use_when") or "[]")
        item["before"] = old_card.body if old_card else ""
        item["before_summary"] = old_card.summary if old_card else ""
        item["title"] = item.get("title") or item["body"][:40]
        item["source"] = item.get("client") or "Manual"
        item["state"] = normalize_state(item.get("state", "pending"))
        out.append(item)
    return out


def _make_project_id(title: str, existing_ids: set[str]) -> str:
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")
    if ascii_slug and len(ascii_slug) >= 2 and ascii_slug not in existing_ids:
        return ascii_slug[:32]
    digest = hashlib.sha256(title.strip().encode("utf-8")).hexdigest()[:16]
    candidate = f"p-{digest}"
    idx = 1
    while candidate in existing_ids:
        candidate = f"p-{digest}-{idx}"
        idx += 1
    return candidate


def _ensure_project_start(
    vault: Path, reader: VaultReader, title: str
) -> tuple[str, Path, bool]:
    info = reader.resolve_project(title)
    if info:
        return info.project_id, info.start_path, False
    pid = _make_project_id(title, set(reader.projects.keys()))
    proj_dir = vault / "projects" / pid
    proj_dir.mkdir(parents=True, exist_ok=True)
    start_path = proj_dir / "START.md"
    sources_name = _vault_sources_file(vault).name
    meta = {
        "project_id": pid,
        "title": title,
        "aliases": [title],
        "paths": [],
    }
    body = f"# {title}\n\n[Sources](../../{sources_name})"
    start_path.write_text(dump_frontmatter(meta, body), encoding="utf-8")
    return pid, start_path, True


def _regenerate_project_index(vault: Path) -> Path:
    reader = VaultReader(vault)
    index_path = _vault_index_file(vault)
    if index_path.name == "项目索引.md":
        lines = ["# 项目索引", "", "由库内项目首页生成。打开项目即可查看现行和历史记忆。", ""]
    else:
        lines = ["# Projects", "", "Auto-generated from project START.md files.", ""]
    for pid, info in sorted(
        reader.projects.items(), key=lambda kv: (kv[1].title.casefold(), kv[0])
    ):
        safe_title = re.sub(r"([\\\[\]])", r"\\\1", " ".join(info.title.splitlines()))
        lines.append(f"- [{safe_title}](projects/{pid}/START.md)")
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return index_path


def _append_project_card_link(start_path: Path, summary: str, card_id: str) -> None:
    text = start_path.read_text(encoding="utf-8")
    target_link = f"{card_id}.md"
    if target_link in text:
        return
    meta, body = parse_frontmatter(text)
    safe_label = re.sub(r"([\\\[\]])", r"\\\1", " ".join(summary.splitlines()))
    bullet = f"- [{safe_label}]({target_link})"
    lines = body.splitlines() if body else []
    insert_at = len(lines)
    for idx, line in enumerate(lines):
        if "SOURCES.md" in line or "来源记录.md" in line:
            insert_at = idx
            break
    lines.insert(insert_at, bullet)
    if insert_at + 1 < len(lines) and lines[insert_at + 1].strip():
        lines.insert(insert_at + 1, "")
    new_body = "\n".join(lines).strip()
    start_path.write_text(dump_frontmatter(meta, new_body), encoding="utf-8")


def _append_evidence_event(
    vault: Path,
    *,
    event_id: str,
    project: str,
    at: str,
    actor: str,
    text: str,
    location: str,
) -> Path:
    evidence_path = _vault_sources_file(vault)
    row = json.dumps(
        {
            "event_id": event_id,
            "project": project,
            "at": at,
            "actor": actor,
            "synthetic": False,
            "location": location,
            "text": text,
        },
        ensure_ascii=False,
    )
    if not evidence_path.is_file():
        header = (
            "# 来源记录\n\n通过事件 ID 查找正式记忆的原话和来源。\n\n```jsonl\n"
            if evidence_path.name == "来源记录.md"
            else "# Sources\n\nTrace published memory cards back to original quotes and events.\n\n```jsonl\n"
        )
        evidence_path.write_text(header + row + "\n```\n", encoding="utf-8")
        return evidence_path

    existing = evidence_path.read_text(encoding="utf-8")
    head, fence, rest = existing.partition("\n```jsonl\n")
    if fence:
        if rest.startswith("```"):
            jsonl_body, end_fence, tail = "", "\n```", rest[3:]
        elif "\n```" in rest:
            jsonl_body, end_fence, tail = rest.partition("\n```")
        else:
            jsonl_body, end_fence, tail = rest, "\n```", "\n"
        prefix = jsonl_body.rstrip("\n")
        updated_body = (prefix + "\n" + row) if prefix else row
        evidence_path.write_text(head + fence + updated_body + end_fence + tail, encoding="utf-8")
    else:
        suffix = "" if existing.endswith("\n") else "\n"
        evidence_path.write_text(
            existing + suffix + "\n```jsonl\n" + row + "\n```\n", encoding="utf-8"
        )
    return evidence_path


def _generate_card_id(scope: str, kind: str, title: str, body: str, proposal_id: str, existing_ids: set[str]) -> str:
    seed = f"{scope}|{kind}|{title}|{body}|{proposal_id}".encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()[:12]
    if scope.startswith("project/"):
        pid = scope.split("/", 1)[1]
        prefix = f"d-{pid}-"
    elif kind == "preference" or scope == "shared/preferences":
        prefix = "p-"
    elif kind == "method" or scope == "shared/methods":
        prefix = "m-"
    else:
        prefix = "k-"
    candidate = prefix + digest
    idx = 1
    while candidate in existing_ids:
        candidate = f"{prefix}{digest}-{idx}"
        idx += 1
    return candidate


def publish_proposals(
    db: sqlite3.Connection,
    vault_path: str | Path | None,
    proposal_ids: list[str],
) -> dict:
    """Publish one or more pending proposals directly into the Vault and commit to Git."""
    if not proposal_ids or len(set(proposal_ids)) != len(proposal_ids):
        raise ValueError("Select at least one pending proposal to publish.")

    vault = resolve_vault(vault_path)
    touched: list[Path] = []
    published_cards: list[dict] = []
    new_project_created = False

    for pid_key in proposal_ids:
        row = db.execute("SELECT * FROM proposals WHERE id=?", (pid_key,)).fetchone()
        if not row or row["state"] in CLOSED_STATES:
            raise ValueError("Selected proposal state has changed; please refresh.")

        p = dict(row)
        title = (p.get("title") or p["body"][:40]).strip()
        body = p["body"].strip()
        quote = (p.get("quote") or "").strip()
        validate_proposal_text(title, body, quote)

        reader = VaultReader(vault)
        raw_scope = p.get("scope") or (
            f"project/{p['project']}" if p.get("project") else ""
        )
        norm_scope, kind, proj_title = normalize_scope(raw_scope, reader)

        eff_from = (p.get("effective_from") or p.get("date") or date.today().isoformat()).strip()
        if _parse_date(eff_from) is None:
            eff_from = date.today().isoformat()

        if norm_scope.startswith("project/new:"):
            project_name = norm_scope.split("project/new:", 1)[1].strip()
            proj_id, start_path, created = _ensure_project_start(vault, reader, project_name)
            norm_scope = f"project/{proj_id}"
            touched.append(start_path)
            if created:
                new_project_created = True
            reader = VaultReader(vault)
        elif norm_scope.startswith("project/"):
            proj_id = norm_scope.split("/", 1)[1]
            if proj_id not in reader.projects:
                proj_id, start_path, created = _ensure_project_start(vault, reader, proj_title or proj_id)
                norm_scope = f"project/{proj_id}"
                touched.append(start_path)
                if created:
                    new_project_created = True
                reader = VaultReader(vault)

        new_id = _generate_card_id(
            norm_scope, kind, title, body, p["id"], set(reader.cards.keys())
        )

        target_id = (p.get("target") or "").strip()
        supersedes_list: list[str] = []
        subj = ""
        if target_id:
            old_card = reader.cards.get(target_id)
            if not old_card:
                raise ValueError(f"Card to supersede not found: {target_id}")
            if old_card.scope != norm_scope:
                raise ValueError("Superseded card belongs to a different scope.")
            supersedes_list.append(target_id)
            subj = old_card.subject_key

            old_meta = dict(old_card.meta)
            old_meta["status"] = "superseded"
            old_meta["superseded_by"] = new_id
            old_from = _parse_date(old_card.effective_from)
            new_from = _parse_date(eff_from)
            if old_from and new_from and new_from > old_from:
                old_meta["effective_until"] = (new_from - timedelta(days=1)).isoformat()
            else:
                old_meta["effective_until"] = eff_from
            old_card.path.write_text(
                dump_frontmatter(old_meta, old_card.body), encoding="utf-8"
            )
            touched.append(old_card.path)

        if not subj:
            scope_key = norm_scope.split("/", 1)[1] if norm_scope.startswith("project/") else norm_scope
            subj = subject_key(scope_key, title)

        source_refs: list[str] = []
        if quote:
            event_id = f"ev-{p['id']}"
            ev_proj = norm_scope.split("/", 1)[1] if norm_scope.startswith("project/") else norm_scope
            ev_path = _append_evidence_event(
                vault,
                event_id=event_id,
                project=ev_proj,
                at=eff_from,
                actor=p.get("client") or "owner",
                text=quote,
                location=f"MemoryS / {p.get('client') or 'manual'}",
            )
            touched.append(ev_path)
            source_refs.append(f"event:{event_id}")

        triggers = _parse_use_when(p.get("use_when") or "[]")
        if not triggers:
            triggers = [title]

        card_meta: dict = {
            "id": new_id,
            "kind": kind,
            "scope": norm_scope,
            "status": "active",
            "basis": "owner_confirmed",
            "summary": title,
            "use_when": triggers,
            "effective_from": eff_from,
            "subject_key": subj,
        }
        if supersedes_list:
            card_meta["supersedes"] = supersedes_list
        if source_refs:
            card_meta["source_refs"] = source_refs

        if norm_scope.startswith("project/"):
            proj_id = norm_scope.split("/", 1)[1]
            card_dir = vault / "projects" / proj_id
            card_dir.mkdir(parents=True, exist_ok=True)
            card_path = card_dir / f"{new_id}.md"
            card_path.write_text(dump_frontmatter(card_meta, body), encoding="utf-8")
            touched.append(card_path)
            start_path = card_dir / "START.md"
            if start_path.is_file():
                _append_project_card_link(start_path, title, new_id)
                touched.append(start_path)
        else:
            folder = norm_scope.split("/", 1)[1]
            card_dir = vault / "notes" / folder
            card_dir.mkdir(parents=True, exist_ok=True)
            card_path = card_dir / f"{new_id}.md"
            card_path.write_text(dump_frontmatter(card_meta, body), encoding="utf-8")
            touched.append(card_path)

        published_cards.append({"id": new_id, "summary": title, "scope": norm_scope, "proposal_id": p["id"]})

    if new_project_created:
        touched.append(_regenerate_project_index(vault))

    summary_names = ", ".join(f"{c['summary']} ({c['id']})" for c in published_cards)
    commit_msg = f"memorys: publish {summary_names}"
    commit_hash = commit_vault_files(vault, touched, commit_msg)

    with db:
        for item in published_cards:
            db.execute(
                "UPDATE proposals SET state='published', scope=? WHERE id=?",
                (item["scope"], item["proposal_id"]),
            )

    return {
        "revision": commit_hash[:10],
        "count": len(published_cards),
        "cards": published_cards,
    }


def archive_card(vault_path: str | Path | None, card_id: str) -> dict:
    """Mark an active card as archived and commit to Git immediately."""
    vault = resolve_vault(vault_path)
    reader = VaultReader(vault)
    card = reader.cards.get((card_id or "").strip())
    if not card:
        raise ValueError(f"Memory card not found: {card_id}")
    if card.status == "archived":
        raise ValueError("Memory card is already archived.")
    meta = dict(card.meta)
    meta["status"] = "archived"
    card.path.write_text(dump_frontmatter(meta, card.body), encoding="utf-8")
    commit_hash = commit_vault_files(
        vault, [card.path], f"memorys: archive {card.summary} ({card.card_id})"
    )
    return {"id": card.card_id, "status": "archived", "revision": commit_hash[:10]}


def update_project_meta(
    vault_path: str | Path | None,
    project_id: str,
    aliases: list[str] | str,
    paths: list[str] | str,
) -> dict:
    """Update a project's START.md aliases and local paths and commit to Git."""
    vault = resolve_vault(vault_path)
    reader = VaultReader(vault)
    info = reader.resolve_project(project_id)
    if not info:
        raise ValueError(f"Project not found: {project_id}")

    raw_meta, body = parse_frontmatter(info.start_path.read_text(encoding="utf-8"))
    clean_aliases = _parse_use_when(aliases)
    if isinstance(paths, str):
        clean_paths = [p.strip() for p in paths.splitlines() if p.strip()]
    else:
        clean_paths = [str(p).strip() for p in paths if str(p).strip()]
    dedup_paths = []
    for p in clean_paths:
        if p not in dedup_paths:
            dedup_paths.append(p)

    meta = {
        "project_id": info.project_id,
        "title": info.title,
        "aliases": clean_aliases,
        "paths": dedup_paths,
    }
    for k, v in raw_meta.items():
        if k not in meta:
            meta[k] = v

    info.start_path.write_text(dump_frontmatter(meta, body), encoding="utf-8")
    commit_hash = commit_vault_files(
        vault, [info.start_path], f"memorys: update project {info.title} ({info.project_id})"
    )
    return {
        "project_id": info.project_id,
        "title": info.title,
        "aliases": clean_aliases,
        "paths": dedup_paths,
        "revision": commit_hash[:10],
    }
