"""Бизнес-логика ChessUp — общая для бота и Mini App API."""
import os
import re
import time

from aiogram import Bot
from aiogram.types import LabeledPrice
from dotenv import load_dotenv

import puzzles as P
from db import DB, today

load_dotenv()

BOT_NAME = "ChessUp"
# На Amvera постоянное хранилище смонтировано в /data — база там переживает пересборки
DB_PATH = os.getenv("DB_PATH") or ("/data/chessbot.db" if os.path.isdir("/data") else "chessbot.db")
FREE_DAILY = int(os.getenv("FREE_DAILY", "5"))
SUB_PERIOD = 30 * 86400  # Telegram Stars поддерживает подписки только на 30 дней
REF_BONUS_DAYS = 7

# ключ: (название, звёзды, дни, подписка с автопродлением?)
PLANS = {
    "sub30": (f"{BOT_NAME} VIP · месяц", 149, 30, True),
    "vip365": (f"{BOT_NAME} VIP · год", 990, 365, False),
}
LEVELS = {600: "Только начинаю", 1000: "Знаю правила", 1400: "Играю регулярно", 1800: "Сильный игрок"}
THEMES = {
    "mateIn1": "Мат в 1 ход", "mateIn2": "Мат в 2 хода", "mateIn3": "Мат в 3 хода",
    "fork": "Вилка", "pin": "Связка", "skewer": "Линейный удар", "discoveredAttack": "Вскрытое нападение",
    "sacrifice": "Жертва", "hangingPiece": "Незащищённая фигура", "endgame": "Эндшпиль",
    "backRankMate": "Мат по последней горизонтали", "promotion": "Превращение пешки",
}

db = DB(DB_PATH)
sessions: dict[int, P.Session] = {}
bot: Bot | None = None          # выставляется при старте
bot_username = ""


class ApiError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(code)
        self.code, self.message = code, message


def is_vip(u) -> bool:
    return bool(u and u["vip_until"] > time.time())


def themes_ru(themes: str) -> list[str]:
    return [THEMES[t] for t in (themes or "").split() if t in THEMES]


SOURCE_RE = re.compile(r"^[a-z0-9_]{1,32}$")


def parse_start_param(start_param: str | None) -> tuple[int | None, str | None]:
    """ref_123 → (123, 'ref'); src_adsgram → (None, 'adsgram'); иначе (None, None)."""
    p = (start_param or "").strip().lower()
    if p.startswith("ref_") and p[4:].isdigit():
        return int(p[4:]), "ref"
    if p.startswith("src_") and SOURCE_RE.match(p[4:]):
        return None, p[4:]
    return None, None


async def ensure_user(uid: int, name: str | None, start_param: str | None = None):
    u = await db.get_user(uid)
    if u:
        return u  # источник записывается только при первом входе
    ref, source = parse_start_param(start_param)
    if ref is not None and (ref == uid or not await db.get_user(ref)):
        ref = None
    u = await db.create_user(uid, name, ref, source=source)
    await db.exec("UPDATE users SET rating=0 WHERE id=?", (uid,))  # 0 = уровень ещё не выбран
    return await db.get_user(uid)


async def profile(uid: int) -> dict:
    u = await db.get_user(uid)
    total = u["solved"] + u["failed"]
    vip = is_vip(u)
    daily_row = await daily_puzzle_row(create=False)
    daily_done = bool(daily_row and await db.one(
        "SELECT 1 FROM attempts WHERE user_id=? AND puzzle_id=? AND daily=1 AND day=?",
        (uid, daily_row["id"], today()),
    ))
    return {
        "id": uid,
        "name": u["name"],
        "needs_level": u["rating"] == 0,
        "rating": u["rating"],
        "solved": u["solved"],
        "failed": u["failed"],
        "accuracy": round(100 * u["solved"] / total) if total else 0,
        "streak": u["streak"],
        "rank": await db.rank_of(uid) if u["solved"] >= 10 else None,
        "vip": vip,
        "vip_until": u["vip_until"] if vip else None,
        "subscription": bool(vip and u["sub_charge_id"]),
        "free_left": None if vip else max(0, FREE_DAILY - await db.attempts_today(uid)),
        "free_daily": FREE_DAILY,
        "daily_done": daily_done,
        "daily_sub": bool(u["daily_sub"]),
        "ref_link": f"https://t.me/{bot_username}?startapp=ref_{uid}",
        "ref_bonus_days": REF_BONUS_DAYS,
        "has_active": uid in sessions,
    }


