"""UserPromptSubmit recall hook: pure local, <50ms, dual-layer recall from MemoryS Vault."""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys

_RUNTIME = Path(__file__).resolve().parent
if str(_RUNTIME) not in sys.path:
    sys.path.insert(0, str(_RUNTIME))

try:
    from runtime.config import resolve_vault
    from runtime.memory_reader import VaultReader
except ImportError:
    from config import resolve_vault
    from memory_reader import VaultReader


PREF_CHAR_BUDGET = 1200
PROJECT_CHAR_BUDGET = 2200
SHARED_CHAR_BUDGET = 1000


def _format_card(card, include_body: bool = True) -> str:
    header = f"- [{card.card_id}] {card.summary} (effective: {card.effective_from or 'active'})"
    if not include_body or not card.body:
        return header
    return header + "\n  " + "\n  ".join(card.body.splitlines())


def recall(payload: dict, vault_path: str | Path | None = None) -> dict:
    """Build UserPromptSubmit additionalContext from preferences + matched projects/methods."""
    prompt = str(payload.get("prompt") or payload.get("user_prompt") or "").strip()
    cwd = str(payload.get("cwd") or "").strip()
    if not prompt and not cwd:
        return {}

    vault = resolve_vault(vault_path)
    if not vault.is_dir():
        return {}

    reader = VaultReader(vault)
    today = date.today()
    sections: list[str] = []

    # Layer 1: Always-on personal preferences (shared/preferences)
    pref_cards = [
        c
        for c in reader.cards.values()
        if c.scope == "shared/preferences" and c.applies(today, reader.cards)
    ]
    pref_cards.sort(key=lambda c: (c.effective_from, c.card_id), reverse=True)
    if pref_cards:
        lines = ["## Personal Preferences (Always-On)"]
        used = 0
        for c in pref_cards:
            full = _format_card(c, include_body=True)
            if used + len(full) <= PREF_CHAR_BUDGET:
                lines.append(full)
                used += len(full)
            else:
                short = _format_card(c, include_body=False)
                if used + len(short) <= PREF_CHAR_BUDGET + 200:
                    lines.append(short)
                    used += len(short)
        if len(lines) > 1:
            sections.append("\n".join(lines))

    # Layer 2a: Dual-path project recognition (cwd paths OR text mention of title/id/aliases)
    matched_projects = {}
    for info in reader.match_projects_by_cwd(cwd):
        matched_projects[info.project_id] = info
    for info in reader.match_projects_in_text(prompt):
        matched_projects[info.project_id] = info

    for _pid, info in matched_projects.items():
        proj_cards = [
            c
            for c in reader.cards.values()
            if c.scope == info.scope and c.applies(today, reader.cards)
        ]
        if not proj_cards:
            continue
        proj_cards.sort(
            key=lambda c: (reader.score_card(c, prompt), c.effective_from, c.card_id),
            reverse=True,
        )
        lines = [f"## Project Memory: {info.title} ({info.project_id})"]
        used = 0
        for idx, c in enumerate(proj_cards):
            if idx < 5:
                entry = _format_card(c, include_body=True)
                if used + len(entry) > PROJECT_CHAR_BUDGET and idx > 0:
                    entry = _format_card(c, include_body=False)
            else:
                entry = _format_card(c, include_body=False)
            lines.append(entry)
            used += len(entry)
        sections.append("\n".join(lines))

    # Layer 2b: Shared methods & domain knowledge matched by prompt relevance
    if prompt:
        shared_hits = []
        for c in reader.cards.values():
            if c.scope not in {"shared/methods", "shared/domain"}:
                continue
            if not c.applies(today, reader.cards):
                continue
            sc = reader.score_card(c, prompt)
            if sc >= 8:
                shared_hits.append((sc, c.effective_from, c))
        shared_hits.sort(key=lambda item: (item[0], item[1], item[2].card_id), reverse=True)
        if shared_hits:
            lines = ["## Shared Methods & Domain Knowledge"]
            used = 0
            for _, _, c in shared_hits[:3]:
                entry = _format_card(c, include_body=(used < SHARED_CHAR_BUDGET))
                lines.append(entry)
                used += len(entry)
            sections.append("\n".join(lines))

    if not sections:
        return {}

    context = (
        "[MemoryS Recall]\n"
        "Confirmed, read-only memories recalled for this turn. "
        "Use `memory_search` (supports `as_of`), `memory_read`, or `memory_trace` for full details or provenance.\n\n"
        + "\n\n".join(sections)
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault")
    args = parser.parse_args()
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        out = recall(payload, vault_path=args.vault or None)
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    except Exception:
        sys.stdout.write("{}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
