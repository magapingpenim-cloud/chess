"""Импорт задач из открытой базы Lichess (лицензия CC0).

Вручную (файл скачан и распакован):
    python import_lichess.py lichess_db_puzzle.csv --per-band 5000
Сразу из интернета (нужен пакет zstandard; так бот делает сам на сервере при первом запуске):
    python import_lichess.py --url

Берёт популярные задачи равномерно по диапазонам рейтинга, чтобы были задачи для всех уровней.
"""
import argparse
import csv
import io
import logging
import os
import sqlite3
import sys
import urllib.request

from db import SCHEMA

LICHESS_URL = "https://database.lichess.org/lichess_db_puzzle.csv.zst"
BANDS = [(0, 800), (800, 1100), (1100, 1400), (1400, 1700), (1700, 2000), (2000, 2400), (2400, 4000)]


def import_rows(conn: sqlite3.Connection, rows, per_band=5000, min_popularity=80, min_plays=500) -> dict:
    """rows — итератор строк CSV (первая строка — заголовок)."""
    counts = {b: 0 for b in BANDS}
    header = next(rows)
    col = {name: i for i, name in enumerate(header)}
    total = 0
    for row in rows:
        try:
            rating = int(row[col["Rating"]])
            if int(row[col["Popularity"]]) < min_popularity or int(row[col["NbPlays"]]) < min_plays:
                continue
        except (ValueError, IndexError):
            continue
        band = next((b for b in BANDS if b[0] <= rating < b[1]), None)
        if band is None or counts[band] >= per_band:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO puzzles(ext_id, fen, moves, rating, themes, lichess) VALUES(?,?,?,?,?,1)",
            (row[col["PuzzleId"]], row[col["FEN"]], row[col["Moves"]], rating, row[col["Themes"]]),
        )
        counts[band] += 1
        total += 1
        if total % 5000 == 0:
            conn.commit()
            logging.info("Импорт задач Lichess: %s…", total)
        if all(c >= per_band for c in counts.values()):
            break
    conn.commit()
    return counts


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def import_file(csv_path: str, db_path: str, per_band=5000) -> dict:
    conn = _connect(db_path)
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            return import_rows(conn, csv.reader(f), per_band)
    finally:
        conn.close()


def import_from_url(db_path: str, per_band=5000, url=LICHESS_URL) -> dict:
    """Скачивает .csv.zst потоком и распаковывает на лету — на диск ничего лишнего не пишет."""
    import zstandard  # только для этого режима

    conn = _connect(db_path)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ChessUp-bot/1.0"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            reader = zstandard.ZstdDecompressor().stream_reader(resp)
            text = io.TextIOWrapper(reader, encoding="utf-8", newline="")
            return import_rows(conn, csv.reader(text), per_band)
    finally:
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path", nargs="?")
    ap.add_argument("--url", action="store_true", help="скачать базу Lichess напрямую")
    ap.add_argument("--db", default=os.getenv("DB_PATH", "chessbot.db"))
    ap.add_argument("--per-band", type=int, default=5000, help="задач на каждый диапазон рейтинга")
    a = ap.parse_args()
    if a.url:
        counts = import_from_url(a.db, a.per_band)
    elif a.csv_path:
        counts = import_file(a.csv_path, a.db, a.per_band)
    else:
        ap.error("укажите путь к CSV или --url")
    for b, c in counts.items():
        print(f"{b[0]:>4}-{b[1]:<4}: {c}")
    print(f"Итого: {sum(counts.values())}")


if __name__ == "__main__":
    sys.exit(main())
