import asyncio
import html
import json
import os
import re
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from redis.asyncio import Redis

# --- НАСТРОЙКИ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ---
TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
REDIS_URL = os.getenv("REDIS_URL", "").strip()
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "admin123").strip()
WEB_HOST = "0.0.0.0"
WEB_PORT = int(os.getenv("PORT", "8080"))

STRANGER_REPLY = "Скачать игру можно тут:\n@noominora_bot"
DB_HASH_KEY = "bot:triggers"

if not TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN")
if not REDIS_URL:
    raise RuntimeError("Не задана переменная окружения REDIS_URL")

# --- ИНИЦИАЛИЗАЦИЯ REDIS И БОТА ---
redis_client = Redis.from_url(REDIS_URL, decode_responses=True)
storage = RedisStorage(redis=redis_client)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)


class MenuState(StatesGroup):
    waiting_phrase = State()
    waiting_answer = State()


# --- РАБОТА С БАЗОЙ В REDIS ---


async def get_all_triggers() -> dict:
    raw = await redis_client.hgetall(DB_HASH_KEY)
    result = {}
    for k, v in raw.items():
        try:
            result[k] = json.loads(v)
        except Exception:
            continue
    return result


async def get_trigger(phrase: str) -> dict | None:
    raw = await redis_client.hget(DB_HASH_KEY, phrase)
    if not raw:
        return None
    return json.loads(raw)


async def save_trigger_db(phrase: str, data: dict) -> None:
    await redis_client.hset(DB_HASH_KEY, phrase, json.dumps(data, ensure_ascii=False))


async def delete_trigger_db(phrase: str) -> None:
    await redis_client.hdel(DB_HASH_KEY, phrase)


async def count_triggers() -> int:
    return await redis_client.hlen(DB_HASH_KEY)


# --- ПАРСИНГ И ОТПРАВКА СООБЩЕНИЙ ---


def extract_buttons_from_html(text: str | None) -> tuple[str, list[list[dict]]]:
    if not text:
        return "", []
    line_pattern = re.compile(r"((?:\[[^\[\]-]+?\s*-\s*(?:https?://|@)[^\s\]]+\]\s*\|?\s*)+)")
    btn_pattern = re.compile(r"\[([^\[\]-]+?)\s*-\s*((?:https?://|@)[^\s\]]+)\]")

    rows = []
    for line_match in line_pattern.finditer(text):
        row = []
        for b_text, raw_url in btn_pattern.findall(line_match.group(1)):
            url = raw_url.strip()
            if url.startswith("@"):
                url = f"https://t.me/{url[1:]}"
            row.append({"text": html.unescape(b_text.strip()), "url": url})
        if row:
            rows.append(row)

    clean_text = line_pattern.sub("", text).strip()
    return clean_text, rows


def serialize_existing_kb(markup: InlineKeyboardMarkup | None) -> list[list[dict]]:
    if not markup or not markup.inline_keyboard:
        return []
    rows = []
    for row in markup.inline_keyboard:
        btn_row = []
        for btn in row:
            if btn.url:
                btn_row.append({"text": btn.text, "url": btn.url})
        if btn_row:
            rows.append(btn_row)
    return rows


def build_markup(rows: list[list[dict]]) -> InlineKeyboardMarkup | None:
    if not rows:
        return None
    keyboard = [
        [InlineKeyboardButton(text=b["text"], url=b["url"]) for b in row]
        for row in rows
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def extract_media_info(message: Message) -> tuple[str, str | None]:
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.video:
        return "video", message.video.file_id
    if message.animation:
        return "animation", message.animation.file_id
    if message.document:
        return "document", message.document.file_id
    if message.sticker:
        return "sticker", message.sticker.file_id
    if message.voice:
        return "voice", message.voice.file_id
    if message.video_note:
        return "video_note", message.video_note.file_id
    return "text", None


async def send_trigger_reply(chat_id: int, item: dict, reply_to_message_id: int | None = None):
    m_type = item.get("media_type", "text")
    file_id = item.get("file_id")
    text = item.get("html_text", "")
    kb = build_markup(item.get("buttons", []))

    kwargs = {"reply_to_message_id": reply_to_message_id} if reply_to_message_id else {}

    if m_type == "photo" and file_id:
        await bot.send_photo(chat_id, file_id, caption=text or None, reply_markup=kb, **kwargs)
    elif m_type == "video" and file_id:
        await bot.send_video(chat_id, file_id, caption=text or None, reply_markup=kb, **kwargs)
    elif m_type == "animation" and file_id:
        await bot.send_animation(chat_id, file_id, caption=text or None, reply_markup=kb, **kwargs)
    elif m_type == "document" and file_id:
        await bot.send_document(chat_id, file_id, caption=text or None, reply_markup=kb, **kwargs)
    elif m_type == "sticker" and file_id:
        await bot.send_sticker(chat_id, file_id, reply_markup=kb, **kwargs)
    elif m_type == "voice" and file_id:
        await bot.send_voice(chat_id, file_id, caption=text or None, reply_markup=kb, **kwargs)
    elif m_type == "video_note" and file_id:
        await bot.send_video_note(chat_id, file_id, reply_markup=kb, **kwargs)
    else:
        await bot.send_message(
            chat_id,
            text or " ",
            reply_markup=kb,
            disable_web_page_preview=True,
            **kwargs,
        )


# --- ИНТЕРФЕЙС МЕНЮ В TELEGRAM ---


async def main_menu_kb() -> InlineKeyboardMarkup:
    total = await count_triggers()
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить заготовку", callback_data="menu:add")],
            [InlineKeyboardButton(text=f"📋 Список заготовок ({total})", callback_data="menu:list:0")],
        ]
    )