async def set_level(uid: int, rating: int):
    if rating not in LEVELS:
        raise ApiError("bad_level")
    await db.set_field(uid, "rating", rating)


async def daily_puzzle_row(create=True):
    key = f"daily:{today()}"
    pid = await db.kv_get(key)
    if pid:
        return await db.get_puzzle(int(pid))
    if not create:
        return None
    row = await db.pick_puzzle(0, 1200)
    if row:
        await db.kv_set(key, row["id"])
    return row


async def current(uid: int) -> dict | None:
    """Незаконченная задача — чтобы продолжить после закрытия приложения."""
    s = sessions.get(uid)
    if not s:
        return None
    row = await db.get_puzzle(s.puzzle_id)
    return _puzzle_payload(row, s, s.title)


def _puzzle_payload(row, s: P.Session, title: str) -> dict:
    s.title = title
    return {
        "id": row["id"],
        "title": title,
        "rating": row["rating"],
        "orientation": "w" if s.player_color else "b",
        "daily": s.daily,
        **P.board_state(s),
    }


async def _abandon(uid: int):
    old = sessions.pop(uid, None)
    if old and not old.daily:  # брошенная задача засчитывается как нерешённая
        u = await db.get_user(uid)
        await db.record_result(uid, old.puzzle_id, False, P.elo(u["rating"], old.rating, False))


async def new_puzzle(uid: int, mode: str = "normal", theme: str | None = None) -> dict:
    u = await db.get_user(uid)
    if u["rating"] == 0:
        raise ApiError("needs_level")
    if mode == "daily":
        row = await daily_puzzle_row()
        if not row:
            raise ApiError("no_puzzles", "Задача дня пока не готова")
        if await db.one("SELECT 1 FROM attempts WHERE user_id=? AND puzzle_id=? AND daily=1 AND day=?",
                        (uid, row["id"], today())):
            raise ApiError("daily_done", "Вы уже решили задачу дня. Завтра будет новая!")
        await _abandon(uid)
        s = P.start_session(row["id"], row["fen"], row["moves"], row["rating"], bool(row["lichess"]), daily=True)
        sessions[uid] = s
        return _puzzle_payload(row, s, "Задача дня")

    await _abandon(uid)
    u = await db.get_user(uid)
    if mode == "theme":
        if not is_vip(u):
            raise ApiError("vip_required", "Тренировка по темам — в VIP")
        if theme not in THEMES:
            raise ApiError("bad_theme")
    if not is_vip(u) and await db.attempts_today(uid) >= FREE_DAILY:
        raise ApiError("limit", f"Бесплатные задачи на сегодня закончились ({FREE_DAILY}/{FREE_DAILY})")

    row = None
    if mode == "theme":
        for spread in (200, 500, 5000):
            row = await db.one(
                "SELECT * FROM puzzles WHERE (' ' || themes || ' ') LIKE ? AND rating BETWEEN ? AND ? "
                "AND id NOT IN (SELECT puzzle_id FROM attempts WHERE user_id=?) ORDER BY RANDOM() LIMIT 1",
                (f"% {theme} %", u["rating"] - spread, u["rating"] + spread, uid),
            )
            if row:
                break
        if not row:
            raise ApiError("no_puzzles", "По этой теме задач пока нет")
        title = THEMES[theme]
    else:
        row = await db.pick_puzzle(uid, u["rating"])
        if not row:
            raise ApiError("no_puzzles", "Задачи ещё не загружены")
        title = f"Задача #{row['id']}"
    s = P.start_session(row["id"], row["fen"], row["moves"], row["rating"], bool(row["lichess"]))
    sessions[uid] = s
    return _puzzle_payload(row, s, title)


