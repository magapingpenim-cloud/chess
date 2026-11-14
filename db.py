"""SQLite-хранилище шахматного бота."""
import time
from datetime import date

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    name TEXT,
    created_at INTEGER,
    rating INTEGER DEFAULT 800,
    solved INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    streak INTEGER DEFAULT 0,
    last_solved_day TEXT,
    vip_until INTEGER DEFAULT 0,
    referrer_id INTEGER,
    ref_rewarded INTEGER DEFAULT 0,
    daily_sub INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS puzzles(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ext_id TEXT UNIQUE,
    fen TEXT,
    moves TEXT,
    rating INTEGER,
    themes TEXT,
    lichess INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_puzzles_rating ON puzzles(rating);
CREATE TABLE IF NOT EXISTS attempts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    puzzle_id INTEGER,
    solved INTEGER,
    day TEXT,
    daily INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_attempts_user ON attempts(user_id, day);
CREATE TABLE IF NOT EXISTS payments(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, plan TEXT, stars INTEGER, days INTEGER,
    charge_id TEXT UNIQUE, created_at INTEGER, refunded INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


def today() -> str:
    return date.today().isoformat()


class DB:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self):
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")  # параллельный импорт задач не блокирует игроков
        await self.conn.execute("PRAGMA busy_timeout=10000")
        await self.conn.executescript(SCHEMA)
        for col in ("sub_charge_id TEXT", "source TEXT",  # миграции для старых баз
                    "online_rating INTEGER DEFAULT 1200", "online_games INTEGER DEFAULT 0"):
            try:
                await self.conn.execute(f"ALTER TABLE users ADD COLUMN {col}")
            except aiosqlite.OperationalError:
                pass
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def one(self, sql, a=()):
        return await (await self.conn.execute(sql, a)).fetchone()

    async def all(self, sql, a=()):
        return await (await self.conn.execute(sql, a)).fetchall()

    async def exec(self, sql, a=()):
        cur = await self.conn.execute(sql, a)
        await self.conn.commit()
        return cur

    # --- kv ---
    async def kv_get(self, k):
        r = await self.one("SELECT v FROM kv WHERE k=?", (k,))
        return r["v"] if r else None

    async def kv_set(self, k, v):
        await self.exec("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))

    # --- users ---
    async def get_user(self, uid):
        return await self.one("SELECT * FROM users WHERE id=?", (uid,))

    async def create_user(self, uid, name, referrer_id=None, rating=800, source=None):
        if referrer_id == uid:
            referrer_id = None
        await self.exec(
            "INSERT OR IGNORE INTO users(id, name, created_at, referrer_id, rating, source) VALUES(?,?,?,?,?,?)",
            (uid, name, int(time.time()), referrer_id, rating, source),
        )
        return await self.get_user(uid)

    async def sources_report(self, days: int | None = None):
        """Воронка по источникам: пришли → решали → вернулись на 2-й день → заплатили → звёзды."""
        where, args = "", ()
        if days:
            where, args = "WHERE u.created_at > ?", (int(time.time()) - days * 86400,)
        return await self.all(f"""
            SELECT COALESCE(u.source, 'organic') AS src,
                   COUNT(*) AS users,
                   SUM(EXISTS(SELECT 1 FROM attempts a WHERE a.user_id = u.id)) AS played,
                   SUM((SELECT COUNT(DISTINCT day) FROM attempts a WHERE a.user_id = u.id) >= 2) AS returned,
                   SUM(EXISTS(SELECT 1 FROM payments p WHERE p.user_id = u.id AND p.refunded = 0)) AS payers,
                   COALESCE(SUM((SELECT SUM(stars) FROM payments p WHERE p.user_id = u.id AND p.refunded = 0)), 0) AS stars
            FROM users u {where}
            GROUP BY src ORDER BY users DESC
        """, args)

    async def set_field(self, uid, field, value):
        assert field in {"rating", "daily_sub", "ref_rewarded", "name", "sub_charge_id"}
        await self.exec(f"UPDATE users SET {field}=? WHERE id=?", (value, uid))

    async def record_result(self, uid, puzzle_id, solved, new_rating, daily=False):
        d = today()
        await self.exec(
            "INSERT INTO attempts(user_id, puzzle_id, solved, day, daily) VALUES(?,?,?,?,?)",
            (uid, puzzle_id, int(solved), d, int(daily)),
        )
        u = await self.get_user(uid)
        if solved:
            yesterday = date.fromordinal(date.today().toordinal() - 1).isoformat()
            if u["last_solved_day"] == d:
                streak = u["streak"]
            elif u["last_solved_day"] == yesterday:
                streak = u["streak"] + 1
            else:
                streak = 1
            await self.exec(
                "UPDATE users SET rating=?, solved=solved+1, streak=?, last_solved_day=? WHERE id=?",
                (new_rating, streak, d, uid),
            )
        else:
            await self.exec("UPDATE users SET rating=?, failed=failed+1 WHERE id=?", (new_rating, uid))

    async def attempts_today(self, uid) -> int:
        r = await self.one("SELECT COUNT(*) c FROM attempts WHERE user_id=? AND day=? AND daily=0", (uid, today()))
        return r["c"]

    async def add_vip_days(self, uid, days) -> int:
        u = await self.get_user(uid)
        until = max(int(time.time()), u["vip_until"] if u else 0) + days * 86400
        await self.exec("UPDATE users SET vip_until=? WHERE id=?", (until, uid))
        return until

    async def update_online(self, uid, new_rating: int):
        await self.exec("UPDATE users SET online_rating=?, online_games=online_games+1 WHERE id=?", (new_rating, uid))

    async def extend_vip_until(self, uid, ts) -> int:
        """Для подписки: VIP до даты окончания оплаченного периода (не укорачивает)."""
        u = await self.get_user(uid)
        until = max(int(ts), u["vip_until"] if u else 0)
        await self.exec("UPDATE users SET vip_until=? WHERE id=?", (until, uid))
        return until

    async def remove_vip_days(self, uid, days):
        await self.exec("UPDATE users SET vip_until=MAX(0, vip_until-?) WHERE id=?", (days * 86400, uid))

    async def leaderboard(self, limit=10):
        return await self.all(
            "SELECT id, name, rating, solved FROM users WHERE solved>=10 ORDER BY rating DESC LIMIT ?", (limit,)
        )

    async def rank_of(self, uid) -> int:
        u = await self.get_user(uid)
        r = await self.one("SELECT COUNT(*)+1 c FROM users WHERE solved>=10 AND rating>?", (u["rating"],))
        return r["c"]

    async def daily_subscribers(self):
        return [r["id"] for r in await self.all("SELECT id FROM users WHERE daily_sub=1")]

    # --- puzzles ---
    async def puzzle_count(self) -> int:
        return (await self.one("SELECT COUNT(*) c FROM puzzles"))["c"]

    async def add_puzzle(self, ext_id, fen, moves, rating, themes="", lichess=1):
        await self.exec(
            "INSERT OR IGNORE INTO puzzles(ext_id, fen, moves, rating, themes, lichess) VALUES(?,?,?,?,?,?)",
            (ext_id, fen, moves, rating, themes, lichess),
        )

    async def get_puzzle(self, pid):
        return await self.one("SELECT * FROM puzzles WHERE id=?", (pid,))

    async def pick_puzzle(self, uid, rating):
        """Случайная нерешённая задача около рейтинга игрока, с расширением окна."""
        for spread in (100, 250, 500, 5000):
            r = await self.one(
                "SELECT * FROM puzzles WHERE rating BETWEEN ? AND ? "
                "AND id NOT IN (SELECT puzzle_id FROM attempts WHERE user_id=?) ORDER BY RANDOM() LIMIT 1",
                (rating - spread, rating + spread, uid),
            )
            if r:
                return r
        return await self.one("SELECT * FROM puzzles ORDER BY RANDOM() LIMIT 1")

    # --- payments ---
    async def add_payment(self, uid, plan, stars, days, charge_id) -> bool:
        cur = await self.exec(
            "INSERT OR IGNORE INTO payments(user_id, plan, stars, days, charge_id, created_at) VALUES(?,?,?,?,?,?)",
            (uid, plan, stars, days, charge_id, int(time.time())),
        )
        return cur.rowcount > 0

    async def paid_count(self, uid):
        return (await self.one("SELECT COUNT(*) c FROM payments WHERE user_id=?", (uid,)))["c"]

    async def get_payment(self, charge_id):
        return await self.one("SELECT * FROM payments WHERE charge_id=?", (charge_id,))

    async def mark_refunded(self, charge_id):
        await self.exec("UPDATE payments SET refunded=1 WHERE charge_id=?", (charge_id,))

    async def stats(self):
        now = int(time.time())
        q = self.one
        return {
            "users": (await q("SELECT COUNT(*) c FROM users"))["c"],
            "active_today": (await q("SELECT COUNT(DISTINCT user_id) c FROM attempts WHERE day=?", (today(),)))["c"],
            "vip": (await q("SELECT COUNT(*) c FROM users WHERE vip_until>?", (now,)))["c"],
            "rev_30d": (await q("SELECT COALESCE(SUM(stars),0) s FROM payments WHERE refunded=0 AND created_at>?",
                                (now - 30 * 86400,)))["s"],
            "rev_all": (await q("SELECT COALESCE(SUM(stars),0) s FROM payments WHERE refunded=0"))["s"],
            "puzzles": await self.puzzle_count(),
        }