def back_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="menu:home")]]
    )


async def main_menu_text() -> str:
    total = await count_triggers()
    render_url = os.getenv("RENDER_EXTERNAL_URL", "")
    web_line = f"Веб-панель: <code>{render_url}</code>\n" if render_url else ""
    return (
        "<b>Панель управления заготовками</b>\n\n"
        f"Активных фраз в базе: <b>{total}</b>\n"
        f"{web_line}"
        "Хранилище: <b>Upstash Redis</b>\n\n"
        "Выберите действие ниже:"
    )


async def safe_edit_menu(chat_id: int, message_id: int | None, text: str, kb: InlineKeyboardMarkup):
    if message_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=kb,
                disable_web_page_preview=True,
            )
            return message_id
        except Exception:
            pass
    msg = await bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)
    return msg.message_id


@router.message(F.chat.type == "private", F.from_user.id != ADMIN_ID)
async def private_stranger(message: Message):
    await message.answer(STRANGER_REPLY)


@router.message(F.chat.type == "private", F.from_user.id == ADMIN_ID, Command("start", "menu"))
async def cmd_menu(message: Message, state: FSMContext):
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass
    msg = await message.answer(await main_menu_text(), reply_markup=await main_menu_kb())
    await state.update_data(menu_msg_id=msg.message_id)


@router.callback_query(F.from_user.id == ADMIN_ID, F.data == "menu:home")
async def cb_home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(menu_msg_id=call.message.message_id)
    await safe_edit_menu(
        call.message.chat.id,
        call.message.message_id,
        await main_menu_text(),
        await main_menu_kb(),
    )
    await call.answer()


@router.callback_query(F.from_user.id == ADMIN_ID, F.data == "menu:add")
async def cb_add_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(MenuState.waiting_phrase)
    await state.update_data(menu_msg_id=call.message.message_id)
    text = (
        "<b>Шаг 1 из 2 — Фраза-триггер</b>\n\n"
        "Отправь слово или фразу, на которую бот должен отвечать в чате.\n"
        "<i>Регистр букв не имеет значения.</i>"
    )
    await safe_edit_menu(call.message.chat.id, call.message.message_id, text, back_menu_kb())
    await call.answer()


@router.message(F.chat.type == "private", F.from_user.id == ADMIN_ID, MenuState.waiting_phrase)
async def step_phrase(message: Message, state: FSMContext):
    data = await state.get_data()
    menu_msg_id = data.get("menu_msg_id")

    try:
        await message.delete()
    except Exception:
        pass

    if not message.text:
        text = (
            "<b>Ошибка:</b> триггер должен быть обычным текстом.\n\n"
            "Напиши фразу ещё раз:"
        )
        new_id = await safe_edit_menu(message.chat.id, menu_msg_id, text, back_menu_kb())
        await state.update_data(menu_msg_id=new_id)
        return

    phrase = message.text.strip().lower()
    await state.update_data(phrase=phrase)
    await state.set_state(MenuState.waiting_answer)

    text = (
        f"<b>Шаг 2 из 2 — Ответ на «{html.escape(phrase)}»</b>\n\n"
        "Отправь готовое сообщение: текст (жирный, курсив, ссылки сохранятся), фото, видео, гифку или пост с кнопками.\n\n"
        "Чтобы добавить свои кнопки, допиши в конце:\n"
        "<code>[Скачать - @noominora_bot]</code>\n"
        "В один ряд через черту: <code>[Кнопка 1 - https://ya.ru] | [Кнопка 2 - @durov]</code>"
    )
    new_id = await safe_edit_menu(message.chat.id, menu_msg_id, text, back_menu_kb())
    await state.update_data(menu_msg_id=new_id)


@router.message(F.chat.type == "private", F.from_user.id == ADMIN_ID, MenuState.waiting_answer)
async def step_answer(message: Message, state: FSMContext):
    data = await state.get_data()
    phrase = data["phrase"]
    menu_msg_id = data.get("menu_msg_id")

    raw_html = message.html_text if message.text else (message.html_caption if message.caption else "")
    clean_html, parsed_rows = extract_buttons_from_html(raw_html)
    existing_rows = serialize_existing_kb(message.reply_markup)
    final_rows = parsed_rows or existing_rows
    media_type, file_id = extract_media_info(message)

    await save_trigger_db(
        phrase,
        {
            "media_type": media_type,
            "file_id": file_id,
            "html_text": clean_html,
            "buttons": final_rows,
        },
    )

    try:
        await message.delete()
    except Exception:
        pass

    await state.clear()
    status_text = (
        f"✅ Заготовка для <b>«{html.escape(phrase)}»</b> сохранена!\n"
        f"Тип: <b>{media_type}</b> | Кнопок: <b>{sum(len(r) for r in final_rows)}</b>\n\n"
        + await main_menu_text()
    )
    new_id = await safe_edit_menu(message.chat.id, menu_msg_id, status_text, await main_menu_kb())
    await state.update_data(menu_msg_id=new_id)


