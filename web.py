"""HTTP-сервер: отдаёт Mini App и JSON API. Пользователь определяется по подписанным initData Telegram."""
import json
import logging
import os
import time
from pathlib import Path

from aiohttp import web
from aiogram.utils.web_app import safe_parse_webapp_init_data

import core
import online

BOT_TOKEN = os.environ["BOT_TOKEN"]
DEV_MODE = os.getenv("DEV_MODE", "0") == "1"   # локальная отладка в обычном браузере без Telegram
INDEX_HTML = Path(__file__).parent / "index.html"  # всё в одной папке — удобно загружать на Amvera
INIT_DATA_MAX_AGE = 24 * 3600


async def current_user(request: web.Request) -> int:
    init = request.headers.get("X-Init-Data", "")
    if init:
        try:
            data = safe_parse_webapp_init_data(BOT_TOKEN, init)
        except ValueError:
            raise web.HTTPUnauthorized(text="bad init data")
        if data.auth_date and time.time() - data.auth_date.timestamp() > INIT_DATA_MAX_AGE:
            raise web.HTTPUnauthorized(text="init data expired")
        await core.ensure_user(data.user.id, data.user.first_name, data.start_param)
        return data.user.id
    if DEV_MODE:  # только локальная отладка: ?dev=2 в адресе — второй тестовый игрок
        hdr = request.headers.get("X-Dev-User", "")
        uid = int(hdr) if hdr.isdigit() and 0 < int(hdr) < 100 else 1
        await core.ensure_user(uid, f"Тестер {uid}")
        return uid
    raise web.HTTPUnauthorized(text="open the app from Telegram")


def api(handler):
    async def wrapper(request: web.Request):
        uid = await current_user(request)
        body = dict(request.query)
        if request.method == "POST" and request.can_read_body:
            try:
                body = await request.json()
            except json.JSONDecodeError:
                body = {}
        try:
            return web.json_response(await handler(uid, body))
        except core.ApiError as e:
            return web.json_response({"error": e.code, "message": e.message}, status=400)
        except Exception:  # noqa: BLE001
            logging.exception("api error")
            return web.json_response({"error": "server", "message": "Ошибка сервера, попробуйте ещё раз"}, status=500)
    return wrapper


@api
async def h_me(uid, body):
    return {"me": await core.profile(uid), "plans": core.plans_payload(),
            "levels": core.LEVELS, "themes": core.THEMES}


@api
async def h_level(uid, body):
    await core.set_level(uid, int(body.get("rating", 0)))
    return {"me": await core.profile(uid)}


@api
async def h_puzzle(uid, body):
    return await core.new_puzzle(uid, body.get("mode", "normal"), body.get("theme"))


@api
async def h_current(uid, body):
    return {"puzzle": await core.current(uid)}


@api
async def h_move(uid, body):
    return await core.move(uid, str(body.get("uci", ""))[:5])


@api
async def h_hint(uid, body):
    return await core.hint(uid)


@api
async def h_giveup(uid, body):
    return await core.give_up(uid)


@api
async def h_top(uid, body):
    return {"top": await core.top()}


@api
async def h_buy(uid, body):
    return {"link": await core.buy_link(body.get("plan", ""))}


@api
async def h_cancel_sub(uid, body):
    return {"message": await core.cancel_subscription(uid)}


@api
async def h_daily_toggle(uid, body):
    return {"daily_sub": await core.toggle_daily(uid)}


# ---------- онлайн-партии ----------
@api
async def h_online_find(uid, body):
    return await online.find(uid, str(body.get("tc", "5+0")))


@api
async def h_online_cancel(uid, body):
    online.cancel(uid)
    return {"ok": True}


@api
async def h_online_state(uid, body):
    return await online.state(uid, body.get("gid"), body.get("v"))


@api
async def h_online_move(uid, body):
    return await online.move(uid, body.get("gid"), str(body.get("uci", ""))[:5])


@api
async def h_online_resign(uid, body):
    return await online.resign(uid, body.get("gid"))


@api
async def h_online_count(uid, body):
    return online.online_count()


async def h_index(request):
    resp = web.FileResponse(INDEX_HTML)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", h_index)
    app.router.add_get("/api/me", h_me)
    for path, h in [("level", h_level), ("puzzle", h_puzzle), ("move", h_move), ("hint", h_hint),
                    ("giveup", h_giveup), ("buy", h_buy), ("cancel_sub", h_cancel_sub),
                    ("daily_toggle", h_daily_toggle)]:
        app.router.add_post(f"/api/{path}", h)
    app.router.add_get("/api/top", h_top)
    app.router.add_get("/api/current", h_current)
    app.router.add_post("/api/online/find", h_online_find)
    app.router.add_post("/api/online/cancel", h_online_cancel)
    app.router.add_get("/api/online/state", h_online_state)
    app.router.add_post("/api/online/move", h_online_move)
    app.router.add_post("/api/online/resign", h_online_resign)
    app.router.add_get("/api/online/count", h_online_count)
    return app
