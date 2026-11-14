"""ChessUp: Telegram-бот + Mini App. Бот открывает приложение, принимает оплату и рассылает задачу дня.

Запуск: python bot.py  (поднимает и бота, и веб-сервер приложения на PORT)
"""
import asyncio
import logging
import os
import time
from datetime import datetime

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    PreCheckoutQuery,
    WebAppInfo,
)

import core
import puzzles as P
from core import BOT_NAME, PLANS, REF_BONUS_DAYS, db
from db import today
from web import make_app

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
SUPPORT = os.getenv("SUPPORT_CONTACT", "@your_support")
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")   # публичный HTTPS-адрес приложения
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
DAILY_HOUR = int(os.getenv("DAILY_HOUR", "10"))

router = Router()


def fmt_date(ts) -> str:
    return time.strftime("%d.%m.%Y", time.localtime(ts))


def app_kb(text="♞ Открыть ChessUp"):
    if not WEBAPP_URL:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=WEBAPP_URL))]])


async def notify_admins(bot: Bot, text: str):
    for a in ADMIN_IDS:
        try:
            await bot.send_message(a, text)
        except Exception:  # noqa: BLE001
            pass


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    await core.ensure_user(message.from_user.id, message.from_user.first_name, command.args)
    if not WEBAPP_URL:
        return await message.answer("Приложение ещё не подключено: админ, укажите WEBAPP_URL в .env.")
    await message.answer(
        f"♞ <b>{BOT_NAME}</b> — шахматная качалка для мозга\n\n"
        "Задачи под ваш уровень, рейтинг, серия дней 🔥 и задача дня.\n"
        "5 минут в день — и вы играете сильнее.\n\n"
        "Жмите кнопку 👇",
        reply_markup=app_kb(),
    )


# ---------- оплата (Telegram Stars) ----------
@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    ok = q.invoice_payload in PLANS and q.currency == "XTR" and q.total_amount == PLANS[q.invoice_payload][1]
    await q.answer(ok=ok, error_message=None if ok else "Тариф устарел, откройте VIP заново.")


@router.message(F.successful_payment)
async def on_paid(message: Message, bot: Bot):
    sp = message.successful_payment
    uid = message.from_user.id
    title, stars, days, _ = PLANS.get(sp.invoice_payload, ("VIP", sp.total_amount, 30, False))
    first = await db.paid_count(uid) == 0
    if not await db.add_payment(uid, sp.invoice_payload, sp.total_amount, days, sp.telegram_payment_charge_id):
        return
    renewal = bool(sp.is_recurring and not sp.is_first_recurring)
    if sp.subscription_expiration_date:  # подписка: первая оплата или автопродление
        until = await db.extend_vip_until(uid, sp.subscription_expiration_date)
        if sp.is_first_recurring or not (await db.get_user(uid))["sub_charge_id"]:
            await db.set_field(uid, "sub_charge_id", sp.telegram_payment_charge_id)
        text = f"🔄 Подписка продлена до {fmt_date(until)}." if renewal else \
            f"🎉 Подписка оформлена! VIP до {fmt_date(until)}, продлится автоматически."
    else:
        until = await db.add_vip_days(uid, days)
        text = f"🎉 Спасибо! VIP активен до {fmt_date(until)}."
    await message.answer(text, reply_markup=app_kb("♞ Тренироваться"))
    await notify_admins(bot, f"💰 {uid}: {title} · {sp.total_amount}⭐{' (продление)' if renewal else ''}")

    u = await db.get_user(uid)
    if first and u["referrer_id"] and not u["ref_rewarded"]:
        await db.set_field(uid, "ref_rewarded", 1)
        r_until = await db.add_vip_days(u["referrer_id"], REF_BONUS_DAYS)
        try:
            await bot.send_message(u["referrer_id"], f"🎁 Друг оформил VIP — вам +{REF_BONUS_DAYS} дней (до {fmt_date(r_until)})!")
        except Exception:  # noqa: BLE001
            pass