@router.callback_query(F.from_user.id == ADMIN_ID, F.data.startswith("menu:list:"))
async def cb_list(call: CallbackQuery):
    page = int(call.data.split(":")[2])
    db = await get_all_triggers()
    keys = sorted(list(db.keys()))

    if not keys:
        await safe_edit_menu(
            call.message.chat.id,
            call.message.message_id,
            "<b>Список пуст</b>\n\nВы ещё не добавили ни одной фразы.",
            back_menu_kb(),
        )
        await call.answer()
        return

    per_page = 8
    start = page * per_page
    chunk = keys[start : start + per_page]

    rows = []
    for idx, phrase in enumerate(chunk, start=start):
        info = db[phrase]
        btn_count = sum(len(r) for r in info.get("buttons", []))
        label = f"💬 {phrase} [{info.get('media_type', 'text')}, кнопок: {btn_count}]"
        rows.append([InlineKeyboardButton(text=label[:55], callback_data=f"menu:item:{idx}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"menu:list:{page - 1}"))
    if start + per_page < len(keys):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"menu:list:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="menu:home")])

    await safe_edit_menu(
        call.message.chat.id,
        call.message.message_id,
        f"<b>Сохранённые заготовки ({len(keys)} шт.)</b>\nНажмите на фразу для просмотра или удаления:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await call.answer()


@router.callback_query(F.from_user.id == ADMIN_ID, F.data.startswith("menu:item:"))
async def cb_item(call: CallbackQuery):
    idx = int(call.data.split(":")[2])
    db = await get_all_triggers()
    keys = sorted(list(db.keys()))
    if idx < 0 or idx >= len(keys):
        await call.answer("Фраза не найдена", show_alert=True)
        return

    phrase = keys[idx]
    info = db[phrase]
    preview_text = info.get("html_text") or "<i>(без текста, только медиа)</i>"
    if len(preview_text) > 500:
        preview_text = preview_text[:500] + "..."

    text = (
        f"<b>Триггер:</b> <code>{html.escape(phrase)}</code>\n"
        f"<b>Медиа:</b> {info.get('media_type', 'text')}\n"
        f"<b>Кнопок:</b> {sum(len(r) for r in info.get('buttons', []))}\n\n"
        f"<b>Текст ответа:</b>\n{preview_text}"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👁 Тест в ЛС", callback_data=f"menu:test:{idx}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"menu:del:{idx}"),
            ],
            [InlineKeyboardButton(text="⬅️ К списку", callback_data="menu:list:0")],
        ]
    )
    await safe_edit_menu(call.message.chat.id, call.message.message_id, text, kb)
    await call.answer()


@router.callback_query(F.from_user.id == ADMIN_ID, F.data.startswith("menu:test:"))
async def cb_test(call: CallbackQuery):
    idx = int(call.data.split(":")[2])
    db = await get_all_triggers()
    keys = sorted(list(db.keys()))
    if 0 <= idx < len(keys):
        await send_trigger_reply(call.message.chat.id, db[keys[idx]])
    await call.answer("Отправлено для проверки")


@router.callback_query(F.from_user.id == ADMIN_ID, F.data.startswith("menu:del:"))
async def cb_del(call: CallbackQuery):
    idx = int(call.data.split(":")[2])
    db = await get_all_triggers()
    keys = sorted(list(db.keys()))
    if 0 <= idx < len(keys):
        removed = keys[idx]
        await delete_trigger_db(removed)
        await call.answer(f"Удалено: {removed}")
    await safe_edit_menu(
        call.message.chat.id,
        call.message.message_id,
        await main_menu_text(),
        await main_menu_kb(),
    )


# --- ОТВЕТЫ В ГРУППАХ ---


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def group_auto_reply(message: Message):
    incoming = message.text or message.caption
    if not incoming:
        return

    key = incoming.strip().lower()
    item = await get_trigger(key)

    if item:
        try:
            await send_trigger_reply(message.chat.id, item, reply_to_message_id=message.message_id)
        except Exception as e:
            print(f"Ошибка при отправке ответа в чат: {e}")


# --- ВЕБ-ИНТЕРФЕЙС (МИНИМАЛИЗМ: ЧЁРНЫЙ ФОН, БЕЛЫЕ КНОПКИ, UNBOUNDED, LUCIDE ICONS) ---

WEB_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TRIGGERS // CONTROL</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Unbounded:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://unpkg.com/lucide@latest"></script>
<style>
  :root {
    --bg: #000000;
    --surface: #0a0a0a;
    --surface-2: #121212;
    --border: #222222;
    --border-hover: #444444;
    --text: #ffffff;
    --muted: #777777;
    --white: #ffffff;
    --white-hover: #e5e5e5;
    --danger: #ff3b30;
  }

  * {
    box-sizing: border-box;
    font-family: 'Unbounded', -apple-system, BlinkMacSystemFont, sans-serif;
  }

  body {
    margin: 0;
    min-height: 100vh;
    background: var(--bg);
    color: var(--text);
    padding: 32px 20px;
    -webkit-font-smoothing: antialiased;
  }

  /* Экран входа */
  #auth_overlay {
    position: fixed;
    inset: 0;
    z-index: 100;
    background: #000000;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
  }

  .auth-box {
    width: 100%;
    max-width: 400px;
    border: 1px solid var(--border);
    background: var(--surface);
    border-radius: 20px;
    padding: 36px 28px;
  }

  .auth-icon {
    width: 48px;
    height: 48px;
    border-radius: 12px;
    background: var(--white);
    color: #000000;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    margin-bottom: 20px;
  }

  .wrapper {
    max-width: 1260px;
    margin: 0 auto;
  }

  /* Шапка */
  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 16px;
    padding-bottom: 24px;
    margin-bottom: 28px;
    border-bottom: 1px solid var(--border);
  }

  .brand {
    display: flex;
    align-items: center;
    gap: 14px;
  }

  .brand-mark {
    width: 40px;
    height: 40px;
    background: var(--white);
    color: #000000;
    border-radius: 10px;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .brand h1 {
    margin: 0;
    font-size: 18px;
    font-weight: 700;
    letter-spacing: -0.03em;
  }

  .brand p {
    margin: 3px 0 0 0;
    font-size: 11px;
    color: var(--muted);
    font-weight: 400;
  }

  .header-right {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .stat-tag {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    border: 1px solid var(--border);
    background: var(--surface);
    padding: 9px 14px;
    border-radius: 10px;
    font-size: 12px;
  }

  .status-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: #ffffff;
  }

  /* Сетка */
  .grid {
    display: grid;
    grid-template-columns: 1.05fr 0.95fr;
    gap: 24px;
    align-items: start;
  }

  @media (max-width: 960px) {
    .grid { grid-template-columns: 1fr; }
  }

  .panel {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 26px;
  }

  .panel-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 22px;
  }

  .panel-title {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 15px;
    font-weight: 600;
    margin: 0;
  }

  label {
    display: block;
    font-size: 11px;
    font-weight: 500;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin: 18px 0 8px 0;
  }

  input[type="text"],
  input[type="password"],
  textarea {
    width: 100%;
    background: #000000;
    border: 1px solid var(--border);
    color: var(--text);
    border-radius: 12px;
    padding: 13px 14px;
    font-size: 13px;
    font-weight: 400;
    outline: none;
    transition: border-color 0.15s;
  }

  input[type="text"]:focus,
  input[type="password"]:focus,
  textarea:focus {
    border-color: var(--white);
  }

  textarea {
    min-height: 120px;
    resize: vertical;
    line-height: 1.5;
  }

  /* Тулбар форматирования */
  .toolbar {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin-bottom: 8px;
  }

  .tool-btn {
    background: #000000;
    border: 1px solid var(--border);
    color: var(--text);
    padding: 7px 11px;
    border-radius: 8px;
    font-size: 11px;
    font-weight: 500;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    transition: 0.15s;
  }

  .tool-btn:hover {
    border-color: var(--white);
    background: var(--surface-2);
  }

  /* Белые кнопки */
  .btn-white {
    background: var(--white);
    color: #000000;
    border: 1px solid var(--white);
    padding: 13px 18px;
    border-radius: 12px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    transition: background 0.15s, transform 0.1s;
  }

  .btn-white:hover {
    background: var(--white-hover);
  }

  .btn-white:active {
    transform: scale(0.99);
  }

  .btn-white:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-outline {
    background: transparent;
    color: var(--text);
    border: 1px solid var(--border);
    padding: 9px 13px;
    border-radius: 10px;
    font-size: 12px;
    font-weight: 500;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    transition: 0.15s;
  }

  .btn-outline:hover {
    border-color: var(--white);
  }

  .btn-del {
    background: transparent;
    color: var(--muted);
    border: 1px solid var(--border);
    padding: 8px 10px;
    border-radius: 10px;
    font-size: 12px;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    transition: 0.15s;
  }

  .btn-del:hover {
    border-color: var(--danger);
    color: var(--danger);
  }

  /* Конструктор кнопок */
  .btn-row {
    display: flex;
    gap: 8px;
    align-items: center;
    margin-top: 8px;
  }

  .check-wrap {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    color: var(--muted);
    white-space: nowrap;
    cursor: pointer;
    margin: 0;
    text-transform: none;
    letter-spacing: 0;
  }

  .check-wrap input {
    accent-color: #ffffff;
    width: 15px;
    height: 15px;
  }

  /* Кастомный инпут файла */
  .file-box {
    border: 1px dashed var(--border);
    background: #000000;
    border-radius: 12px;
    padding: 12px 14px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }

  .file-box input[type="file"] {
    font-size: 11px;
    color: var(--muted);
    width: 100%;
  }

  .file-box input[type="file"]::file-selector-button {
    background: var(--white);
    color: #000000;
    border: none;
    padding: 7px 12px;
    border-radius: 8px;
    font-family: 'Unbounded', sans-serif;
    font-size: 11px;
    font-weight: 600;
    margin-right: 12px;
    cursor: pointer;
  }

  /* Превью */
  .preview-box {
    margin-top: 20px;
    border: 1px solid var(--border);
    background: #000000;
    border-radius: 14px;
    padding: 16px;
  }

  .preview-caption {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
    margin-bottom: 12px;
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .tg-msg {
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: 14px 14px 14px 4px;
    padding: 13px 15px;
    font-size: 13px;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .tg-msg a {
    color: #ffffff;
    text-decoration: underline;
  }

  .tg-msg code {
    background: #000000;
    border: 1px solid var(--border);
    padding: 2px 6px;
    border-radius: 6px;
    font-family: monospace;
  }

  .tg-kb {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin-top: 8px;
  }

  .tg-kb-line {
    display: flex;
    gap: 6px;
  }

  .tg-kb-item {
    flex: 1;
    background: var(--white);
    color: #000000;
    text-align: center;
    padding: 9px 10px;
    border-radius: 9px;
    font-size: 11px;
    font-weight: 600;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 5px;
  }

  /* Список */
  .search-wrap {
    position: relative;
    margin-bottom: 16px;
  }

  .search-wrap i {
    position: absolute;
    left: 14px;
    top: 50%;
    transform: translateY(-50%);
    color: var(--muted);
    width: 16px;
    height: 16px;
  }

  .search-wrap input {
    padding-left: 40px;
  }

  .list-scroll {
    display: flex;
    flex-direction: column;
    gap: 12px;
    max-height: 780px;
    overflow-y: auto;
    padding-right: 4px;
  }

  .list-scroll::-webkit-scrollbar {
    width: 4px;
  }

  .list-scroll::-webkit-scrollbar-thumb {
    background: #333333;
    border-radius: 4px;
  }

  .item-card {
    background: #000000;
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 16px;
    transition: border-color 0.15s;
  }

  .item-card:hover {
    border-color: var(--border-hover);
  }

  .item-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
  }

  .item-phrase {
    font-size: 14px;
    font-weight: 600;
    color: #ffffff;
  }

  .type-pill {
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    padding: 3px 8px;
    border-radius: 6px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    color: var(--muted);
  }

  .item-text {
    margin-top: 10px;
    font-size: 12.5px;
    color: #bbbbbb;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .item-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 12px;
  }

  .item-chip {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-size: 11px;
    padding: 4px 9px;
    border-radius: 7px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    color: #ffffff;
  }

  /* Уведомление */
  #toast {
    position: fixed;
    bottom: 24px;
    right: 24px;
    z-index: 200;
    background: var(--white);
    color: #000000;
    padding: 13px 18px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 8px;
    transform: translateY(120px);
    opacity: 0;
    transition: all 0.25s ease;
  }

  #toast.show {
    transform: translateY(0);
    opacity: 1;
  }

  #toast.error {
    background: var(--danger);
    color: #ffffff;
  }

  svg.lucide {
    width: 15px;
    height: 15px;
    stroke-width: 2;
    flex-shrink: 0;
  }
