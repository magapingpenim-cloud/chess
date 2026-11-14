"""Онлайн-партии между игроками: подбор соперника, часы, проверка ходов на сервере, рейтинг партий."""
import asyncio
import itertools
import random
import time
from dataclasses import dataclass, field

import chess

import core
from core import ApiError, db
from puzzles import adjust_delta

TIME_CONTROLS = {"3+0": (180, 0), "5+0": (300, 0), "10+0": (600, 0), "3+2": (180, 2)}
QUEUE_STALE = 7.0          # сек без опроса — игрок ушёл из поиска
FIRST_MOVE_TIMEOUT = 30.0  # не сделал первый ход — партия отменяется без рейтинга
KEEP_FINISHED = 600        # сколько хранить законченную партию для показа результата
K_FACTOR = 24

_ids = itertools.count(1)


@dataclass
class Game:
    id: int
    tc: str
    white: int
    black: int
    names: dict
    ratings: dict
    clock: dict                    # секунд осталось: {chess.WHITE: .., chess.BLACK: ..}
    inc: int
    board: chess.Board = field(default_factory=chess.Board)
    turn_started: float = field(default_factory=time.monotonic)
    last_move: str | None = None
    status: str = "playing"        # playing | over
    result: str | None = None      # 1-0 | 0-1 | 1/2-1/2 | aborted
    reason: str | None = None
    deltas: dict = field(default_factory=dict)
    version: int = 0
    finished_at: float = 0.0
    event: asyncio.Event = field(default_factory=asyncio.Event)

    def color_of(self, uid: int) -> bool:
        return chess.WHITE if uid == self.white else chess.BLACK

    def remaining(self, color: bool, now: float | None = None) -> float:
        left = self.clock[color]
        if self.status == "playing" and self.board.turn == color and self._clock_running(color):
            left -= (now or time.monotonic()) - self.turn_started
        return max(0.0, left)

    def _clock_running(self, color: bool) -> bool:
        # часы стороны идут после её первого хода (как на Lichess): первый ход без спешки, но с таймаутом
        return len(self.board.move_stack) >= 2

    def notify(self):
        self.version += 1
        self.event.set()
        self.event = asyncio.Event()


games: dict[int, Game] = {}
user_game: dict[int, int] = {}
queue: dict[int, tuple[str, float]] = {}   # uid -> (tc, когда последний раз опрашивал)


def _cleanup():
    now = time.monotonic()
    for gid in [g.id for g in games.values() if g.status == "over" and now - g.finished_at > KEEP_FINISHED]:
        games.pop(gid, None)
    for uid in [u for u, (_, seen) in queue.items() if now - seen > QUEUE_STALE]:
        queue.pop(uid, None)


async def find(uid: int, tc: str) -> dict:
    if tc not in TIME_CONTROLS:
        raise ApiError("bad_tc", "Неизвестный контроль времени")
    _cleanup()
    gid = user_game.get(uid)
    if gid and gid in games and games[gid].status == "playing":
        return {"status": "matched", "game": await state(uid, gid)}
    now = time.monotonic()
    opponent = next((u for u, (t, seen) in queue.items() if u != uid and t == tc and now - seen <= QUEUE_STALE), None)
    if opponent is None:
        queue[uid] = (tc, now)
        return {"status": "waiting", "in_queue": sum(1 for t, _ in queue.values() if t == tc)}
    queue.pop(opponent, None)
    queue.pop(uid, None)
    g = await _create(uid, opponent, tc)
    return {"status": "matched", "game": await state(uid, g.id)}


def cancel(uid: int):
    queue.pop(uid, None)


async def _create(a: int, b: int, tc: str) -> Game:
    white, black = (a, b) if random.random() < 0.5 else (b, a)
    ua, ub = await db.get_user(white), await db.get_user(black)
    base, inc = TIME_CONTROLS[tc]
    g = Game(
        id=next(_ids), tc=tc, white=white, black=black,
        names={chess.WHITE: (ua["name"] or "Игрок")[:20], chess.BLACK: (ub["name"] or "Игрок")[:20]},
        ratings={chess.WHITE: ua["online_rating"], chess.BLACK: ub["online_rating"]},
        clock={chess.WHITE: float(base), chess.BLACK: float(base)}, inc=inc,
    )
    games[g.id] = g
    user_game[white] = user_game[black] = g.id
    return g


def _get(uid: int, gid) -> Game:
    try:
        g = games[int(gid)]
    except (KeyError, ValueError, TypeError):
        raise ApiError("no_game", "Партия не найдена")
    if uid not in (g.white, g.black):
        raise ApiError("no_game", "Партия не найдена")
    return g


def _elo(ra: int, rb: int, score: float) -> int:
    raw = K_FACTOR * (score - 1 / (1 + 10 ** ((rb - ra) / 400)))
    return adjust_delta(ra, raw)   # чем выше рейтинг игрока, тем дороже поражение и скромнее победа


