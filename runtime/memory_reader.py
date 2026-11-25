"""Read published memories, projects, and provenance directly from a MemoryS Vault.

Zero external dependencies: YAML frontmatter parsing, temporal filtering (`as_of`),
supersession chains, and bilingual (EN/ZH) vault compatibility all use Python stdlib.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import hashlib
import json
from pathlib import Path
import re

try:
    from runtime.config import resolve_vault, vault_child
except ImportError:
    from config import resolve_vault, vault_child


def _strip_comment(text: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    for idx, ch in enumerate(text):
        if escaped:
            escaped = False
            continue
        if in_double:
            if ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
        elif in_single:
            if ch == "'":
                in_single = False
        else:
            if ch == '"':
                in_double = True
            elif ch == "'":
                in_single = True
            elif ch == "#" and (idx == 0 or text[idx - 1].isspace()):
                return text[:idx].rstrip()
    return text.rstrip()


def _parse_scalar(raw: str):
    val = _strip_comment(raw.strip())
    if not val or val in {"null", "Null", "NULL", "~"}:
        return ""
    if val in {"true", "True", "TRUE"}:
        return True
    if val in {"false", "False", "FALSE"}:
        return False
    if val.startswith('"') and val.endswith('"') and len(val) >= 2:
        try:
            return json.loads(val)
        except ValueError:
            return val[1:-1]
    if val.startswith("'") and val.endswith("'") and len(val) >= 2:
        return val[1:-1].replace("''", "'")
    if val.startswith("[") and val.endswith("]"):
        return _parse_inline_list(val[1:-1])
    if re.fullmatch(r"-?\d+", val):
        return int(val)
    return val


def _parse_inline_list(inner: str) -> list:
    items = []
    buf = []
    in_single = False
    in_double = False
    escaped = False
    for ch in inner:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if in_double:
            buf.append(ch)
            if ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
        elif in_single:
            buf.append(ch)
            if ch == "'":
                in_single = False
        else:
            if ch == '"':
                in_double = True
                buf.append(ch)
            elif ch == "'":
                in_single = True
                buf.append(ch)
            elif ch == ",":
                token = "".join(buf).strip()
                if token:
                    items.append(_parse_scalar(token))
                buf = []
            else:
                buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        items.append(_parse_scalar(tail))
    return items


def parse_yaml_mapping(text: str) -> dict:
    """Parse a flat YAML mapping with scalars and inline/block lists using stdlib."""
    data: dict = {}
    current_list_key: str | None = None
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        stripped = raw_line.lstrip()
        if stripped.startswith("- ") or stripped == "-":
            if current_list_key is not None:
                item_raw = stripped[2:] if len(stripped) > 2 else ""
                data[current_list_key].append(_parse_scalar(item_raw))
            continue
        if ":" not in raw_line:
            continue
        key, _, rest = raw_line.partition(":")
        key = key.strip()
        if not key:
            continue
        val_raw = _strip_comment(rest.strip())
        if not val_raw:
            data[key] = []
            current_list_key = key
        else:
            parsed = _parse_scalar(val_raw)
            data[key] = parsed
            current_list_key = key if isinstance(parsed, list) and not parsed else None
    return data


def parse_frontmatter(document: str) -> tuple[dict, str]:
    """Split YAML frontmatter and markdown body."""
    normalized = document.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized.strip()
    rest = normalized[4:]
    match = re.search(r"\n---(?:\n|$)", rest)
    if not match:
        return {}, normalized.strip()
    meta_text = rest[: match.start()]
    body = rest[match.end() :].strip()
    return parse_yaml_mapping(meta_text), body


def _format_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, date):
        return f"'{value.isoformat()}'"
    if isinstance(value, int):
        return str(value)
    text = "" if value is None else str(value)
    if (
        text == ""
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}", text)
        or re.fullmatch(r"-?\d+(?:\.\d+)?", text)
        or text in {"true", "false", "null", "True", "False", "None", "~"}
        or any(ch in text for ch in (":", "#", "\n", '"', "'", "[", "]", "{", "}", ","))
        or text.startswith(("-", "!", "&", "*", "?", "|", ">", "@", "`", " "))
        or text.endswith(" ")
    ):
        return json.dumps(text, ensure_ascii=False)
    return text


def dump_frontmatter(meta: dict, body: str) -> str:
    """Serialize metadata dict and markdown body into standard YAML frontmatter markdown."""
    lines = ["---"]
    for key, value in meta.items():
        if value is None:
            continue
        if isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                for item in value:
                    lines.append(f"- {_format_scalar(item)}")
        else:
            lines.append(f"{key}: {_format_scalar(value)}")
    lines.append("---")
    content = "\n".join(lines) + "\n"
    cleaned_body = (body or "").strip()
    if cleaned_body:
        content += "\n" + cleaned_body + "\n"
    return content


def _norm_token(text: str) -> str:
    return re.sub(r"[\s_-]+", "", (text or "").strip()).casefold()


def _as_str_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _parse_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _tokenize(query: str) -> set[str]:
    cleaned = (query or "").strip().casefold()
    if not cleaned:
        return set()
    terms = set(re.findall(r"[a-z0-9][a-z0-9_-]*|[\u3400-\u9fff]+", cleaned))
    for term in tuple(terms):
        if len(term) > 2 and "\u3400" <= term[0] <= "\u9fff":
            terms.update(term[i : i + 2] for i in range(len(term) - 1))
        elif len(term) > 1 and term.isascii() and ("-" in term or "_" in term):
            terms.update(part for part in re.split(r"[-_]+", term) if len(part) >= 2)
    return terms


def validate_project_id(identifier: str) -> str:
    if not identifier or identifier == "." or any(part in identifier for part in ("/", "\\", "..", ":")):
        raise ValueError(f"Invalid project ID: {identifier}")
    return identifier


def subject_key(project: str, topic: str) -> str:
    """Compute a stable subject_key for a topic within a project or shared scope."""
    norm_topic = topic.strip().casefold()
    return project + "." + hashlib.sha256(norm_topic.encode("utf-8")).hexdigest()[:24]


SCOPE_LABELS = {
    "shared/preferences": "Preferences",
    "shared/methods": "Methods",
    "shared/domain": "Domain Knowledge",
}


@dataclass
class ProjectInfo:
    project_id: str
    title: str
    aliases: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    start_path: Path = Path(".")
    body: str = ""

    @property
    def scope(self) -> str:
        return f"project/{self.project_id}"

    def all_names(self) -> list[str]:
        seen: list[str] = []
        for item in [self.title, self.project_id, *self.aliases]:
            val = str(item).strip()
            if val and val not in seen:
                seen.append(val)
        return seen


@dataclass
class Card:
    meta: dict
    body: str
    path: Path
    relpath: str
    project_title: str = ""

    @property
    def card_id(self) -> str:
        return str(self.meta.get("id") or self.path.stem)

    @property
    def kind(self) -> str:
        return str(self.meta.get("kind") or "decision")

    @property
    def scope(self) -> str:
        return str(self.meta.get("scope") or "")

    @property
    def status(self) -> str:
        return str(self.meta.get("status") or "active")

    @property
    def summary(self) -> str:
        return str(self.meta.get("summary") or self.card_id)

    @property
    def effective_from(self) -> str:
        return str(self.meta.get("effective_from") or "")

    @property
    def effective_until(self) -> str:
        return str(self.meta.get("effective_until") or "")

    @property
    def supersedes(self) -> list[str]:
        return _as_str_list(self.meta.get("supersedes"))

    @property
    def superseded_by(self) -> str:
        return str(self.meta.get("superseded_by") or "")

    @property
    def use_when(self) -> list[str]:
        return _as_str_list(self.meta.get("use_when"))

    @property
    def source_refs(self) -> list[str]:
        return _as_str_list(self.meta.get("source_refs"))

    @property
    def subject_key(self) -> str:
        return str(self.meta.get("subject_key") or "")

    def _in_date_range(self, day: date) -> bool:
        start = _parse_date(self.effective_from)
        end = _parse_date(self.effective_until)
        if start and day < start:
            return False
        if end and day > end:
            return False
        return True

    def _successor_applies(
        self, day: date, cards_by_id: dict[str, Card] | None, visited: set[str] | None = None
    ) -> bool:
        if not cards_by_id or not self.superseded_by:
            return False
        seen = visited or {self.card_id}
        nxt_id = self.superseded_by
        if nxt_id in seen:
            return False
        nxt = cards_by_id.get(nxt_id)
        if not nxt:
            return False
        seen.add(nxt_id)
        if nxt.status in {"active", "disputed"} and nxt._in_date_range(day):
            return True
        if nxt.status == "superseded" and nxt._in_date_range(day):
            if nxt._successor_applies(day, cards_by_id, seen):
                return True
            return True
        return False

    def applies(self, on: date | None = None, cards_by_id: dict[str, Card] | None = None) -> bool:
        status = self.status
        if status == "archived":
            return False
        day = on or date.today()
        if status in {"active", "disputed"}:
            return self._in_date_range(day)
        if status == "superseded":
            if not self.effective_until:
                return False
            if not self._in_date_range(day):
                return False
            if self._successor_applies(day, cards_by_id):
                return False
            return True
        return False

    def to_dict(self, include_body: bool = True) -> dict:
        data = {
            "id": self.card_id,
            "kind": self.kind,
            "scope": self.scope,
            "status": self.status,
            "basis": str(self.meta.get("basis") or "owner_confirmed"),
            "summary": self.summary,
            "use_when": self.use_when,
            "effective_from": self.effective_from,
            "effective_until": self.effective_until,
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
            "subject_key": self.subject_key,
            "source_refs": self.source_refs,
            "project_title": self.project_title,
            "path": self.relpath,
        }
        if include_body:
            data["body"] = self.body
        return data


class VaultReader:
    """Reads all projects, cards, and provenance from a MemoryS Obsidian vault."""

    def __init__(self, vault_path: str | Path | None = None):
        self.vault = resolve_vault(vault_path)
        self.projects: dict[str, ProjectInfo] = {}
        self.cards: dict[str, Card] = {}
        self._events_cache: dict[str, dict] | None = None
        self._load()

    def _load(self) -> None:
        projects_dir = self.vault / "projects"
        if projects_dir.is_dir():
            for start_file in sorted(projects_dir.glob("*/START.md")):
                meta, body = parse_frontmatter(vault_child(self.vault, start_file).read_text(encoding="utf-8"))
                pid = str(meta.get("project_id") or start_file.parent.name).strip()
                validate_project_id(pid)
                title = str(meta.get("title") or pid).strip()
                aliases = _as_str_list(meta.get("aliases"))
                paths = _as_str_list(meta.get("paths"))
                self.projects[pid] = ProjectInfo(
                    project_id=pid,
                    title=title,
                    aliases=aliases,
                    paths=paths,
                    start_path=start_file,
                    body=body,
                )

        if projects_dir.is_dir():
            for md_file in sorted(projects_dir.glob("*/*.md")):
                if md_file.name in {"START.md", "handoff.md"}:
                    continue
                self._load_card_file(md_file, default_scope=f"project/{md_file.parent.name}")

        notes_dir = self.vault / "notes"
        for folder_name in ("preferences", "methods", "domain"):
            sub = notes_dir / folder_name
            if not sub.is_dir():
                continue
            for md_file in sorted(sub.glob("*.md")):
                self._load_card_file(md_file, default_scope=f"shared/{folder_name}")

    def _load_card_file(self, md_file: Path, default_scope: str) -> None:
        raw = vault_child(self.vault, md_file).read_text(encoding="utf-8")
        meta, body = parse_frontmatter(raw)
        if not meta.get("id"):
            meta["id"] = md_file.stem
        if not meta.get("scope"):
            meta["scope"] = default_scope
        scope = str(meta.get("scope") or "")
        if scope.startswith("project/"):
            pid = validate_project_id(scope.split("/", 1)[1])
            info = self.projects.get(pid)
            project_title = info.title if info else pid
        else:
            project_title = SCOPE_LABELS.get(scope, "Shared")
        relpath = md_file.relative_to(self.vault).as_posix()
        card = Card(
            meta=meta,
            body=body,
            path=md_file,
            relpath=relpath,
            project_title=project_title,
        )
        self.cards[card.card_id] = card

    def resolve_project(self, identifier: str) -> ProjectInfo | None:
        key = (identifier or "").strip()
        if not key:
            return None
        if key.startswith("project/"):
            key = key.split("/", 1)[1]
        validate_project_id(key)
        if key in self.projects:
            return self.projects[key]
        norm_key = _norm_token(key)
        for info in self.projects.values():
            if any(_norm_token(name) == norm_key for name in info.all_names()):
                return info
        return None

    def match_projects_by_cwd(self, cwd: str) -> list[ProjectInfo]:
        if not cwd:
            return []
        try:
            resolved_cwd = Path(cwd).expanduser().resolve()
        except OSError:
            return []
        matched: list[ProjectInfo] = []
        for info in self.projects.values():
            for raw_path in info.paths:
                try:
                    p = Path(raw_path).expanduser().resolve()
                except OSError:
                    continue
                if resolved_cwd == p or p in resolved_cwd.parents:
                    matched.append(info)
                    break
        return matched

    def match_projects_in_text(self, text: str) -> list[ProjectInfo]:
        if not text:
            return []
        norm_text = _norm_token(text)
        lowered_text = text.casefold()
        matched: list[ProjectInfo] = []
        for info in self.projects.values():
            hit = False
            for name in info.all_names():
                n = _norm_token(name)
                if not n:
                    continue
                if n in norm_text:
                    if len(n) <= 4 and n.isascii():
                        pattern = rf"(?<![a-z0-9]){re.escape(name.strip().casefold())}(?![a-z0-9])"
                        alt_pattern = rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])"
                        if re.search(pattern, lowered_text) or re.search(alt_pattern, lowered_text):
                            hit = True
                            break
                    else:
                        hit = True
                        break
            if hit:
                matched.append(info)
        return matched

    def scope_label(self, scope: str) -> str:
        if scope in SCOPE_LABELS:
            return SCOPE_LABELS[scope]
        if scope.startswith("project/"):
            pid = validate_project_id(scope.split("/", 1)[1])
            info = self.projects.get(pid)
            return info.title if info else pid
        return scope

    def _normalize_scope_filter(self, scope: str, project: str) -> str:
        if project:
            info = self.resolve_project(project)
            if info:
                return info.scope
            pid = project.strip()
            return pid if pid.startswith("project/") else f"project/{pid}"
        s = (scope or "").strip()
        if not s:
            return ""
        if s.casefold() in SCOPE_LABELS:
            return s.casefold()
        aliases = {
            "preferences": "shared/preferences",
            "preference": "shared/preferences",
            "个人偏好": "shared/preferences",
            "methods": "shared/methods",
            "method": "shared/methods",
            "通用方法": "shared/methods",
            "domain": "shared/domain",
            "领域知识": "shared/domain",
        }
        if s.casefold() in aliases:
            return aliases[s.casefold()]
        if s in aliases:
            return aliases[s]
        if s.startswith("project/"):
            info = self.resolve_project(s)
            return info.scope if info else s
        info = self.resolve_project(s)
        return info.scope if info else s

    def score_card(self, card: Card, query: str) -> int:
        q = (query or "").strip().casefold()
        if not q:
            return 1
        if card.subject_key and card.subject_key.casefold() == q:
            return 1000
        if card.card_id.casefold() == q:
            return 1000
        terms = _tokenize(q)
        summary_cf = card.summary.casefold()
        body_cf = card.body.casefold()
        use_when_cf = " ".join(card.use_when).casefold()
        score = 0
        if q in summary_cf:
            score += 60
        if q in use_when_cf:
            score += 50
        if q in body_cf:
            score += 30
        for term in terms:
            if term in summary_cf:
                score += 12
            if term in use_when_cf:
                score += 10
            if term in body_cf:
                score += 4
        return score

    def search(
        self,
        query: str = "",
        project: str = "",
        scope: str = "",
        as_of: str | date | None = None,
        include_history: bool = False,
    ) -> list[dict]:
        target_date = _parse_date(as_of) if as_of else date.today()
        if as_of and target_date is None:
            raise ValueError(f"Invalid date format, expected YYYY-MM-DD: {as_of}")
        target_scope = self._normalize_scope_filter(scope, project)
        q = (query or "").strip()
        q_cf = q.casefold()
        exact_subjects = {
            c.subject_key
            for c in self.cards.values()
            if c.subject_key and c.subject_key.casefold() == q_cf
        }

        results: list[tuple[int, str, dict]] = []
        for card in self.cards.values():
            if target_scope and card.scope != target_scope:
                continue
            if exact_subjects and card.subject_key not in exact_subjects:
                continue
            applies_flag = card.applies(target_date, self.cards)
            if not include_history and not applies_flag:
                continue
            score = self.score_card(card, q)
            if q and score <= 0:
                continue
            item = card.to_dict(include_body=True)
            item["applies"] = applies_flag
            item["score"] = score
            results.append((score, card.effective_from, item))

        results.sort(key=lambda row: (row[0], row[1], row[2]["id"]), reverse=True)
        return [row[2] for row in results]

    def read_card(self, card_id: str, project: str = "") -> dict:
        key = (card_id or "").strip()
        if key == "handoff" and project:
            info = self.resolve_project(project)
            if info:
                ho = self.handoff(info.project_id)
                if ho:
                    return ho
        card = self.cards.get(key)
        if not card:
            raise ValueError(f"Memory card not found: {card_id}")
        data = card.to_dict(include_body=True)
        chain: dict[str, list[str]] = {"supersedes": list(card.supersedes), "superseded_by": []}
        cur = card
        seen = {cur.card_id}
        while cur.superseded_by and cur.superseded_by not in seen:
            seen.add(cur.superseded_by)
            chain["superseded_by"].append(cur.superseded_by)
            nxt = self.cards.get(cur.superseded_by)
            if not nxt:
                break
            cur = nxt
        data["supersession_chain"] = chain
        return data

    def handoff(self, project_id: str) -> dict | None:
        validate_project_id(project_id)
        info = self.resolve_project(project_id)
        if not info:
            return None
        handoff_file = vault_child(self.vault, info.start_path.parent / "handoff.md")
        if not handoff_file.is_file():
            return None
        return {
            "id": "handoff",
            "project_id": info.project_id,
            "project_title": info.title,
            "path": handoff_file.relative_to(self.vault).as_posix(),
            "body": handoff_file.read_text(encoding="utf-8").strip(),
        }

    def load_events(self) -> dict[str, dict]:
        if self._events_cache is not None:
            return self._events_cache
        events: dict[str, dict] = {}
        # Support both standard English SOURCES.md and legacy Chinese 来源记录.md
        for fname in ("SOURCES.md", "来源记录.md"):
            evidence_md = self.vault / fname
            if not evidence_md.is_file():
                continue
            text = vault_child(self.vault, evidence_md).read_text(encoding="utf-8")
            head, fence, rest = text.partition("\n```jsonl\n")
            if not fence:
                continue
            jsonl_body, _, _ = rest.partition("\n```")
            offset = head.count("\n") + 2
            for idx, line in enumerate(jsonl_body.splitlines(), start=offset + 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                eid = str(row.get("event_id") or "").strip()
                if eid and eid not in events:
                    row["path"] = fname
                    row["line"] = idx
                    events[eid] = row

        # Optional standalone evidence JSONL inside the vault
        vault_jsonl = self.vault / "evidence" / "events.jsonl"
        if vault_jsonl.is_file():
            for idx, line in enumerate(vault_child(self.vault, vault_jsonl).read_text(encoding="utf-8").splitlines(), start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                eid = str(row.get("event_id") or "").strip()
                if eid and eid not in events:
                    row["path"] = "evidence/events.jsonl"
                    row["line"] = idx
                    events[eid] = row

        self._events_cache = events
        return events

    def trace_card(self, card_id: str) -> dict:
        card = self.cards.get((card_id or "").strip())
        if not card:
            raise ValueError(f"Memory card not found: {card_id}")
        events = self.load_events()
        matched_events = []
        missing_refs = []
        for ref in card.source_refs:
            eid = ref.split(":", 1)[1] if ref.startswith("event:") else ref
            ev = events.get(eid)
            if ev:
                matched_events.append(ev)
            else:
                missing_refs.append(ref)
        return {
            "id": card.card_id,
            "summary": card.summary,
            "scope": card.scope,
            "project_title": card.project_title,
            "source_refs": card.source_refs,
            "events": matched_events,
            "missing_refs": missing_refs,
            "path": card.relpath,
        }