</style>
</head>
<body>

<!-- Экран авторизации -->
<div id="auth_overlay">
  <div class="auth-box">
    <div class="auth-icon">
      <i data-lucide="lock" style="width: 22px; height: 22px;"></i>
    </div>
    <h2 style="margin: 0 0 8px 0; font-size: 18px; font-weight: 700;">ДОСТУП В ПАНЕЛЬ</h2>
    <p style="margin: 0 0 22px 0; font-size: 12px; color: var(--muted); line-height: 1.5;">
      Введите пароль из переменной WEB_PASSWORD
    </p>
    <input type="password" id="pass_input" placeholder="Пароль" onkeydown="if(event.key==='Enter') login()">
    <button class="btn-white" style="width: 100%; margin-top: 14px;" onclick="login()">
      <span>Войти</span>
      <i data-lucide="arrow-right"></i>
    </button>
  </div>
</div>

<div class="wrapper">
  <header class="header">
    <div class="brand">
      <div class="brand-mark">
        <i data-lucide="terminal" style="width: 20px; height: 20px;"></i>
      </div>
      <div>
        <h1>TRIGGER STUDIO</h1>
        <p>RENDER + UPSTASH REDIS</p>
      </div>
    </div>

    <div class="header-right">
      <div class="stat-tag">
        <span class="status-dot"></span>
        <span>БАЗА: <strong id="total_count">0</strong></span>
      </div>
      <button class="btn-outline" onclick="logout()" title="Выйти">
        <i data-lucide="log-out"></i>
        <span>Выход</span>
      </button>
    </div>
  </header>

  <div class="grid">
    <!-- Левая колонка: редактор -->
    <section class="panel">
      <div class="panel-head">
        <h2 class="panel-title">
          <i data-lucide="plus-square"></i>
          <span id="form_title">НОВАЯ ЗАГОТОВКА</span>
        </h2>
        <button class="btn-outline" onclick="resetForm()">
          <i data-lucide="rotate-ccw"></i>
          <span>Сброс</span>
        </button>
      </div>

      <label>Фраза-триггер (регистр не важен)</label>
      <input type="text" id="phrase" placeholder="Например: как скачать">

      <label>Текст ответа (HTML)</label>
      <div class="toolbar">
        <button type="button" class="tool-btn" onclick="wrapTag('b')"><i data-lucide="bold"></i> B</button>
        <button type="button" class="tool-btn" onclick="wrapTag('i')"><i data-lucide="italic"></i> I</button>
        <button type="button" class="tool-btn" onclick="wrapTag('u')"><i data-lucide="underline"></i> U</button>
        <button type="button" class="tool-btn" onclick="wrapTag('s')"><i data-lucide="strikethrough"></i> S</button>
        <button type="button" class="tool-btn" onclick="wrapTag('code')"><i data-lucide="code"></i> Code</button>
        <button type="button" class="tool-btn" onclick="wrapTag('tg-spoiler')"><i data-lucide="eye-off"></i> Спойлер</button>
        <button type="button" class="tool-btn" onclick="insertLink()"><i data-lucide="link"></i> Ссылка</button>
      </div>
      <textarea id="html_text" placeholder="Введите текст ответа..." oninput="updatePreview()"></textarea>

      <label>Медиафайл (фото, видео, гиф, документ)</label>
      <div class="file-box">
        <input type="file" id="media_file" onchange="updatePreview()">
      </div>
      <input type="hidden" id="existing_media_type" value="text">
      <input type="hidden" id="existing_file_id" value="">

      <label>Кнопки под сообщением</label>
      <div id="buttons_box"></div>
      <button type="button" class="btn-outline" style="width: 100%; margin-top: 10px;" onclick="addBtnRow()">
        <i data-lucide="plus"></i>
        <span>Добавить кнопку</span>
      </button>

      <div class="preview-box">
        <div class="preview-caption">
          <i data-lucide="eye"></i>
          <span>Предпросмотр сообщения</span>
        </div>
        <div class="tg-msg" id="preview_bubble">
          <span style="color: var(--muted);">Текст ответа появится здесь...</span>
        </div>
        <div class="tg-kb" id="preview_kb"></div>
      </div>

      <button type="button" class="btn-white" id="save_btn" style="width: 100%; margin-top: 20px;" onclick="saveTrigger()">
        <i data-lucide="check"></i>
        <span>Сохранить заготовку</span>
      </button>
    </section>

    <!-- Правая колонка: список -->
    <section class="panel">
      <div class="panel-head">
        <h2 class="panel-title">
          <i data-lucide="layers"></i>
          <span>СПИСОК ФРАЗ</span>
        </h2>
        <button class="btn-outline" onclick="loadTriggers()">
          <i data-lucide="refresh-cw"></i>
          <span>Обновить</span>
        </button>
      </div>

      <div class="search-wrap">
        <i data-lucide="search"></i>
        <input type="text" id="search_input" placeholder="Поиск по фразе или тексту..." oninput="renderList()">
      </div>

      <div class="list-scroll" id="list_box"></div>
    </section>
  </div>
