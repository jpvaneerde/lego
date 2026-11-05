"""Rebrickable catalog: downloads the public CSV dumps into SQLite and answers
"which sets can I (almost) build with these parts?".

Dumps: https://rebrickable.com/downloads/ (refreshed daily by Rebrickable).
"""
import csv
import gzip
import io
import logging
import math
import os
import sqlite3
import threading
import time

import httpx

from . import config

log = logging.getLogger("catalog")

FILES = ["colors", "themes", "parts", "part_relationships", "sets", "inventories", "inventory_parts"]
# Variant relations treated as the same part: M = mould variant, A = alternate.
EQUIV_RELS = {"M", "A"}

_state = {"status": "missing", "detail": "", "built_at": None}
_lock = threading.Lock()


def status() -> dict:
    return dict(_state)


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{config.CATALOG_DB}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def _rows(path: str):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        yield from csv.DictReader(fh)


def _download(name: str, dest_dir: str) -> str:
    url = f"{config.REBRICKABLE_DOWNLOADS}/{name}.csv.gz"
    path = os.path.join(dest_dir, f"{name}.csv.gz")
    with httpx.stream("GET", url, timeout=120, follow_redirects=True) as r:
        r.raise_for_status()
        with open(path, "wb") as fh:
            for chunk in r.iter_bytes():
                fh.write(chunk)
    return path


def _truthy(v) -> int:
    return 1 if str(v).strip().lower() in ("t", "true", "1", "yes") else 0


def build(source_dir: str | None = None) -> None:
    """Build a fresh catalog DB. With source_dir, read *.csv.gz from there
    instead of downloading (used for tests/offline)."""
    os.makedirs(config.DATA_DIR, exist_ok=True)
    tmp_db = config.CATALOG_DB + ".tmp"
    dl_dir = source_dir or os.path.join(config.DATA_DIR, "downloads")
    os.makedirs(dl_dir, exist_ok=True)

    paths = {}
    for name in FILES:
        _state["detail"] = f"downloading {name}"
        paths[name] = os.path.join(dl_dir, f"{name}.csv.gz") if source_dir else _download(name, dl_dir)

    if os.path.exists(tmp_db):
        os.remove(tmp_db)
    con = sqlite3.connect(tmp_db)
    con.executescript("""
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
        CREATE TABLE colors(id INTEGER PRIMARY KEY, name TEXT, rgb TEXT, is_trans INTEGER, num_parts INTEGER);
        CREATE TABLE themes(id INTEGER PRIMARY KEY, name TEXT, parent_id INTEGER);
        CREATE TABLE parts(part_num TEXT PRIMARY KEY, name TEXT, part_cat_id INTEGER, cpart TEXT);
        CREATE TABLE sets(set_num TEXT PRIMARY KEY, name TEXT, year INTEGER, theme_id INTEGER, num_parts INTEGER, img_url TEXT);
        CREATE TABLE inv(id INTEGER PRIMARY KEY, set_num TEXT);
        CREATE TABLE need(inv_id INTEGER, cpart TEXT, color_id INTEGER, qty INTEGER);
        CREATE TABLE totals(inv_id INTEGER PRIMARY KEY, set_num TEXT, total INTEGER);
        CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
    """)

    _state["detail"] = "importing colours/themes/parts"
    con.executemany("INSERT INTO colors VALUES(?,?,?,?,?)", (
        (int(r["id"]), r["name"], r.get("rgb", ""), _truthy(r.get("is_trans", "f")),
         int(r["num_parts"]) if (r.get("num_parts") or "").isdigit() else None)
        for r in _rows(paths["colors"])))
    con.executemany("INSERT INTO themes VALUES(?,?,?)", (
        (int(r["id"]), r["name"], int(r["parent_id"]) if r.get("parent_id") else None)
        for r in _rows(paths["themes"])))

    # Canonical part: follow mould/alternate relations to one representative.
    parent = {}
    for r in _rows(paths["part_relationships"]):
        if r.get("rel_type") in EQUIV_RELS:
            parent.setdefault(r["child_part_num"], r["parent_part_num"])

    def canon(p: str) -> str:
        seen = {p}
        while p in parent and parent[p] not in seen:
            p = parent[p]
            seen.add(p)
        return p

    con.executemany("INSERT OR IGNORE INTO parts VALUES(?,?,?,?)", (
        (r["part_num"], r["name"], int(r["part_cat_id"]) if r.get("part_cat_id") else None, canon(r["part_num"]))
        for r in _rows(paths["parts"])))

    con.executemany("INSERT OR IGNORE INTO sets VALUES(?,?,?,?,?,?)", (
        (r["set_num"], r["name"], int(r["year"] or 0), int(r["theme_id"] or 0),
         int(r["num_parts"] or 0), r.get("img_url", ""))
        for r in _rows(paths["sets"])))

    # Latest inventory version per real set (skip minifig inventories).
    latest = {}
    for r in _rows(paths["inventories"]):
        sn = r["set_num"]
        if sn.startswith("fig-"):
            continue
        v = int(r.get("version") or 1)
        if sn not in latest or v > latest[sn][0]:
            latest[sn] = (v, int(r["id"]))
    keep = {iid: sn for sn, (_, iid) in latest.items()}
    con.executemany("INSERT INTO inv VALUES(?,?)", ((i, s) for i, s in keep.items()))

    _state["detail"] = "importing set inventories"
    agg: dict[tuple, int] = {}
    for r in _rows(paths["inventory_parts"]):
        iid = int(r["inventory_id"])
        if iid not in keep or _truthy(r.get("is_spare", "f")):
            continue
        key = (iid, canon(r["part_num"]), int(r["color_id"]))
        agg[key] = agg.get(key, 0) + int(r["quantity"])
    con.executemany("INSERT INTO need VALUES(?,?,?,?)", ((k[0], k[1], k[2], q) for k, q in agg.items()))
    del agg

    _state["detail"] = "indexing"
    con.executescript("""
        CREATE INDEX need_part_color ON need(cpart, color_id);
        CREATE INDEX need_inv ON need(inv_id);
        INSERT INTO totals SELECT n.inv_id, i.set_num, SUM(n.qty) FROM need n JOIN inv i ON i.id = n.inv_id GROUP BY n.inv_id;
        CREATE INDEX parts_cpart ON parts(cpart);
    """)
    con.execute("INSERT INTO meta VALUES('built_at', ?)", (str(time.time()),))
    con.commit()
    con.close()
    os.replace(tmp_db, config.CATALOG_DB)


