# db/db.py
import sqlite3
import hashlib
from pathlib import Path
from datetime import datetime
from utils.config import DB_PATH as _DB_PATH, DEFECT_LABELS

DB_PATH = Path(_DB_PATH).resolve()

def get_db_path() -> str:
    return str(DB_PATH)

def _connect():
    return sqlite3.connect(str(DB_PATH))

def _file_sha256(fpath: str) -> str:
    h = hashlib.sha256()
    with open(fpath, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def ensure_schema():
    """
    results (
        id, file_name, image_path, image_hash,
        defect_type, severity, location, score, detail, action, ts
    )
    """
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name   TEXT,
            image_path  TEXT NOT NULL,
            image_hash  TEXT,
            defect_type TEXT,
            severity    TEXT,
            location    TEXT,
            score       REAL,
            detail      TEXT,
            action      TEXT,
            ts          TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    # 인덱스
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_results_image_path ON results(image_path)")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_results_image_hash ON results(image_hash)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_results_type_sev_ts ON results(defect_type, severity, ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_results_location ON results(location)")
    conn.commit()
    conn.close()

def insert_result(
    image_path: str,
    defect_type: str,
    severity: str,
    location: str,
    score: float,
    detail: str = "",
    action: str = "Hold",
    ts: str | None = None,
) -> bool:
    ensure_schema()
    ihash = _file_sha256(image_path)
    file_name = Path(image_path).name
    ts_val = ts or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 라벨 검증
    if defect_type not in DEFECT_LABELS:
        defect_type = DEFECT_LABELS[0]

    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO results
        (file_name, image_path, image_hash, defect_type, severity, location, score, detail, action, ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        file_name, image_path, ihash,
        defect_type, severity, location, float(score), detail, action, ts_val
    ))
    conn.commit()
    ok = (cur.rowcount == 1)
    conn.close()
    return ok

def upsert_result(
    image_path: str,
    defect_type: str,
    severity: str,
    location: str,
    score: float,
    detail: str = "",
    action: str = "Hold",
    ts: str | None = None,
) -> int:
    ensure_schema()
    ihash = _file_sha256(image_path)
    file_name = Path(image_path).name
    ts_val = ts or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if defect_type not in DEFECT_LABELS:
        defect_type = DEFECT_LABELS[0]

    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT id FROM results WHERE image_hash = ?", (ihash,))
    row = cur.fetchone()
    if row:
        rid = row[0]
        cur.execute("""
            UPDATE results
            SET file_name=?, image_path=?, defect_type=?, severity=?, location=?, score=?, detail=?, action=?, ts=?
            WHERE id=?
        """, (
            file_name, image_path, defect_type, severity, location,
            float(score), detail, action, ts_val, rid
        ))
        conn.commit()
        conn.close()
        return rid
    else:
        cur.execute("""
            INSERT INTO results
            (file_name, image_path, image_hash, defect_type, severity, location, score, detail, action, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            file_name, image_path, ihash, defect_type, severity, location,
            float(score), detail, action, ts_val
        ))
        conn.commit()
        rid = cur.lastrowid
        conn.close()
        return rid

def fetch_results(limit: int = 200):
    ensure_schema()
    conn = _connect()
    rows = conn.execute(
        "SELECT id, image_path, file_name, defect_type, severity, location, score, detail, action, ts "
        "FROM results ORDER BY datetime(ts) DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return rows

def search_results(defect_type=None, severity=None, action=None,
                   location=None, keyword=None, date_from=None, date_to=None, limit: int = 500):
    ensure_schema()
    conn = _connect()
    base = ("SELECT id, image_path, file_name, defect_type, severity, location, score, detail, action, ts "
            "FROM results WHERE 1=1")
    args = []
    if defect_type:
        base += " AND defect_type = ?"; args.append(defect_type)
    if severity:
        base += " AND severity = ?"; args.append(severity)
    if action:
        base += " AND action = ?"; args.append(action)
    if location:
        base += " AND location LIKE ?"; args.append(f"%{location}%")
    if keyword:
        w = f"%{keyword}%"
        base += " AND (file_name LIKE ? OR detail LIKE ? OR location LIKE ?)"
        args.extend([w, w, w])
    if date_from:
        base += " AND date(ts) >= date(?)"; args.append(date_from)
    if date_to:
        base += " AND date(ts) <= date(?)"; args.append(date_to)
    base += " ORDER BY datetime(ts) DESC, id DESC LIMIT ?"; args.append(limit)
    rows = conn.execute(base, args).fetchall()
    conn.close()
    return rows

def delete_results(ids):
    if not ids:
        return 0
    ensure_schema()
    conn = _connect()
    q = "DELETE FROM results WHERE id IN ({})".format(",".join(["?"] * len(ids)))
    cur = conn.cursor()
    cur.execute(q, ids)
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n