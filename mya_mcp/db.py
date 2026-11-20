"""Stockage SQLite (FTS5) : construction atomique, requêtes et historique des changements."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("mya.db")

MAX_CHANGES_KEPT = 2000
SUMMARY_COLUMNS = (
    "id, url, university_name, country, language, diploma, spots, gpa_requirement, "
    "extra_charge_cents, erasmus, semester, specializations, display, suspended, updated_at"
)

SCHEMA = """
CREATE TABLE destinations (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL,
    university_name TEXT NOT NULL,
    country TEXT,
    language TEXT,
    diploma TEXT,
    spots INTEGER,
    gpa_requirement REAL,
    extra_charge_cents INTEGER,
    erasmus TEXT,
    semester TEXT,
    specializations TEXT NOT NULL DEFAULT '[]',
    display TEXT,
    suspended TEXT,
    updated_at TEXT,
    images TEXT NOT NULL DEFAULT '[]',
    full_text TEXT NOT NULL DEFAULT '',
    scraped_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE dest_fts USING fts5(
    name, country, language, specializations, full_text,
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_at TEXT NOT NULL,
    dest_id INTEGER NOT NULL,
    dest_name TEXT,
    change_type TEXT NOT NULL,   -- added | removed | modified
    field TEXT,                  -- champ modifié (modified uniquement)
    old_value TEXT,
    new_value TEXT
);
CREATE INDEX idx_changes_date ON changes(changed_at DESC);
"""

# Champs comparés entre deux scrapes (scraped_at exclu)
COMPARED_FIELDS = [
    "university_name", "country", "language", "diploma", "spots", "gpa_requirement",
    "extra_charge_cents", "erasmus", "semester", "specializations", "display",
    "suspended", "updated_at", "images", "full_text",
]


def fold(text) -> str:
    """Minuscules sans accents, pour comparaisons insensibles."""
    if text is None:
        return ""
    s = unicodedata.normalize("NFKD", str(text))
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path, readonly: bool = True) -> sqlite3.Connection:
    uri = f"file:{path}?mode={'ro' if readonly else 'rwc'}"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.create_function("fold", 1, fold, deterministic=True)
    return conn


def _row_values(d: dict) -> dict:
    """Valeurs normalisées pour insertion (listes en JSON)."""
    v = dict(d)
    v["specializations"] = json.dumps(d.get("specializations") or [], ensure_ascii=False)
    v["images"] = json.dumps(d.get("images") or [])
    return v


def _short(value) -> str | None:
    if value is None:
        return None
    s = str(value)
    return s if len(s) <= 200 else s[:200] + "..."


def read_all(path: Path) -> dict[int, dict]:
    """Relit toutes les fiches d'une base (pour comparaison et reprise)."""
    if not path.exists():
        return {}
    try:
        conn = connect(path)
        try:
            return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM destinations")}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.error("Lecture de l'ancienne base impossible : %r", exc)
        return {}


def get_meta(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        conn = connect(path)
        try:
            return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
        finally:
            conn.close()
    except sqlite3.Error:
        return {}


def count_destinations(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        conn = connect(path)
        try:
            return conn.execute("SELECT COUNT(*) FROM destinations").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def record_attempt_failure(path: Path, error: str) -> None:
    """Note l'échec du dernier scrape dans l'ancienne base (conservée telle quelle)."""
    if not path.exists():
        return
    try:
        conn = connect(path, readonly=False)
        try:
            with conn:
                conn.executemany(
                    "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    [("last_attempt_at", now_iso()), ("last_error", error[:500])],
                )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.error("Impossible d'enregistrer l'échec dans la base : %r", exc)


def build_and_swap(path: Path, destinations: list[dict], scraped_at: str) -> dict:
    """Construit une nouvelle base à côté, puis la substitue atomiquement (os.replace).

    L'historique des changements de l'ancienne base est recopié, puis complété
    par les différences détectées. Retourne un petit bilan.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".new")
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(str(tmp) + suffix).unlink(missing_ok=True)

    old = read_all(path)
    old_changes: list[sqlite3.Row] = []
    if old:
        c = connect(path)
        try:
            old_changes = c.execute(
                "SELECT changed_at, dest_id, dest_name, change_type, field, old_value, new_value "
                "FROM changes ORDER BY id DESC LIMIT ?", (MAX_CHANGES_KEPT,)).fetchall()[::-1]
        finally:
            c.close()

    conn = connect(tmp, readonly=False)
    try:
        conn.executescript(SCHEMA)
        with conn:
            for d in destinations:
                v = _row_values(d)
                v["scraped_at"] = scraped_at
                cols = [
                    "id", "url", "university_name", "country", "language", "diploma", "spots",
                    "gpa_requirement", "extra_charge_cents", "erasmus", "semester", "specializations",
                    "display", "suspended", "updated_at", "images", "full_text", "scraped_at",
                ]
                conn.execute(
                    f"INSERT INTO destinations({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                    [v.get(c) for c in cols],
                )
                specs = " ".join(d.get("specializations") or [])
                conn.execute(
                    "INSERT INTO dest_fts(rowid, name, country, language, specializations, full_text) "
                    "VALUES(?,?,?,?,?,?)",
                    (d["id"], d["university_name"], d.get("country") or "", d.get("language") or "",
                     specs, d.get("full_text") or ""),
                )
            conn.executemany(
                "INSERT INTO changes(changed_at, dest_id, dest_name, change_type, field, old_value, new_value) "
                "VALUES(?,?,?,?,?,?,?)", [tuple(r) for r in old_changes])

            # Détection des changements (pas de bruit au tout premier scrape)
            added = removed = modified = 0
            if old:
                new = {d["id"]: _row_values(d) for d in destinations}
                rows = []
                for uid, nv in new.items():
                    if uid not in old:
                        added += 1
                        rows.append((scraped_at, uid, nv["university_name"], "added", None, None, None))
                        continue
                    ov = old[uid]
                    diff = [f for f in COMPARED_FIELDS if ov.get(f) != nv.get(f)]
                    if diff:
                        modified += 1
                    for f in diff:
                        # le texte long n'est pas stocké, on garde seulement le fait qu'il a changé
                        o, n = (None, None) if f == "full_text" else (_short(ov.get(f)), _short(nv.get(f)))
                        rows.append((scraped_at, uid, nv["university_name"], "modified", f, o, n))
                for uid, ov in old.items():
                    if uid not in new:
                        removed += 1
                        rows.append((scraped_at, uid, ov["university_name"], "removed", None, None, None))
                conn.executemany(
                    "INSERT INTO changes(changed_at, dest_id, dest_name, change_type, field, old_value, new_value) "
                    "VALUES(?,?,?,?,?,?,?)", rows)
            conn.execute(
                "DELETE FROM changes WHERE id NOT IN (SELECT id FROM changes ORDER BY id DESC LIMIT ?)",
                (MAX_CHANGES_KEPT,))
            conn.executemany(
                "INSERT INTO meta(key, value) VALUES(?, ?)",
                [("last_success_at", scraped_at), ("last_attempt_at", scraped_at), ("last_error", ""),
                 ("count", str(len(destinations)))])
        conn.execute("PRAGMA integrity_check")
        conn.execute("INSERT INTO dest_fts(dest_fts) VALUES('optimize')")
        conn.commit()
    except Exception:
        conn.close()
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(str(tmp) + suffix).unlink(missing_ok=True)
        raise
    conn.close()
    os.replace(tmp, path)  # substitution atomique
    return {"added": added, "removed": removed, "modified": modified, "total": len(destinations),
            "first_load": not old}


# Requêtes de lecture


def _decode(row: sqlite3.Row | dict, full: bool = False) -> dict:
    d = dict(row)
    d["specializations"] = json.loads(d.get("specializations") or "[]")
    if full:
        d["images"] = json.loads(d.get("images") or "[]")
    if d.get("extra_charge_cents") is not None:
        d["extra_charge_eur"] = d["extra_charge_cents"] / 100
    # champs None retirés pour une sortie compacte
    return {k: v for k, v in d.items() if v is not None and v != ""}


def _filters(country=None, language=None, erasmus=None, semester=None, max_gpa=None,
             diploma=None, specialization=None, max_extra_charge_eur=None) -> tuple[str, list]:
    where, args = [], []
    if country:
        where.append("fold(d.country) = ?"); args.append(fold(country).strip())
    if language:
        where.append("fold(d.language) LIKE ?"); args.append(f"%{fold(language).strip()}%")
    if erasmus:
        where.append("d.erasmus = ?"); args.append(erasmus.strip().upper())
    if semester:
        where.append("fold(d.semester) = ?"); args.append(fold(semester).strip())
    if max_gpa is not None:
        where.append("COALESCE(d.gpa_requirement, 0) <= ?"); args.append(float(max_gpa))
    if diploma:
        where.append("d.diploma = ?"); args.append(diploma.strip().upper())
    if specialization:
        where.append("EXISTS (SELECT 1 FROM json_each(d.specializations) j WHERE fold(j.value) LIKE ?)")
        args.append(f"%{fold(specialization).strip()}%")
    if max_extra_charge_eur is not None:
        where.append("COALESCE(d.extra_charge_cents, 0) <= ?"); args.append(round(float(max_extra_charge_eur) * 100))
    return (" AND ".join(where) if where else "1=1"), args


def list_destinations(path: Path, limit: int, offset: int, **filters) -> dict:
    where, args = _filters(**filters)
    conn = connect(path)
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM destinations d WHERE {where}", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT {SUMMARY_COLUMNS} FROM destinations d WHERE {where} "
            "ORDER BY fold(d.country), fold(d.university_name) LIMIT ? OFFSET ?",
            [*args, limit, offset]).fetchall()
    finally:
        conn.close()
    return {"total": total, "offset": offset, "count": len(rows), "results": [_decode(r) for r in rows]}


def get_destination(path: Path, uid: int) -> dict | None:
    conn = connect(path)
    try:
        row = conn.execute("SELECT * FROM destinations WHERE id = ?", (uid,)).fetchone()
    finally:
        conn.close()
    return _decode(row, full=True) if row else None


def get_destinations(path: Path, ids: list[int]) -> list[dict]:
    conn = connect(path)
    try:
        marks = ",".join("?" * len(ids))
        rows = conn.execute(f"SELECT * FROM destinations WHERE id IN ({marks})", ids).fetchall()
    finally:
        conn.close()
    by_id = {r["id"]: _decode(r, full=True) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


_WORD = re.compile(r"\w+", re.UNICODE)


def _stem(tok: str) -> str:
    """Singularisation très simple pour que 'courses' trouve 'course'."""
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def tokenize_query(query: str) -> list[str]:
    """Mots de la requête, sans accents ni syntaxe FTS (injection impossible)."""
    toks = [_stem(t) for t in _WORD.findall(fold(query)) if t.strip("_")]
    return toks[:12]


def search_destinations(path: Path, query: str, limit: int, **filters) -> dict:
    toks = tokenize_query(query)
    if not toks:
        return {"query": query, "mode": "none", "count": 0, "results": [],
                "note": "Requête vide après nettoyage."}
    where, fargs = _filters(**filters)
    conn = connect(path)
    try:
        def run_fts(expr: str) -> list[sqlite3.Row]:
            return conn.execute(
                f"SELECT {', '.join('d.' + c.strip() for c in SUMMARY_COLUMNS.split(','))}, "
                "bm25(dest_fts, 10.0, 4.0, 2.0, 3.0, 1.0) AS score, "
                "snippet(dest_fts, 4, '[', ']', ' ... ', 24) AS snippet "
                "FROM dest_fts JOIN destinations d ON d.id = dest_fts.rowid "
                f"WHERE dest_fts MATCH ? AND {where} ORDER BY score LIMIT ?",
                [expr, *fargs, limit]).fetchall()

        # chaque terme est entre guillemets (chaîne littérale) avec préfixe '*'
        terms = [f'"{t}"*' for t in toks]
        mode, rows = "fts_and", []
        try:
            rows = run_fts(" ".join(terms))
            if not rows and len(terms) > 1:
                mode, rows = "fts_or", run_fts(" OR ".join(terms))
        except sqlite3.OperationalError as exc:
            log.warning("FTS indisponible pour %r : %s", query, exc)
        if not rows:
            mode = "like"
            conds = " AND ".join(
                "fold(d.university_name || ' ' || COALESCE(d.country,'') || ' ' || d.specializations || ' ' || d.full_text) LIKE ?"
                for _ in toks)
            rows = conn.execute(
                f"SELECT {', '.join('d.' + c.strip() for c in SUMMARY_COLUMNS.split(','))} "
                f"FROM destinations d WHERE {conds} AND {where} ORDER BY fold(d.university_name) LIMIT ?",
                [*[f"%{t}%" for t in toks], *fargs, limit]).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = _decode(r)
        if "score" in d:
            d["score"] = round(-d["score"], 3)  # plus grand = plus pertinent
        out.append(d)
    return {"query": query, "mode": mode, "count": len(out), "results": out}


def list_filters(path: Path) -> dict:
    conn = connect(path)
    try:
        def counted(col: str):
            return [{"value": r[0], "count": r[1]} for r in conn.execute(
                f"SELECT {col}, COUNT(*) FROM destinations WHERE {col} IS NOT NULL AND {col} != '' "
                f"GROUP BY {col} ORDER BY COUNT(*) DESC, {col}")]
        specs = [{"value": r[0], "count": r[1]} for r in conn.execute(
            "SELECT j.value, COUNT(*) FROM destinations d, json_each(d.specializations) j "
            "GROUP BY j.value ORDER BY COUNT(*) DESC, j.value")]
        res = {
            "countries": counted("country"), "languages": counted("language"),
            "semesters": counted("semester"), "erasmus": counted("erasmus"),
            "diploma": counted("diploma"), "specializations": specs,
            "max_gpa_values": [r[0] for r in conn.execute(
                "SELECT DISTINCT gpa_requirement FROM destinations WHERE gpa_requirement IS NOT NULL ORDER BY 1")],
        }
    finally:
        conn.close()
    return res


def recent_changes(path: Path, limit: int = 20) -> list[dict]:
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT id, changed_at, dest_id, dest_name, change_type, field, old_value, new_value "
            "FROM changes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    return [{k: v for k, v in dict(r).items() if v is not None} for r in rows]
