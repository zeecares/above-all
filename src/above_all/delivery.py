"""Conflict-free delivery of approved memory into provider-native context files.

One fact has one durable owner: above-all. Each provider receives the same
approved global/project notes inside a clearly marked managed section of its
native context file, with a manifest recording the source note IDs, a content
hash, a size budget, and an expiry. Content outside the managed markers is
user-owned: delivery never edits it, and drift inside the managed section is
detected and surfaced instead of overwritten. Provider-native memory (the
unmanaged remainder of the file) can be imported only as deduplicated
review candidates - never silently, never directly into active memory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from .agent_context import merged_active_notes
from .notes import list_candidates, parse_note
from .providers import ProviderSpec, get_provider

BEGIN = "<!-- above-all:managed begin -->"
END = "<!-- above-all:managed end -->"
DEFAULT_BUDGET_BYTES = 4000
DEFAULT_TTL_DAYS = 7
MAX_IMPORT_BLOCKS = 20

_SECTION_RE = re.compile(
    re.escape(BEGIN) + r"\n?(.*?)\n?" + re.escape(END), re.DOTALL
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_claim(text: str) -> str:
    """Normalized claim text for dedup, matching the write gate's heading strip."""
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return " ".join(" ".join(lines).split()).casefold()


def _manifest_path(scope_dir: Path, spec: ProviderSpec) -> Path:
    return scope_dir / "delivery" / f"{spec.name}.json"


def _load_manifest(scope_dir: Path, spec: ProviderSpec) -> dict | None:
    path = _manifest_path(scope_dir, spec)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"corrupt delivery manifest {path}; delete it and re-deliver") from exc