</div>

<div id="toast">
  <i data-lucide="check-circle-2"></i>
  <span id="toast_text">Сохранено</span>
</div>

<script>
let currentDb = {};
let authPass = localStorage.getItem('tg_admin_pass') || '';

function refreshIcons() {
  if (window.lucide) {
    lucide.createIcons();
  }
}

function showToast(msg, isError = false) {
  const t = document.getElementById('toast');
  const txt = document.getElementById('toast_text');
  txt.textContent = msg;
  t.className = isError ? 'show error' : 'show';
  setTimeout(() => { t.className = ''; }, 2600);
}

async function login() {
  const val = document.getElementById('pass_input').value.trim();
  if (!val) return;
  authPass = val;
  const ok = await loadTriggers();
  if (ok) {
    localStorage.setItem('tg_admin_pass', authPass);
    document.getElementById('auth_overlay').style.display = 'none';
  } else {
    showToast('Неверный пароль', true);
  }
}

function logout() {
  localStorage.removeItem('tg_admin_pass');
  location.reload();
}

function wrapTag(tag) {
  const ta = document.getElementById('html_text');
  const start = ta.selectionStart;
  const end = ta.selectionEnd;
  const selected = ta.value.substring(start, end) || 'текст';
  ta.setRangeText(`<${tag}>${selected}</${tag}>`, start, end, 'select');
  ta.focus();
  updatePreview();
}