async def _finish(uid: int, s: P.Session, solved: bool) -> dict:
    """solved=True — дошёл до конца сам (возможно, с ошибками); False — сдался."""
    sessions.pop(uid, None)
    u = await db.get_user(uid)
    score = P.result_score(solved, s.mistakes, s.hint_used)
    solved_clean = solved and s.mistakes == 0 and not s.hint_used
    new_rating = P.elo(u["rating"], s.rating, score)
    # решённой (и для серии дней) считается задача, доведённая до конца без сдачи
    await db.record_result(uid, s.puzzle_id, solved, new_rating, daily=s.daily)
    u2 = await db.get_user(uid)
    row = await db.get_puzzle(s.puzzle_id)
    return {
        "solved_clean": solved_clean,
        "solved": solved,
        "mistakes": s.mistakes,
        "hint_used": s.hint_used,
        "rating": u2["rating"],
        "delta": u2["rating"] - u["rating"],
        "streak": u2["streak"],
        "themes": themes_ru(row["themes"]),
        "free_left": None if is_vip(u2) else max(0, FREE_DAILY - await db.attempts_today(uid)),
    }


async def move(uid: int, uci: str) -> dict:
    s = sessions.get(uid)
    if not s:
        raise ApiError("no_session", "Нет активной задачи")
    mv = P.resolve_uci(s, uci)
    if mv is None:
        return {"result": "illegal", **P.board_state(s)}
    if not P.check_move(s, mv):
        s.mistakes += 1
        return {"result": "wrong", "mistakes": s.mistakes, **P.board_state(s)}
    my_uci = mv.uci()
    my_san = s.board.san(mv)
    solved, reply_san = P.play_correct(s, mv)
    out = {"result": "solved" if solved else "correct", "my_move": my_uci, "my_san": my_san,
           "mid_fen": s.mid_fen, "reply_san": reply_san, **P.board_state(s)}
    if solved:
        out["finish"] = await _finish(uid, s, solved=True)
    return out


async def hint(uid: int) -> dict:
    s = sessions.get(uid)
    if not s:
        raise ApiError("no_session", "Нет активной задачи")
    if not is_vip(await db.get_user(uid)):
        raise ApiError("vip_required", "Подсказки — в VIP")
    s.hint_used = True
    exp = s.solution[s.idx]
    return {"square": exp[:2], "text": P.hint(s)}


async def give_up(uid: int) -> dict:
    s = sessions.get(uid)
    if not s:
        raise ApiError("no_session", "Нет активной задачи")
    solution = P.solution_text(s)
    first = s.solution[s.idx][:4]
    return {"solution": solution, "first_move": first, "finish": await _finish(uid, s, solved=False)}


async def top() -> list[dict]:
    return [{"name": (r["name"] or "Игрок")[:20], "rating": r["rating"], "solved": r["solved"]}
            for r in await db.leaderboard(20)]


async def buy_link(plan: str) -> str:
    if plan not in PLANS:
        raise ApiError("bad_plan")
    title, stars, days, sub = PLANS[plan]
    return await bot.create_invoice_link(
        title=title,
        description=("Безлимит задач, подсказки и тренировка по темам. Продлевается каждые 30 дней, отмена в любой момент."
                     if sub else f"Безлимит задач, подсказки и тренировка по темам на {days} дней."),
        payload=plan, currency="XTR", prices=[LabeledPrice(label=title, amount=stars)],
        subscription_period=SUB_PERIOD if sub else None,
    )


async def cancel_subscription(uid: int) -> str:
    u = await db.get_user(uid)
    if not u or not u["sub_charge_id"]:
        raise ApiError("no_subscription", "Нет активной подписки с автопродлением")
    await bot.edit_user_star_subscription(user_id=uid, telegram_payment_charge_id=u["sub_charge_id"], is_canceled=True)
    await db.set_field(uid, "sub_charge_id", None)
    return f"Автопродление отменено. VIP работает до {time.strftime('%d.%m.%Y', time.localtime(u['vip_until']))}."


async def toggle_daily(uid: int) -> bool:
    u = await db.get_user(uid)
    new = 0 if u["daily_sub"] else 1
    await db.set_field(uid, "daily_sub", new)
    return bool(new)


def plans_payload() -> list[dict]:
    return [{"key": k, "title": t, "stars": s, "days": d, "subscription": sub} for k, (t, s, d, sub) in PLANS.items()]