def _write_manifest(scope_dir: Path, spec: ProviderSpec, manifest: dict) -> None:
    path = _manifest_path(scope_dir, spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    staged.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(staged, path)


def render_section(notes: list[dict], budget_bytes: int) -> tuple[str, list[str], bool]:
    """Render approved notes into the managed section body within the exact byte budget."""
    if budget_bytes <= 0:
        raise ValueError("delivery budget must be a positive number of bytes")
    parts = [
        "# Project context (managed by above-all)",
        "",
        (
            "_Regenerate with `above-all deliver`. Edits inside the managed markers "
            "are detected as drift; keep your own content outside them._"
        ),
        "",
    ]
    section = "\n".join(parts).strip()
    if len(section.encode("utf-8")) > budget_bytes:
        raise ValueError("delivery budget is too small for the managed-section header")
    included: list[str] = []
    truncated = False
    for note in notes:
        body = note["body"].strip()
        if BEGIN in body or END in body or BEGIN in note["title"] or END in note["title"]:
            raise ValueError(
                f"note {note['id']!r} contains an above-all managed-section marker; "
                "refusing to render because it would corrupt section extraction"
            )
        if body.startswith("# "):
            body = body.split("\n", 1)[1].strip() if "\n" in body else ""
        block = f"## {note['title']}\n\n_Source: {note['scope']} knowledge, note {note['id']}_\n\n{body}"
        candidate = section + "\n" + block
        if len(candidate.encode("utf-8")) > budget_bytes:
            truncated = True
            continue
        section = candidate
        included.append(note["id"])
    return section, included, truncated


def _extract_section(text: str) -> str | None:
    match = _SECTION_RE.search(text)
    return match.group(1) if match else None


def _section_matches(text: str) -> list[re.Match]:
    return list(_SECTION_RE.finditer(text))


def _replace_section(text: str, section: str) -> str:
    block = f"{BEGIN}\n{section}\n{END}"
    matches = _section_matches(text)
    if len(matches) > 1:
        raise ValueError("multiple above-all managed sections found; refusing ambiguous rewrite")
    if matches:
        match = matches[0]
        return text[: match.start()] + block + text[match.end() :]
    if not text:
        return block + "\n"
    separator = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    return text + separator + block + "\n"


def _manifest_fresh(manifest: dict, now: datetime) -> bool:
    expires = manifest.get("expires_at")
    return bool(expires) and datetime.fromisoformat(expires) > now


def check_delivery(scope_dir: Path, provider: str, *, now: datetime | None = None) -> dict:
    """Report the state of the delivered file without changing anything."""
    now = now or _utcnow()
    spec = get_provider(provider)
    target = scope_dir.parent / spec.context_filename
    manifest = _load_manifest(scope_dir, spec)
    result = {"provider": spec.name, "file": str(target), "manifest": manifest is not None}
    if not target.is_file():
        return result | {"status": "missing", "detail": "no delivered file"}
    text = target.read_text(encoding="utf-8")
    matches = _section_matches(text)
    if len(matches) > 1:
        return result | {
            "status": "ambiguous",
            "detail": "multiple managed sections found; refusing to choose one",
        }
    section = matches[0].group(1) if matches else None
    if section is None:
        return result | {
            "status": "unmanaged",
            "detail": "file exists with no above-all managed section; content is user-owned",
        }
    if manifest is None:
        return result | {
            "status": "no-manifest",
            "detail": "managed section present but no manifest; refusing to treat it as ours",
        }
    result["expires_at"] = manifest.get("expires_at")
    if _hash(section) != manifest.get("content_hash"):
        return result | {
            "status": "drifted",
            "detail": "managed section was edited outside above-all; review the edits in "
            "the file itself (import strips managed sections), then `deliver --force` to regenerate",
        }
    if not _manifest_fresh(manifest, now):
        return result | {"status": "expired", "detail": "manifest past its expiry; regenerate"}
    return result | {"status": "clean", "detail": "delivered context matches the manifest"}


def deliver(
    scope_dir: Path,
    db: sqlite3.Connection,
    backend,
    global_db: sqlite3.Connection | None = None,
    *,
    provider: str,
    force: bool = False,
    budget_bytes: int = DEFAULT_BUDGET_BYTES,
    ttl_days: int = DEFAULT_TTL_DAYS,
    now: datetime | None = None,
) -> dict:
    """Write approved memory into the provider's native context file.

    Refuses to touch the file when the managed section drifted or the manifest
    is missing, unless force=True. Content outside the markers is never edited.
    """
    now = now or _utcnow()
    if ttl_days <= 0:
        raise ValueError("delivery TTL must be a positive number of days")
    spec = get_provider(provider)
    target = scope_dir.parent / spec.context_filename
    notes = merged_active_notes(global_db or db, db if global_db else None, backend)
    section, included, truncated = render_section(notes, budget_bytes)
    content_hash = _hash(section)
    manifest = _load_manifest(scope_dir, spec)
    eligible = [note["id"] for note in notes]

    if target.is_file():
        text = target.read_text(encoding="utf-8")
        matches = _section_matches(text)
        if len(matches) > 1:
            raise ValueError("multiple above-all managed sections found; refusing ambiguous rewrite")
        existing = matches[0].group(1) if matches else None
        if existing is not None:
            if manifest is None:
                if not force:
                    return {
                        "status": "refused",
                        "reason": "no-manifest",
                        "detail": "managed section exists but no manifest records it; "
                        "rerun with --force to adopt and overwrite that section",
                        "file": str(target),
                    }
            elif _hash(existing) != manifest.get("content_hash") and not force:
                return {
                    "status": "refused",
                    "reason": "drifted",
                    "detail": "managed section was edited outside above-all; "
                    "review with `above-all import` or rerun with --force",
                    "file": str(target),
                }
            elif (
                _hash(existing) == content_hash
                and manifest.get("source_note_ids") == included
                and manifest.get("budget_bytes") == budget_bytes
                and manifest.get("ttl_days") == ttl_days
                and _manifest_fresh(manifest, now)
            ):
                return {
                    "status": "unchanged",
                    "file": str(target),
                    "source_note_ids": included,
                    "truncated": truncated,
                }
        new_text = _replace_section(text, section)
    else:
        new_text = f"{BEGIN}\n{section}\n{END}\n"

    staged = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    staged.write_text(new_text, encoding="utf-8")
    os.replace(staged, target)
    _write_manifest(
        scope_dir,
        spec,
        {
            "provider": spec.name,
            "file": str(target),
            "generated_at": now.isoformat(),
            "expires_at": (now + timedelta(days=ttl_days)).isoformat(),
            "content_hash": content_hash,
            "source_note_ids": included,
            "eligible_note_ids": eligible,
            "budget_bytes": budget_bytes,
            "ttl_days": ttl_days,
            "truncated": truncated,
        },
    )
    return {
        "status": "written",
        "file": str(target),
        "source_note_ids": included,
        "truncated": truncated,
        "expires_at": (now + timedelta(days=ttl_days)).isoformat(),
    }


def _strip_managed_for_import(text: str) -> str:
    """Remove managed bytes conservatively, including a drifted/unclosed section."""
    unmanaged = _SECTION_RE.sub("", text)
    if BEGIN in unmanaged:
        unmanaged = unmanaged.split(BEGIN, 1)[0]
    if END in unmanaged:
        unmanaged = unmanaged.split(END, 1)[1]
    return unmanaged


def _import_blocks(text: str) -> list[tuple[str, str]]:
    """Split unmanaged text into (title, body) blocks on markdown headings."""
    unmanaged = _strip_managed_for_import(text)
    blocks: list[tuple[str, str]] = []
    current_title: str | None = None
    current: list[str] = []
    for line in unmanaged.splitlines():
        if line.startswith("#"):
            if current_title is not None or "".join(current).strip():
                blocks.append((current_title or "Imported memory", "\n".join(current).strip()))
            current_title = line.lstrip("#").strip() or "Imported memory"
            current = []
        else:
            current.append(line)
    if current_title is not None or "".join(current).strip():
        blocks.append((current_title or "Imported memory", "\n".join(current).strip()))
    return [(title, body) for title, body in blocks if body.strip()]


def import_provider_memory(
    scope_dir: Path,
    db: sqlite3.Connection,
    backend,
    global_db: sqlite3.Connection | None = None,
    *,
    provider: str,
    path: Path | None = None,
) -> dict:
    """Import provider-native memory as deduplicated review-only candidates.

    The managed section is never imported (it is ours). Exact duplicates of
    active notes, knowledge-map claims, or pending candidates are skipped.
    Nothing here promotes anything; approval stays on the human review path.
    """
    spec = get_provider(provider)
    target = path or (scope_dir.parent / spec.context_filename)
    if not target.is_file():
        return {"provider": spec.name, "file": str(target), "created": [], "skipped_duplicates": 0, "blocks": 0}

    active_claims: set[str] = set()
    for source_db in (db, global_db):
        if source_db is None:
            continue
        active_claims |= {
            _normalize_claim(row[0])
            for row in source_db.execute(
                "SELECT body FROM notes WHERE status='active'"
            ).fetchall()
        }
        try:
            active_claims |= {
                _normalize_claim(row[0])
                for row in source_db.execute(
                    "SELECT text FROM knowledge_claims WHERE status='active'"
                ).fetchall()
            }
        except sqlite3.OperationalError:
            pass  # knowledge map predates this scope's migrations
    candidates_dir = scope_dir / "candidates"
    pending_claims = set()
    for item in list_candidates(candidates_dir):
        pending_claims.add(
            _normalize_claim(parse_note((candidates_dir / f"{item['id']}.md").read_text()).body)
        )

    blocks = _import_blocks(target.read_text(encoding="utf-8"))
    created: list[dict] = []
    skipped = 0
    for title, body in blocks[:MAX_IMPORT_BLOCKS]:
        claim = _normalize_claim(body)
        if not claim or claim in active_claims or claim in pending_claims:
            skipped += 1
            continue
        try:
            relative = str(target.relative_to(scope_dir.parent))
        except ValueError:
            relative = str(target)
        candidate = backend.create_candidate(
            scope_dir,
            title=title,
            body=body,
            note_type="fact",
            sources=[f"provider:{spec.name}", f"file:{relative}"],
            extra={
                "observer": f"provider:{spec.name}",
                "import_kind": "provider-native",
            },
        )
        pending_claims.add(claim)
        created.append({"id": candidate.stem, "title": title})
    overflow = max(0, len(blocks) - MAX_IMPORT_BLOCKS)
    return {
        "provider": spec.name,
        "file": str(target),
        "created": created,
        "skipped_duplicates": skipped,
        "blocks": len(blocks),
        "overflow_not_imported": overflow,
    }