def _age_days() -> float | None:
    if not os.path.exists(config.CATALOG_DB):
        return None
    return (time.time() - os.path.getmtime(config.CATALOG_DB)) / 86400


def ensure(force: bool = False) -> None:
    """Build or refresh the catalog if missing/stale. Safe to call from a thread."""
    if not _lock.acquire(blocking=False):
        return
    try:
        age = _age_days()
        if age is not None:
            _state.update(status="ready", built_at=os.path.getmtime(config.CATALOG_DB))
        if not force and age is not None and age < config.CATALOG_MAX_AGE_DAYS:
            return
        _state.update(status="building" if age is None else "ready", detail="starting")
        t0 = time.time()
        build()
        _state.update(status="ready", detail=f"built in {time.time() - t0:.0f}s",
                      built_at=os.path.getmtime(config.CATALOG_DB))
        log.info("catalog built in %.0fs", time.time() - t0)
    except Exception as e:  # keep old DB if refresh fails
        log.exception("catalog build failed")
        _state.update(status="ready" if _age_days() is not None else "error", detail=f"build failed: {e}")
    finally:
        _lock.release()


def refresher_loop() -> None:
    while True:
        ensure()
        time.sleep(6 * 3600)


# ---------------------------------------------------------------- queries ---

def colors(con) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT id, name, rgb, is_trans, num_parts FROM colors WHERE id >= 0 ORDER BY name")]


def part_info(con, part_nums: list[str]) -> dict[str, dict]:
    if not part_nums:
        return {}
    q = ",".join("?" * len(part_nums))
    return {r["part_num"]: dict(r) for r in con.execute(
        f"SELECT part_num, name, cpart FROM parts WHERE part_num IN ({q})", part_nums)}


