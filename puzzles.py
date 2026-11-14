"""Шахматная логика: разбор ходов, проверка решений, рейтинг, картинка доски."""
from dataclasses import dataclass, field
from urllib.parse import quote

import chess

# Русская нотация -> английская (Кр, Ф, Л, С, К)
RU_PIECES = [("Кр", "K"), ("кр", "K"), ("Ф", "Q"), ("Л", "R"), ("С", "B"), ("К", "N")]


@dataclass
class Session:
    """Состояние решения одной задачи пользователем."""
    puzzle_id: int
    rating: int
    board: chess.Board
    solution: list[str]          # UCI-ходы: мой, ответ соперника, мой, ...
    idx: int = 0                 # индекс следующего ожидаемого хода игрока
    mistakes: int = 0
    hint_used: bool = False
    last_move: str | None = None
    player_color: bool = chess.WHITE
    daily: bool = False
    history: list[str] = field(default_factory=list)
    mid_fen: str | None = None   # позиция после хода игрока (до ответа соперника) — для анимации
    title: str = "Задача"


def start_session(puzzle_id: int, fen: str, moves: str, rating: int, lichess_format: bool, daily=False) -> Session:
    """lichess_format: первый ход в moves — ход соперника, который надо сыграть автоматически."""
    board = chess.Board(fen)
    seq = moves.split()
    last = None
    if lichess_format:
        board.push_uci(seq[0])
        last = seq[0]
        seq = seq[1:]
    return Session(puzzle_id, rating, board, seq, last_move=last, player_color=board.turn, daily=daily)


def resolve_uci(s: Session, uci: str) -> chess.Move | None:
    """Ход из приложения (from+to). Превращение пешки подбирается по решению, иначе ферзь."""
    try:
        mv = chess.Move.from_uci(uci)
    except ValueError:
        return None
    legal = s.board.legal_moves
    if mv in legal:
        return mv
    if len(uci) == 4:
        exp = s.solution[s.idx] if s.idx < len(s.solution) else ""
        cand = chess.Move.from_uci(exp) if exp[:4] == uci and len(exp) == 5 else chess.Move.from_uci(uci + "q")
        if cand in legal:
            return cand
    return None


def board_state(s: Session) -> dict:
    """Состояние доски для приложения."""
    b = s.board
    return {
        "fen": b.fen(),
        "legal": [m.uci()[:4] for m in b.legal_moves],
        "last_move": s.last_move,
        "check": chess.square_name(b.king(b.turn)) if b.is_check() else None,
        "turn": "w" if b.turn == chess.WHITE else "b",
    }


def parse_move(board: chess.Board, text: str) -> chess.Move | None:
    t = text.strip().replace("х", "x").replace("Х", "x").replace("0-0-0", "O-O-O").replace("0-0", "O-O")
    for ru, en in RU_PIECES:
        if t.startswith(ru):
            t = en + t[len(ru):]
            break
    t = t.rstrip("+#!?")
    for parser in (board.parse_san, board.parse_uci):
        for variant in (t, t.lower()):
            try:
                return parser(variant)
            except ValueError:
                continue
    return None


def check_move(s: Session, move: chess.Move) -> bool:
    """Верен ли ход. На последнем ходу принимаем любой мат (как на Lichess)."""
    expected = s.solution[s.idx]
    if move.uci() == expected:
        return True
    if s.idx == len(s.solution) - 1:
        b = s.board.copy()
        b.push(move)
        return b.is_checkmate()
    return False


def play_correct(s: Session, move: chess.Move) -> tuple[bool, str | None]:
    """Делает ход игрока и ответ соперника. Возвращает (решено, SAN ответа соперника)."""
    s.history.append(s.board.san(move))
    s.board.push(move)
    s.last_move = move.uci()
    s.mid_fen = s.board.fen()
    s.idx += 1
    if s.idx >= len(s.solution):
        return True, None
    reply = chess.Move.from_uci(s.solution[s.idx])
    san = s.board.san(reply)
    s.board.push(reply)
    s.last_move = reply.uci()
    s.idx += 1
    return s.idx >= len(s.solution), san


def hint(s: Session) -> str:
    mv = chess.Move.from_uci(s.solution[s.idx])
    piece = s.board.piece_at(mv.from_square)
    names = {chess.PAWN: "пешкой", chess.KNIGHT: "конём", chess.BISHOP: "слоном",
             chess.ROOK: "ладьёй", chess.QUEEN: "ферзём", chess.KING: "королём"}
    return f"Ходите {names[piece.piece_type]} с поля {chess.square_name(mv.from_square)}"