function insertLink() {
  const url = prompt('Ссылка (https://... или @username):', 'https://t.me/noominora_bot');
  if (!url) return;
  const cleanUrl = url.startsWith('@') ? 'https://t.me/' + url.slice(1) : url;
  const ta = document.getElementById('html_text');
  const start = ta.selectionStart;
  const end = ta.selectionEnd;
  const selected = ta.value.substring(start, end) || 'Скачать';
  ta.setRangeText(`<a href="${cleanUrl}">${selected}</a>`, start, end, 'select');
  ta.focus();
  updatePreview();
}

function addBtnRow(text = '', url = '', sameRow = false) {
  const box = document.getElementById('buttons_box');
  const div = document.createElement('div');
  div.className = 'btn-row';
  div.innerHTML = `
    <input type="text" placeholder="Текст кнопки" value="${text}" class="b-text" style="flex:1" oninput="updatePreview()">
    <input type="text" placeholder="https://... или @bot" value="${url}" class="b-url" style="flex:1" oninput="updatePreview()">
    <label class="check-wrap">
      <input type="checkbox" class="b-same" ${sameRow ? 'checked' : ''} onchange="updatePreview()"> В ряд
    </label>
    <button type="button" class="btn-del" onclick="this.parentElement.remove(); updatePreview()">
      <i data-lucide="x"></i>
    </button>
  `;
  box.appendChild(div);
  refreshIcons();
  updatePreview();
}