@router.message(Command("cancel_sub"))
async def cmd_cancel_sub(message: Message):
    try:
        await message.answer(await core.cancel_subscription(message.from_user.id))
    except core.ApiError as e:
        await message.answer(e.message)
    except Exception:  # noqa: BLE001
        await message.answer("Не получилось. Отмените в Telegram: Настройки → Звёзды → Подписки, или /paysupport.")


@router.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


@router.message(Command("paysupport"))
async def cmd_paysupport(message: Message):
    await message.answer(f"Вопросы по оплате и возвратам: {SUPPORT}. Укажите дату и ID платежа из чека.")


@router.message(Command("terms"))
async def cmd_terms(message: Message):
    await message.answer(
        "<b>Условия</b>\n1. VIP — цифровая услуга, оплата в Telegram Stars.\n"
        "2. Месячная подписка продлевается автоматически каждые 30 дней. Отмена: /cancel_sub или в приложении — "
        "VIP останется до конца оплаченного периода.\n"
        "3. Возврат — по запросу в поддержку, если VIP не использовался.\n"
        "4. Задачи — из открытой базы Lichess (CC0).\n"
        f"Поддержка: {SUPPORT}"
    )


# ---------- админ ----------
def admin_only(message: Message) -> bool:
    return message.from_user.id in ADMIN_IDS


@router.message(Command("stats"), admin_only)
async def cmd_stats(message: Message):
    s = await db.stats()
    await message.answer(
        f"👥 Пользователей: {s['users']} · активны сегодня: {s['active_today']}\n"
        f"👑 VIP: {s['vip']}\n⭐ Выручка 30д: {s['rev_30d']} · всего: {s['rev_all']}\n"
        f"🧩 Задач в базе: {s['puzzles']} · решают сейчас: {len(core.sessions)}"
    )


@router.message(Command("link"), admin_only)
async def cmd_link(message: Message, command: CommandObject):
    name = (command.args or "").strip().lower()
    if not core.SOURCE_RE.match(name):
        return await message.answer(
            "Создать ссылку для рекламы: <code>/link adsgram</code>\n"
            "Имя — латиница, цифры и _, до 32 символов. Потом смотрите /sources."
        )
    u = core.bot_username
    await message.answer(
        f"🔗 Ссылки для источника <b>{name}</b>:\n\n"
        f"Сразу в приложение:\n<code>https://t.me/{u}?startapp=src_{name}</code>\n\n"
        f"Через бота (кнопка «Старт»):\n<code>https://t.me/{u}?start=src_{name}</code>\n\n"
        "Первая ссылка работает, если в @BotFather настроен Mini App (Configure Mini App)."
    )


@router.message(Command("sources"), admin_only)
async def cmd_sources(message: Message, command: CommandObject):
    days = int(command.args) if command.args and command.args.strip().isdigit() else None
    rows = await db.sources_report(days)
    if not rows:
        return await message.answer("Пользователей пока нет.")
    pct = lambda a, b: f"{round(100 * a / b)}%" if b else "—"  # noqa: E731
    lines = [f"📈 <b>Источники</b> {'за ' + str(days) + ' дн.' if days else 'за всё время'}\n"
             "пришли → решали → вернулись → заплатили · ⭐"]
    for r in rows:
        lines.append(
            f"\n<b>{r['src']}</b>: {r['users']} → {r['played']} ({pct(r['played'], r['users'])}) → "
            f"{r['returned']} ({pct(r['returned'], r['users'])}) → {r['payers']} ({pct(r['payers'], r['users'])}) · "
            f"{r['stars']}⭐"
        )
    lines.append("\n\n«вернулись» — решали задачи минимум в 2 разных дня. /sources 7 — только пришедшие за 7 дней.")
    await message.answer("".join(lines))


@router.message(Command("refund"), admin_only)
async def cmd_refund(message: Message, command: CommandObject, bot: Bot):
    parts = (command.args or "").split()
    if len(parts) != 2 or not parts[0].isdigit():
        return await message.answer("/refund <user_id> <charge_id>")
    uid, charge = int(parts[0]), parts[1]
    pay = await db.get_payment(charge)
    if not pay or pay["user_id"] != uid or pay["refunded"]:
        return await message.answer("Платёж не найден или уже возвращён")
    await bot.refund_star_payment(user_id=uid, telegram_payment_charge_id=charge)
    await db.mark_refunded(charge)
    await db.remove_vip_days(uid, pay["days"])
    await message.answer(f"Возвращено {pay['stars']}⭐ пользователю {uid}")