def solution_text(s: Session) -> str:
    b = s.board.copy()
    out = []
    for u in s.solution[s.idx:]:
        mv = chess.Move.from_uci(u)
        out.append(b.san(mv))
        b.push(mv)
    return " ".join(out)


def result_score(solved: bool, mistakes: int, hint_used: bool) -> float:
    """Насколько хорошо решена задача (0..1) — от этого зависит изменение рейтинга.
    Одна ошибка штрафуется мягко, сдача — полностью."""
    if not solved:
        return 0.0
    if hint_used or mistakes >= 2:
        return 0.25
    if mistakes == 1:
        return 0.45
    return 1.0


def level_factor(rating: int, gaining: bool) -> float:
    """Чем выше уровень, тем меньше плюс за победу и тем больше минус за поражение.
    600: ×1.4 за победу / ×0.6 за поражение · 1400: ×1.0 / ×1.0 · 2400: ×0.5 / ×1.5."""
    t = (rating - 1400) / 1000          # 0 на уровне 1400
    f = 1 - 0.5 * t if gaining else 1 + 0.5 * t
    return min(1.5, max(0.5, f))


def adjust_delta(rating: int, raw: float) -> int:
    return round(raw * level_factor(rating, raw > 0))


def elo(user: int, puzzle: int, score: float | bool, k: int = 32) -> int:
    expected = 1 / (1 + 10 ** ((puzzle - user) / 400))
    return max(100, user + adjust_delta(user, k * (float(score) - expected)))


def board_image_url(s: Session) -> str:
    color = "white" if s.player_color == chess.WHITE else "black"
    url = f"https://lichess1.org/export/fen.gif?fen={quote(s.board.fen())}&color={color}"
    if s.last_move:
        url += f"&lastMove={s.last_move}"
    return url


def board_text(s: Session) -> str:
    b = s.board if s.player_color == chess.WHITE else s.board.transform(chess.flip_vertical).transform(chess.flip_horizontal)
    return "<pre>" + b.unicode(invert_color=True, empty_square="·") + "</pre>"


def side_to_move(s: Session) -> str:
    return "белых" if s.board.turn == chess.WHITE else "чёрных"


# --- стартовый набор: позиции «мат в 1 ход». Решение вычисляется и проверяется автоматически. ---
SEED_FENS = [
    ("6k1/5ppp/8/8/8/8/5PPP/3R2K1 w - - 0 1", 500),
    ("rnbqkb1r/pppp1ppp/5n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 0 1", 600),
    ("k7/8/1K6/8/8/8/8/7R w - - 0 1", 450),
    ("6rk/6pp/7N/8/8/8/8/6K1 w - - 0 1", 800),
    ("7k/8/6K1/8/8/8/8/Q7 w - - 0 1", 500),
    ("3r2k1/8/8/8/8/8/5PPP/6K1 b - - 0 1", 500),
    ("rnbqkbnr/pppp1ppp/8/4p3/6P1/5P2/PPPPP2P/RNBQKBNR b KQkq - 0 1", 650),
    ("k7/2Q5/1K6/8/8/8/8/8 w - - 0 1", 400),
    ("8/8/8/8/8/5k2/7r/5K2 b - - 0 1", 550),
    ("1k6/ppp5/8/8/8/8/8/K2R4 w - - 0 1", 450),
    ("6k1/6p1/6Kp/8/8/8/8/1R6 w - - 0 1", 600),
    ("4k3/4P3/4K3/8/8/8/8/7Q w - - 0 1", 550),
    ("8/8/8/8/8/1k6/7q/K7 b - - 0 1", 450),
]


def mating_moves(fen: str) -> list[str]:
    b = chess.Board(fen)
    if not b.is_valid():
        return []
    out = []
    for mv in b.legal_moves:
        b.push(mv)
        if b.is_checkmate():
            out.append(mv.uci())
        b.pop()
    return out


def seed_puzzles() -> list[tuple[str, str, int]]:
    """Возвращает (fen, moves, rating) только для проверенных позиций."""
    res = []
    for fen, rating in SEED_FENS:
        mates = mating_moves(fen)
        if mates:
            res.append((fen, mates[0], rating))
    return res