function collectButtonRows() {
  const rows = [];
  document.querySelectorAll('#buttons_box .btn-row').forEach(el => {
    const text = el.querySelector('.b-text').value.trim();
    let url = el.querySelector('.b-url').value.trim();
    const same = el.querySelector('.b-same').checked;
    if (!text || !url) return;
    if (url.startsWith('@')) url = 'https://t.me/' + url.slice(1);
    if (same && rows.length > 0) {
      rows[rows.length - 1].push({ text, url });
    } else {
      rows.push([{ text, url }]);
    }
  });
  return rows;
}

function updatePreview() {
  const text = document.getElementById('html_text').value;
  const fileInput = document.getElementById('media_file');
  const exMedia = document.getElementById('existing_media_type').value;
  const bubble = document.getElementById('preview_bubble');

  let mediaNote = '';
  if (fileInput.files.length > 0) {
    mediaNote = `<div style="border:1px solid #333; padding:6px 10px; border-radius:8px; margin-bottom:8px; font-size:11px; color:#fff;">Файл: ${fileInput.files[0].name}</div>`;
  } else if (exMedia && exMedia !== 'text') {
    mediaNote = `<div style="border:1px solid #333; padding:6px 10px; border-radius:8px; margin-bottom:8px; font-size:11px; color:#fff;">Медиа в Telegram: ${exMedia}</div>`;
  }

  if (!text && !mediaNote) {
    bubble.innerHTML = '<span style="color: var(--muted);">Текст ответа появится здесь...</span>';
  } else {
    bubble.innerHTML = mediaNote + (text || '');
  }

  const rows = collectButtonRows();
  const kbBox = document.getElementById('preview_kb');
  kbBox.innerHTML = rows.map(row => `
    <div class="tg-kb-line">
      ${row.map(b => `<div class="tg-kb-item"><span>${b.text}</span><i data-lucide="arrow-up-right"></i></div>`).join('')}
    </div>
  `).join('');
  refreshIcons();
}

async function loadTriggers() {
  try {
    const res = await fetch('/api/triggers', {
      headers: { 'X-Admin-Password': authPass }
    });
    if (res.status === 401) return false;
    currentDb = await res.json();
    document.getElementById('total_count').textContent = Object.keys(currentDb).length;
    renderList();
    return true;
  } catch (e) {
    return false;
  }
}

function renderList() {
  const box = document.getElementById('list_box');
  const query = document.getElementById('search_input').value.trim().toLowerCase();
  const keys = Object.keys(currentDb).sort().filter(k => {
    const item = currentDb[k];
    return k.includes(query) || (item.html_text || '').toLowerCase().includes(query);
  });

  if (!keys.length) {
    box.innerHTML = '<p style="color:var(--muted); text-align:center; padding: 36px 0; font-size: 12px;">Список пуст</p>';
    return;
  }

  box.innerHTML = '';
  keys.forEach(phrase => {
    const item = currentDb[phrase];
    const div = document.createElement('div');
    div.className = 'item-card';
    const btns = (item.buttons || []).flat().map(b => `<span class="item-chip"><i data-lucide="link-2"></i> ${b.text}</span>`).join('');
    div.innerHTML = `
      <div class="item-head">
        <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
          <span class="item-phrase">${phrase}</span>
          <span class="type-pill">${item.media_type || 'text'}</span>
        </div>
        <div style="display:flex; gap:6px;">
          <button class="btn-outline" style="padding:6px 10px; font-size:11px;" onclick='editTrigger(${JSON.stringify(phrase)})'>
            <i data-lucide="pencil"></i>
          </button>
          <button class="btn-del" style="padding:6px 10px;" onclick='deleteTrigger(${JSON.stringify(phrase)})'>
            <i data-lucide="trash-2"></i>
          </button>
        </div>
      </div>
      <div class="item-text">${item.html_text || '<span style="color:var(--muted)">(только медиафайл)</span>'}</div>
      ${btns ? `<div class="item-chips">${btns}</div>` : ''}
    `;
    box.appendChild(div);
  });
  refreshIcons();
}