def match_sets(con, have: list[dict], mode: str = "color", limit: int = 30,
               min_parts: int = 8, max_parts: int | None = None) -> list[dict]:
    """have: [{part_num, color_id, qty}]. Returns sets ranked by how much of
    them the given parts cover. mode 'color' needs matching colours, 'shape'
    ignores colour."""
    info = part_info(con, list({h["part_num"] for h in have}))
    agg: dict[tuple, int] = {}
    for h in have:
        cp = info.get(h["part_num"], {}).get("cpart", h["part_num"])
        key = (cp, int(h.get("color_id", -1)) if mode == "color" else -1)
        agg[key] = agg.get(key, 0) + int(h.get("qty", 1))
    if not agg:
        return []
    have_total = sum(agg.values())

    con.execute("CREATE TEMP TABLE IF NOT EXISTS have(cpart TEXT, color_id INTEGER, qty INTEGER)")
    con.execute("DELETE FROM have")
    con.executemany("INSERT INTO have VALUES(?,?,?)", ((k[0], k[1], q) for k, q in agg.items()))

    if mode == "color":
        sql = """
          SELECT n.inv_id, SUM(MIN(n.qty, h.qty)) AS matched
          FROM need n JOIN have h ON h.cpart = n.cpart AND h.color_id = n.color_id
          GROUP BY n.inv_id"""
    else:
        sql = """
          SELECT n.inv_id, SUM(MIN(n.qty, h.qty)) AS matched FROM (
            SELECT inv_id, cpart, SUM(qty) AS qty FROM need
            WHERE cpart IN (SELECT cpart FROM have) GROUP BY inv_id, cpart) n
          JOIN have h ON h.cpart = n.cpart
          GROUP BY n.inv_id"""

    max_parts = max_parts or max(have_total * 4, 40)
    rows = con.execute(f"""
        SELECT m.inv_id, m.matched, t.total, s.set_num, s.name, s.year, s.img_url, s.num_parts,
               th.name AS theme
        FROM ({sql}) m
        JOIN totals t ON t.inv_id = m.inv_id
        JOIN sets s ON s.set_num = t.set_num
        LEFT JOIN themes th ON th.id = s.theme_id
        WHERE t.total >= ? AND t.total <= ?
    """, (min_parts, max_parts)).fetchall()

    scored = []
    for r in rows:
        cov = r["matched"] / r["total"]
        # Rank by coverage, nudged towards bigger builds that use more of your pile.
        use = r["matched"] / have_total
        scored.append((cov + 0.15 * use + 0.02 * math.log1p(r["total"]), r, cov, use))
    scored.sort(key=lambda x: x[0], reverse=True)

    out = []
    for _, r, cov, use in scored[:limit]:
        out.append({
            "set_num": r["set_num"], "name": r["name"], "year": r["year"], "theme": r["theme"],
            "img_url": r["img_url"], "parts_total": r["total"], "parts_matched": r["matched"],
            "coverage": round(cov, 4), "uses_of_pile": round(use, 4),
            "rebrickable_url": f"https://rebrickable.com/sets/{r['set_num']}/",
            "instructions_url": f"https://www.lego.com/service/buildinginstructions/{r['set_num'].split('-')[0]}",
            "_inv_id": r["inv_id"],
        })
    return out


def missing_parts(con, inv_id: int, mode: str = "color") -> list[dict]:
    """Parts of a set not covered by the current temp 'have' table."""
    if mode == "color":
        rows = con.execute("""
            SELECT n.cpart, n.color_id, n.qty - COALESCE(h.qty, 0) AS missing,
                   p.name AS part_name, c.name AS color_name, c.rgb
            FROM need n
            LEFT JOIN have h ON h.cpart = n.cpart AND h.color_id = n.color_id
            LEFT JOIN parts p ON p.part_num = n.cpart
            LEFT JOIN colors c ON c.id = n.color_id
            WHERE n.inv_id = ? AND n.qty > COALESCE(h.qty, 0)
            ORDER BY missing DESC""", (inv_id,)).fetchall()
    else:
        rows = con.execute("""
            SELECT n.cpart, -1 AS color_id, n.qty - COALESCE(h.qty, 0) AS missing,
                   p.name AS part_name, NULL AS color_name, NULL AS rgb
            FROM (SELECT cpart, SUM(qty) qty FROM need WHERE inv_id = ? GROUP BY cpart) n
            LEFT JOIN have h ON h.cpart = n.cpart
            LEFT JOIN parts p ON p.part_num = n.cpart
            WHERE n.qty > COALESCE(h.qty, 0)
            ORDER BY missing DESC""", (inv_id,)).fetchall()
    return [{"part_num": r["cpart"], "color_id": r["color_id"], "qty": r["missing"],
             "part_name": r["part_name"], "color_name": r["color_name"], "rgb": r["rgb"]} for r in rows]