async def _finish(g: Game, result: str, reason: str):
    if g.status == "over":
        return
    g.status, g.result, g.reason, g.finished_at = "over", result, reason, time.monotonic()
    if result != "aborted":
        sw = {"1-0": 1.0, "0-1": 0.0}.get(result, 0.5)
        rw, rb = g.ratings[chess.WHITE], g.ratings[chess.BLACK]
        dw, db_ = _elo(rw, rb, sw), _elo(rb, rw, 1 - sw)
        g.deltas = {g.white: dw, g.black: db_}
        await db.update_online(g.white, max(100, rw + dw))
        await db.update_online(g.black, max(100, rb + db_))
    for u in (g.white, g.black):
        if user_game.get(u) == g.id:
            user_game.pop(u, None)
    g.notify()


async def _check_time(g: Game):
    if g.status != "playing":
        return
    now = time.monotonic()
    side = g.board.turn
    if len(g.board.move_stack) < 2 and now - g.turn_started > FIRST_MOVE_TIMEOUT:
        await _finish(g, "aborted", "Первый ход не сделан вовремя — партия отменена")
    elif g.remaining(side, now) <= 0:
        await _finish(g, "0-1" if side == chess.WHITE else "1-0", "Время вышло")


def _state_for(g: Game, uid: int) -> dict:
    color = g.color_of(uid)
    b = g.board
    my_turn = g.status == "playing" and b.turn == color
    now = time.monotonic()
    san, tmp = [], chess.Board()
    for mv in b.move_stack:
        san.append(tmp.san(mv))
        tmp.push(mv)
    return {
        "gid": g.id, "version": g.version, "tc": g.tc,
        "fen": b.fen(),
        "legal": [m.uci()[:4] for m in b.legal_moves] if my_turn else [],
        "last_move": g.last_move,
        "check": chess.square_name(b.king(b.turn)) if b.is_check() else None,
        "turn": "w" if b.turn == chess.WHITE else "b",
        "color": "w" if color == chess.WHITE else "b",
        "clocks": {"w": round(g.remaining(chess.WHITE, now) * 1000), "b": round(g.remaining(chess.BLACK, now) * 1000)},
        "clock_running": g._clock_running(b.turn) and g.status == "playing",
        "me": {"name": g.names[color], "rating": g.ratings[color]},
        "opponent": {"name": g.names[not color], "rating": g.ratings[not color]},
        "status": g.status, "result": g.result, "reason": g.reason,
        "rating_delta": g.deltas.get(uid),
        "moves": san,
    }


async def state(uid: int, gid, since_version=None, wait: float = 20.0) -> dict:
    """Long-poll: если версия не изменилась — ждём хода соперника до wait секунд."""
    g = _get(uid, gid)
    await _check_time(g)
    if since_version is not None and g.status == "playing":
        try:
            since = int(since_version)
        except ValueError:
            since = -1
        if since == g.version:
            ev = g.event
            # просыпаемся и к моменту, когда у ходящего кончится время
            left = g.remaining(g.board.turn) if g._clock_running(g.board.turn) else FIRST_MOVE_TIMEOUT
            try:
                await asyncio.wait_for(ev.wait(), timeout=max(0.2, min(wait, left + 0.3)))
            except asyncio.TimeoutError:
                pass
            await _check_time(g)
    return _state_for(g, uid)


async def move(uid: int, gid, uci: str) -> dict:
    g = _get(uid, gid)
    await _check_time(g)
    if g.status != "playing":
        return _state_for(g, uid)
    color = g.color_of(uid)
    if g.board.turn != color:
        raise ApiError("not_your_turn", "Сейчас ход соперника")
    try:
        mv = chess.Move.from_uci(uci)
    except ValueError:
        raise ApiError("illegal", "Так ходить нельзя")
    if mv not in g.board.legal_moves and len(uci) == 4:
        mv = chess.Move.from_uci(uci + "q")   # превращение пешки — в ферзя
    if mv not in g.board.legal_moves:
        raise ApiError("illegal", "Так ходить нельзя")
    now = time.monotonic()
    if g._clock_running(color):
        g.clock[color] = max(0.0, g.clock[color] - (now - g.turn_started)) + g.inc
    g.board.push(mv)
    g.last_move = mv.uci()
    g.turn_started = now
    outcome = g.board.outcome(claim_draw=True)
    if outcome:
        reasons = {
            chess.Termination.CHECKMATE: "Мат", chess.Termination.STALEMATE: "Пат",
            chess.Termination.INSUFFICIENT_MATERIAL: "Недостаточно материала",
            chess.Termination.THREEFOLD_REPETITION: "Троекратное повторение",
            chess.Termination.FIVEFOLD_REPETITION: "Пятикратное повторение",
            chess.Termination.FIFTY_MOVES: "Правило 50 ходов", chess.Termination.SEVENTYFIVE_MOVES: "Правило 75 ходов",
        }
        await _finish(g, outcome.result(), reasons.get(outcome.termination, "Конец партии"))
    else:
        g.notify()
    return _state_for(g, uid)


async def resign(uid: int, gid) -> dict:
    g = _get(uid, gid)
    if g.status == "playing":
        if len(g.board.move_stack) < 2:
            await _finish(g, "aborted", "Партия отменена")
        else:
            await _finish(g, "0-1" if g.color_of(uid) == chess.WHITE else "1-0", "Сдача")
    return _state_for(g, uid)


def online_count() -> dict:
    return {"playing": sum(1 for g in games.values() if g.status == "playing") * 2, "searching": len(queue)}