function editTrigger(phrase) {
  const item = currentDb[phrase];
  if (!item) return;
  document.getElementById('form_title').textContent = 'РЕДАКТИРОВАНИЕ';
  document.getElementById('phrase').value = phrase;
  document.getElementById('html_text').value = item.html_text || '';
  document.getElementById('existing_media_type').value = item.media_type || 'text';
  document.getElementById('existing_file_id').value = item.file_id || '';
  const box = document.getElementById('buttons_box');
  box.innerHTML = '';
  (item.buttons || []).forEach(row => {
    row.forEach((btn, idx) => addBtnRow(btn.text, btn.url, idx > 0));
  });
  updatePreview();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function resetForm() {
  document.getElementById('form_title').textContent = 'НОВАЯ ЗАГОТОВКА';
  document.getElementById('phrase').value = '';
  document.getElementById('html_text').value = '';
  document.getElementById('media_file').value = '';
  document.getElementById('existing_media_type').value = 'text';
  document.getElementById('existing_file_id').value = '';
  document.getElementById('buttons_box').innerHTML = '';
  updatePreview();
}

async function saveTrigger() {
  const phrase = document.getElementById('phrase').value.trim().toLowerCase();
  if (!phrase) {
    showToast('Введите фразу-триггер', true);
    return;
  }

  const btn = document.getElementById('save_btn');
  btn.disabled = true;

  const form = new FormData();
  form.append('phrase', phrase);
  form.append('html_text', document.getElementById('html_text').value);
  form.append('buttons', JSON.stringify(collectButtonRows()));
  form.append('existing_media_type', document.getElementById('existing_media_type').value);
  form.append('existing_file_id', document.getElementById('existing_file_id').value);

  const fileInput = document.getElementById('media_file');
  if (fileInput.files.length > 0) {
    form.append('file', fileInput.files[0]);
  }

  try {
    const res = await fetch('/api/triggers', {
      method: 'POST',
      headers: { 'X-Admin-Password': authPass },
      body: form
    });
    if (res.ok) {
      resetForm();
      await loadTriggers();
      showToast('Заготовка сохранена');
    } else {
      showToast('Ошибка: ' + await res.text(), true);
    }
  } finally {
    btn.disabled = false;
  }
}

async function deleteTrigger(phrase) {
  if (!confirm(`Удалить заготовку «${phrase}»?`)) return;
  const res = await fetch('/api/triggers?phrase=' + encodeURIComponent(phrase), {
    method: 'DELETE',
    headers: { 'X-Admin-Password': authPass }
  });
  if (res.ok) {
    await loadTriggers();
    showToast('Удалено');
  }
}

refreshIcons();
if (authPass) {
  loadTriggers().then(ok => {
    if (ok) document.getElementById('auth_overlay').style.display = 'none';
  });
}
</script>
</body>
</html>
"""


# --- РОУТЫ AIOHTTP ---


def check_auth(request: web.Request):
    pwd = request.headers.get("X-Admin-Password", "")
    if not pwd or pwd != WEB_PASSWORD:
        raise web.HTTPUnauthorized(text="Unauthorized")


async def handle_index(request: web.Request):
    return web.Response(text=WEB_HTML, content_type="text/html")


async def handle_ping(request: web.Request):
    return web.Response(text="OK")


async def handle_get_triggers(request: web.Request):
    check_auth(request)
    return web.json_response(await get_all_triggers())


async def handle_save_trigger(request: web.Request):
    check_auth(request)
    reader = await request.multipart()
    fields = {}
    file_bytes = None
    file_name = None
    content_type = ""

    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "file":
            file_name = part.filename
            content_type = part.headers.get("Content-Type", "")
            file_bytes = await part.read()
        else:
            fields[part.name] = await part.text()

    phrase = fields.get("phrase", "").strip().lower()
    if not phrase:
        return web.Response(status=400, text="Empty phrase")

    html_text = fields.get("html_text", "")
    buttons = json.loads(fields.get("buttons", "[]"))
    media_type = fields.get("existing_media_type", "text")
    file_id = fields.get("existing_file_id") or None

    if file_bytes and file_name:
        inp = BufferedInputFile(file_bytes, filename=file_name)
        if content_type.startswith("image/gif"):
            sent = await bot.send_animation(ADMIN_ID, inp)
            media_type, file_id = "animation", sent.animation.file_id
        elif content_type.startswith("image/"):
            sent = await bot.send_photo(ADMIN_ID, inp)
            media_type, file_id = "photo", sent.photo[-1].file_id
        elif content_type.startswith("video/"):
            sent = await bot.send_video(ADMIN_ID, inp)
            media_type, file_id = "video", sent.video.file_id
        else:
            sent = await bot.send_document(ADMIN_ID, inp)
            media_type, file_id = "document", sent.document.file_id
        try:
            await bot.delete_message(ADMIN_ID, sent.message_id)
        except Exception:
            pass

    await save_trigger_db(
        phrase,
        {
            "media_type": media_type,
            "file_id": file_id,
            "html_text": html_text,
            "buttons": buttons,
        },
    )
    return web.json_response({"ok": True})


async def handle_delete_trigger(request: web.Request):
    check_auth(request)
    phrase = request.query.get("phrase", "").strip().lower()
    if phrase:
        await delete_trigger_db(phrase)
    return web.json_response({"ok": True})


async def start_web_server():
    app = web.Application(client_max_size=50 * 1024 * 1024)
    app.router.add_get("/", handle_index)
    app.router.add_get("/ping", handle_ping)
    app.router.add_get("/api/triggers", handle_get_triggers)
    app.router.add_post("/api/triggers", handle_save_trigger)
    app.router.add_delete("/api/triggers", handle_delete_trigger)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_HOST, WEB_PORT)
    await site.start()
    print(f"Веб-панель запущена на порту {WEB_PORT}")


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await start_web_server()
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