@router.message()
async def fallback(message: Message):
    await message.answer("Всё происходит в приложении 👇", reply_markup=app_kb())


# ---------- рассылка задачи дня ----------
async def daily_loop(bot: Bot):
    while True:
        try:
            if WEBAPP_URL and datetime.now().hour >= DAILY_HOUR and await db.kv_get("daily_sent") != today():
                await db.kv_set("daily_sent", today())
                if await core.daily_puzzle_row():
                    for uid in await db.daily_subscribers():
                        try:
                            await bot.send_message(uid, "☀️ Новая задача дня ждёт! Сохраните серию 🔥",
                                                   reply_markup=app_kb("📅 Решить задачу дня"))
                        except Exception:  # noqa: BLE001 — пользователь заблокировал бота
                            pass
                        await asyncio.sleep(0.05)  # лимиты Telegram ~30 сообщений/с
        except Exception:  # noqa: BLE001
            logging.exception("daily_loop")
        await asyncio.sleep(60)


async def setup_profile(bot: Bot):
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="♞ Открыть ChessUp"),
            BotCommand(command="cancel_sub", description="Отменить автопродление"),
            BotCommand(command="terms", description="Условия"),
            BotCommand(command="paysupport", description="Помощь по оплате"),
        ])
        if WEBAPP_URL:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Играть", web_app=WebAppInfo(url=WEBAPP_URL)))
        if (await bot.get_my_name()).name != f"{BOT_NAME} — шахматные задачи":
            await bot.set_my_name(f"{BOT_NAME} — шахматные задачи")
        await bot.set_my_short_description("Шахматная качалка для мозга: 5 минут в день — и рейтинг растёт 🔥")
        await bot.set_my_description(
            f"♞ {BOT_NAME} — шахматные задачи прямо в Telegram.\n\n"
            "• задачи от новичка до мастера\n• рейтинг и серия дней 🔥\n• задача дня каждое утро\n"
            "• подсказки и тренировка по темам в VIP\n\nЖмите «Старт»!"
        )
    except Exception:  # noqa: BLE001 — не критично
        logging.exception("setup_profile")


async def auto_import():
    from import_lichess import import_from_url
    logging.info("Загружаю базу задач Lichess в фоне (несколько минут)…")
    try:
        counts = await asyncio.to_thread(import_from_url, core.DB_PATH)
        logging.info("База Lichess загружена: %s задач. Всего в базе: %s",
                     sum(counts.values()), await db.puzzle_count())
    except Exception:  # noqa: BLE001 — бот продолжает работать на стартовых задачах
        logging.exception("Не удалось загрузить базу Lichess")


async def main():
    logging.basicConfig(level=logging.INFO)
    await db.connect()
    for i, (fen, moves, rating) in enumerate(P.seed_puzzles()):
        await db.add_puzzle(f"seed_{i}", fen, moves, rating, "mateIn1", lichess=0)
    logging.info("Задач в базе: %s", await db.puzzle_count())
    # На сервере (Amvera, есть /data) при первом запуске сами качаем базу Lichess в фоне
    auto = os.getenv("AUTO_IMPORT", "1" if os.path.isdir("/data") else "0") == "1"
    if auto and await db.puzzle_count() < 1000:
        asyncio.create_task(auto_import())

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    core.bot = bot
    core.bot_username = (await bot.get_me()).username
    await setup_profile(bot)

    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, HOST, PORT).start()
    if HOST == "0.0.0.0":  # туннели (ssh) часто ходят на localhost по IPv6
        try:
            await web.TCPSite(runner, "::1", PORT).start()
        except OSError:
            pass
    logging.info("Mini App: http://localhost:%s  (публично: %s)", PORT, WEBAPP_URL or "WEBAPP_URL не задан")

    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(daily_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
