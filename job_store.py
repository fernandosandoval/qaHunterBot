"""
job_store.py
SQLite-backed history of processed job offers.

Features:
- Dedupe by source id AND by cross-source fingerprint (same company + title posted on
  several boards is evaluated/notified only once).
- Atomic writes (SQLite transactions) instead of rewriting a JSON file.
- One-time automatic migration from the legacy data/processed_jobs.json.
- Statuses: "rejected", "notified" and "legacy" (migrated records).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_DB_PATH = Path(os.getenv("JOB_DB_PATH", "data/jobs.db"))
LEGACY_JSON_PATH = Path("data/processed_jobs.json")

STATUS_REJECTED = "rejected"
STATUS_NOTIFIED = "notified"
STATUS_LEGACY = "legacy"

_GENERIC_COMPANIES = {
    "",
    "remote client",
    "cliente freelance",
    "unknown client",
    "indian startup",
    "indian startup hasjob",
    "confidential",
    "stealth startup",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    fingerprint   TEXT NOT NULL,
    source        TEXT,
    title         TEXT,
    company       TEXT,
    url           TEXT,
    job_type      TEXT,
    location      TEXT,
    is_india      INTEGER DEFAULT 0,
    is_viable     INTEGER DEFAULT 0,
    match_score   INTEGER DEFAULT 0,
    status        TEXT NOT NULL,
    notes         TEXT,
    processed_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(fingerprint);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _normalize(text: str, strip_work_terms: bool = True) -> str:
    """Lowercase, strip accents/parentheticals/punctuation and collapse whitespace."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", text.lower())
    if strip_work_terms:
        text = re.sub(r"\b(remote|remoto|worldwide|anywhere|hybrid|full[- ]?time|part[- ]?time|contract)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _is_generic_company(company_norm: str) -> bool:
    return company_norm in _GENERIC_COMPANIES or company_norm.startswith("feed ") or company_norm.startswith("customfeed")


def job_fingerprint(title: str, company: str, description: str = "", url: str = "") -> str:
    """
    Stable cross-source identifier.
    - Known company: hash(company + title), so the same offer on Remotive and WWR collides.
    - Generic/unknown company: hash(title + start of description) (or URL when no text), to avoid
      merging unrelated postings that merely share a title.
    """
    title_n = _normalize(title)
    company_n = _normalize(company, strip_work_terms=False)
    if _is_generic_company(company_n):
        body = _normalize(description)[:300] or (url or "").strip().lower()
        key = f"generic|{title_n}|{body}"
    else:
        key = f"{company_n}|{title_n}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def fingerprint_of(job: Any) -> str:
    return job_fingerprint(
        getattr(job, "title", ""),
        getattr(job, "company", ""),
        getattr(job, "description", ""),
        getattr(job, "url", ""),
    )


class JobHistoryManager:
    """Tracks evaluated jobs to avoid re-evaluating or re-notifying them."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        legacy_json: Path | str | None = LEGACY_JSON_PATH,
    ) -> None:
        self.db_path = Path(db_path or DEFAULT_DB_PATH)
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        if legacy_json:
            self._migrate_legacy_json(Path(legacy_json))

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "JobHistoryManager":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def save(self) -> None:
        """Kept for backwards compatibility: every write is already committed."""
        self._conn.commit()

    # -- migration ---------------------------------------------------------

    def _migrate_legacy_json(self, json_path: Path) -> None:
        done = self._conn.execute("SELECT value FROM meta WHERE key='legacy_json_migrated'").fetchone()
        if done or not json_path.exists():
            return
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[WARN] No se pudo leer {json_path} para migrar: {exc}", file=sys.stderr)
            return

        rows = []
        for job_id, rec in data.items():
            rows.append(
                (
                    job_id,
                    job_fingerprint(rec.get("title", ""), rec.get("company", ""), "", rec.get("url", "")),
                    rec.get("source", ""),
                    rec.get("title", ""),
                    rec.get("company", ""),
                    rec.get("url", ""),
                    rec.get("job_type", ""),
                    rec.get("location", ""),
                    int(bool(rec.get("is_india"))),
                    int(bool(rec.get("is_viable"))),
                    int(rec.get("match_score") or 0),
                    STATUS_LEGACY,
                    rec.get("notes", ""),
                    rec.get("processed_at") or datetime.now(timezone.utc).isoformat(),
                )
            )
        with self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO jobs (id, fingerprint, source, title, company, url, job_type, location,"
                " is_india, is_viable, match_score, status, notes, processed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('legacy_json_migrated', '1')")
        print(f"[*] Historial migrado desde {json_path.name} a SQLite: {len(rows)} registros.")

    # -- queries -----------------------------------------------------------

    def is_processed(self, job_id: str) -> bool:
        return self._conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is not None

    def is_known(self, job: Any) -> bool:
        """True if this job (by id) or an equivalent posting from another source was already processed."""
        row = self._conn.execute(
            "SELECT 1 FROM jobs WHERE id = ? OR fingerprint = ? LIMIT 1", (job.id, fingerprint_of(job))
        ).fetchone()
        return row is not None

    def stats(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        result = {r["status"]: r["n"] for r in rows}
        result["total"] = sum(result.values())
        return result

    # -- writes ------------------------------------------------------------

    def mark_processed(
        self,
        job: Any,
        is_viable: bool,
        match_score: int = 0,
        notes: str = "",
    ) -> None:
        status = STATUS_NOTIFIED if is_viable else STATUS_REJECTED
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO jobs (id, fingerprint, source, title, company, url, job_type, location,"
                " is_india, is_viable, match_score, status, notes, processed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job.id,
                    fingerprint_of(job),
                    getattr(job, "source", ""),
                    getattr(job, "title", ""),
                    getattr(job, "company", ""),
                    getattr(job, "url", ""),
                    getattr(job, "job_type", ""),
                    getattr(job, "location", ""),
                    int(bool(getattr(job, "is_india", False))),
                    int(bool(is_viable)),
                    int(match_score or 0),
                    status,
                    notes,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def forget_legacy(self) -> int:
        """
        Removes migrated (legacy) records so those jobs are evaluated again with the current
        pipeline. Useful because legacy records may come from the old mock-based evaluation.
        """
        with self._conn:
            cur = self._conn.execute("DELETE FROM jobs WHERE status = ?", (STATUS_LEGACY,))
        return cur.rowcount
