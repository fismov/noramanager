import asyncio
import html
import json
import logging
import os
import random
import re
import ssl
import time
from collections.abc import Callable
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from redis.asyncio import Redis

# --- НАСТРОЙКИ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ---
TOKEN = os.getenv("BOT_TOKEN", "9209163816:AAFMjQ4Q5NUWBatcPXjl6oQJyrqoshrHAVY").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "7831019966"))
REDIS_URL = os.getenv("REDIS_URL", "rediss://default:gQAAAAAAAR8zAAIgcDE5YTVjZjE0OTU3MzA0ZDFmYjY1MDBkNDRjMGViMmQ5YQ@in-lemur-73523.upstash.io:6379").strip()
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "noranoomi2121").strip()
GAME_ADMIN_TOKEN = os.getenv("GAME_ADMIN_TOKEN", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOjU5NiwiaXNfYWRtaW4iOnRydWUsImlhdCI6MTc5NTcyNTM4MiwiZXhwIjoxODAzNTAxMzgyLCJqdGkiOiI4ZWRiYzcwOTU1YjA4NjhmNGY3YTcxZmZhMWJhMThkMSJ9.TBcNiN5c5K8VMGdwD2TSfhgR4ipxH4OoJ_mmTH-Dp1o").strip()
WEB_HOST = "0.0.0.0"
WEB_PORT = int(os.getenv("PORT", "8080"))

STRANGER_REPLY = "Скачать игру можно тут:\n@noominora_bot"
DB_HASH_KEY = "bot:triggers"

# --- АВТООТВЕТЫ: ОТДЕЛЬНЫЕ КЛЮЧИ REDIS (старые данные bot:triggers не трогаем) ---
MEDIA_REPLIES_KEY = "bot:media_replies"
CHANNEL_REPLY_KEY = "bot:channel_reply"
CHANNEL_CHAT_CONFIGS_KEY = "bot:channel_reply:chats"
CHANNEL_CHAT_TITLES_KEY = "bot:channel_reply:chat_titles"
SETTINGS_KEY = "bot:settings"
STATS_KEY = "bot:stats"
LAST_REPLY_PREFIX = "bot:last_reply"

AUTO_MEDIA_TYPES = ("sticker", "photo", "video", "voice", "animation", "video_note", "audio")
AUTO_MEDIA_TITLES = {
    "sticker": "Стикеры",
    "photo": "Фото",
    "video": "Видео",
    "voice": "Голосовые (ГС)",
    "animation": "Гифки",
    "video_note": "Видео-кружки",
    "audio": "Аудио",
}

VALID_MATCH_TYPES = ("exact", "contains", "regex")
MATCH_TYPE_LABELS = {
    "exact": "Точное",
    "contains": "Содержит",
    "regex": "Regex",
}

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
    waiting_type = State()
    waiting_answer = State()


# --- РАБОТА С БАЗОЙ В REDIS ---

_regex_cache: dict[str, re.Pattern] = {}
REGEX_CACHE_LIMIT = 500


def clear_regex_cache() -> None:
    _regex_cache.clear()


def legacy_regex_pattern(phrase: str) -> str:
    """Безопасный regex для старых записей: ведёт себя как «Содержит»."""
    return re.escape(phrase)


def get_item_match_type(key: str, item: dict | None) -> str:
    match_type = (item or {}).get("match_type")
    return match_type if match_type in VALID_MATCH_TYPES else "regex"


def get_item_pattern(key: str, item: dict | None) -> str:
    if get_item_match_type(key, item) != "regex":
        return key
    return (item or {}).get("pattern") or legacy_regex_pattern(key)


def normalize_trigger(key: str, data: dict | None) -> dict:
    """Дополняет запись недостающими полями, не удаляя существующие данные."""
    item = dict(data or {})
    match_type = item.get("match_type")
    if match_type not in VALID_MATCH_TYPES:
        item["match_type"] = "regex"
        if not item.get("pattern"):
            item["pattern"] = legacy_regex_pattern(key)
    elif match_type == "regex" and not item.get("pattern"):
        item["pattern"] = key
    return item


def get_regex(pattern: str) -> re.Pattern:
    rx = _regex_cache.get(pattern)
    if rx is None:
        rx = re.compile(pattern, re.IGNORECASE | re.UNICODE)
        if len(_regex_cache) >= REGEX_CACHE_LIMIT:
            _regex_cache.clear()
        _regex_cache[pattern] = rx
    return rx


async def get_all_triggers() -> dict:
    raw = await redis_client.hgetall(DB_HASH_KEY)
    result = {}
    for k, v in raw.items():
        try:
            data = json.loads(v)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        result[k] = normalize_trigger(k, data)
    return result


async def get_trigger(phrase: str) -> dict | None:
    raw = await redis_client.hget(DB_HASH_KEY, phrase)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return normalize_trigger(phrase, data)


async def save_trigger_db(phrase: str, data: dict) -> None:
    clear_regex_cache()
    await redis_client.hset(DB_HASH_KEY, phrase, json.dumps(data, ensure_ascii=False))


async def delete_trigger_db(phrase: str) -> None:
    clear_regex_cache()
    await redis_client.hdel(DB_HASH_KEY, phrase)


async def count_triggers() -> int:
    return await redis_client.hlen(DB_HASH_KEY)


async def summarize_triggers(db: dict | None = None) -> dict:
    db = db if db is not None else await get_all_triggers()
    summary = {
        "total": len(db),
        "exact": 0,
        "contains": 0,
        "regex": 0,
        "media": 0,
        "buttons": 0,
    }
    for key, item in db.items():
        summary[get_item_match_type(key, item)] += 1
        if item.get("file_id") and (item.get("media_type") or "text") != "text":
            summary["media"] += 1
        summary["buttons"] += sum(len(row) for row in item.get("buttons", []) or [])
    return summary


async def migrate_legacy_triggers() -> int:
    """Старые записи переводим в Regex с экранированием, сохраняя все данные."""
    raw = await redis_client.hgetall(DB_HASH_KEY)
    migrated = 0
    for key, value in raw.items():
        try:
            data = json.loads(value)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue

        changed = False
        if data.get("match_type") not in VALID_MATCH_TYPES:
            data["match_type"] = "regex"
            changed = True
        if data["match_type"] == "regex" and not data.get("pattern"):
            data["pattern"] = legacy_regex_pattern(key)
            changed = True

        if changed:
            await redis_client.hset(
                DB_HASH_KEY, key, json.dumps(data, ensure_ascii=False)
            )
            migrated += 1

    if migrated:
        clear_regex_cache()
        print(
            f"Миграция: {migrated} старых записей переведены в Regex с экранированием"
        )
    return migrated


def find_matching_trigger(text: str, db: dict) -> tuple[str, dict] | None:
    """Ищет лучший триггер: точное совпадение > «Содержит» > Regex."""
    normalized = text.strip().casefold()
    best_key = None
    best_item = None
    best_weight = -1
    best_length = -1

    for key in sorted(db):
        item = db[key]
        match_type = get_item_match_type(key, item)
        matched = False
        weight = 1
        length = len(key)

        if match_type == "exact":
            matched = normalized == key.casefold()
            weight = 3
        elif match_type == "contains":
            matched = key.casefold() in normalized
            weight = 2
        else:
            pattern = get_item_pattern(key, item)
            length = len(pattern)
            try:
                matched = get_regex(pattern).search(text) is not None
            except re.error:
                matched = False

        if not matched:
            continue

        if weight > best_weight or (weight == best_weight and length > best_length):
            best_key = key
            best_item = item
            best_weight = weight
            best_length = length

    if best_key is None:
        return None
    return best_key, best_item


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
    if message.audio:
        return "audio", message.audio.file_id
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
    elif m_type == "audio" and file_id:
        await bot.send_audio(chat_id, file_id, caption=text or None, reply_markup=kb, **kwargs)
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
    summary = await summarize_triggers()
    render_url = os.getenv("RENDER_EXTERNAL_URL", "")
    web_line = f"Веб-панель: <code>{render_url}</code>\n" if render_url else ""
    return (
        "<b>Панель управления заготовками</b>\n\n"
        f"Активных фраз в базе: <b>{summary['total']}</b>\n"
        f"Точные: <b>{summary['exact']}</b> · Содержит: <b>{summary['contains']}</b> · Regex: <b>{summary['regex']}</b>\n"
        f"{web_line}"
        "Хранилище: <b>Upstash Redis</b>\n"
        "Реакции на медиа и посты канала настраиваются в веб-панели.\n\n"
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


# --- ПРОФИЛИ ИГРОКОВ NOOMINORA: «АККАУНТ» И «ЧЕКНУТЬ <ID>» ---
# Команды работают и в ЛС, и в группах для любых пользователей бота.
# Обработчики зарегистрированы ДО catch-all (private_stranger / group_auto_reply),
# поэтому имеют приоритет над автоответами.

GAMESERVER_API = os.getenv("GAMESERVER_API", "https://85.192.20.22:13882").rstrip("/")
GAME_API_SECRET = os.getenv(
    "GAME_API_SECRET", "ZYs8n0qHGIm24VpFeYfdGu7uuEcOXslqDHca3vxVBs8"
).strip()
TG_LINK_TTL = 60 * 60 * 24 * 30  # кэш связки account_id -> tg_id, 30 дней
PREMIUM_ADMIN_IDS = frozenset({7831019966, 6793480366})
GAME_ADMIN_TLS_SHA256 = os.getenv(
    "GAME_ADMIN_TLS_SHA256",
    "7816f62148e353a6c1a631bff20ec31817b0fc4f73bf656e02fe3393c6c059f9"
    if GAMESERVER_API == "https://85.192.20.22:13882" else "",
).strip()

try:
    from profile_card import render_error_card, render_premium_card, render_profile_card

    PROFILE_RENDER_OK = True
except Exception as _profile_import_err:  # Pillow или шрифт недоступны
    PROFILE_RENDER_OK = False
    print(f"Карточки профилей недоступны: {_profile_import_err}")

_game_ssl = ssl.create_default_context()
_game_ssl.set_alpn_protocols(["http/1.1"])
_game_ssl.check_hostname = False
_game_ssl.verify_mode = ssl.CERT_NONE  # у игрового сервера самоподписанный сертификат
_game_session: aiohttp.ClientSession | None = None

_profile_cooldown: dict[int, float] = {}

_PROFILE_OWN_CMDS = {"аккаунт", "/аккаунт", "/account", "!аккаунт", ".аккаунт"}
_CHECK_RE = re.compile(r"^[/!.]?чекнуть(?:\s+(\d{1,12}))?\s*$", re.IGNORECASE)
_CHECK_EN_RE = re.compile(r"^[/!.]?check(?:\s+(\d{1,12}))?\s*$", re.IGNORECASE)


def _game_http() -> aiohttp.ClientSession:
    global _game_session
    if _game_session is None or _game_session.closed:
        _game_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
            connector=aiohttp.TCPConnector(ssl=_game_ssl),
        )
    return _game_session


async def game_api_get(path: str, bot_secret: bool = False):
    """GET к игровому API. Возвращает (status, json|None). Ошибки сети — (0, None)."""
    headers = {"X-Bot-Secret": GAME_API_SECRET} if bot_secret else {}
    timeout = aiohttp.ClientTimeout(total=11, connect=5, sock_connect=5, sock_read=8)
    for attempt in range(2):
        try:
            async with _game_http().get(
                f"{GAMESERVER_API}{path}", headers=headers, timeout=timeout, allow_redirects=False
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except ValueError:
                    data = None
                return resp.status, data
        except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, asyncio.TimeoutError) as exc:
            if attempt == 0:
                continue
            logging.warning("Game API GET %s failed after two attempts: %s",
                            path.split("?", 1)[0], type(exc).__name__)
        except aiohttp.ClientError as exc:
            logging.warning("Game API GET %s failed: %s", path.split("?", 1)[0], type(exc).__name__)
        return 0, None


async def _set_game_premium(account_id: int, enable: bool) -> int:
    """Возвращает HTTP-статус админского API или 0, если сервер не ответил."""
    try:
        if GAME_ADMIN_TLS_SHA256:
            tls = aiohttp.Fingerprint(bytes.fromhex(GAME_ADMIN_TLS_SHA256))
        else:
            tls = ssl.create_default_context()
            tls.set_alpn_protocols(["http/1.1"])
    except ValueError:
        logging.warning("Некорректный отпечаток сертификата игрового API")
        return 0

    timeout = aiohttp.ClientTimeout(total=20, connect=6, sock_connect=6, sock_read=12)
    for attempt in range(2):
        try:
            async with _game_http().post(
                f"{GAMESERVER_API}/admin/toggle_prem",
                json={"account_id": account_id, "enable": enable},
                headers={"Authorization": f"Bearer {GAME_ADMIN_TOKEN}"},
                ssl=tls,
                timeout=timeout,
                allow_redirects=False,
            ) as response:
                return response.status
        except aiohttp.ConnectionTimeoutError:
            if attempt == 0:
                # На этом этапе POST ещё не был отправлен.
                continue
            logging.warning("Не удалось подключиться к игровому API для ID %s", account_id)
        except asyncio.TimeoutError:
            logging.warning("Игровой API не подтвердил изменение премиума для ID %s вовремя", account_id)
        except aiohttp.ClientError as exc:
            logging.warning("Ошибка игрового API для ID %s: %s", account_id, type(exc).__name__)
        return 0


async def _avatar_bytes(tg_id: int) -> bytes | None:
    """Аватарка пользователя Telegram (самый большой размер)."""
    try:
        photos = await bot.get_user_profile_photos(tg_id, limit=1)
        if not photos.photos or not photos.photos[0]:
            return None
        src = await bot.download(photos.photos[0][-1].file_id)
        if src is None:
            return None
        return src.read()
    except Exception as e:
        print(f"Не удалось получить аватар {tg_id}: {e}")
        return None


async def _avatar_for_account(account_id: int) -> bytes | None:
    """Аватар чужого профиля — из кэша связок account_id -> tg_id."""
    try:
        cached = await redis_client.get(f"bot:tglink:{account_id}")
    except Exception:
        cached = None
    if cached:
        return await _avatar_bytes(int(cached))
    return None


def _profile_cooldown_ok(user_id: int) -> bool:
    now = time.time()
    if now - _profile_cooldown.get(user_id, 0) < 4:
        return False
    _profile_cooldown[user_id] = now
    if len(_profile_cooldown) > 5000:
        _profile_cooldown.clear()
    return True


def _parse_profile_command(message: Message):
    """('own', None) | ('check', id) | None, если это не команда."""
    text = (message.text or "").strip()
    if not text or len(text) > 64:
        return None
    low = text.casefold()
    if low in _PROFILE_OWN_CMDS:
        return ("own", None)
    m = _CHECK_RE.match(low) or _CHECK_EN_RE.match(low)
    if m:
        return ("check", int(m.group(1)) if m.group(1) else None)
    return None


async def _is_profile_command(message: Message):
    if not message.from_user or message.from_user.is_bot:
        return False
    parsed = _parse_profile_command(message)
    return {"profile_command": parsed} if parsed else False


async def _send_card_png(
    message: Message, png: bytes, loading: Message | None = None, *, filename: str = "profile.png"
):
    photo = BufferedInputFile(png, filename=filename)
    if loading is not None:
        try:
            await loading.edit_media(InputMediaPhoto(media=photo))
            return
        except TelegramBadRequest:
            # Если Telegram отклонил замену текста на фото, отправляем карточку отдельно.
            pass

    if message.chat.type in ("group", "supergroup"):
        await message.reply_photo(photo)
    else:
        await message.answer_photo(photo)

    if loading is not None:
        try:
            await loading.delete()
        except TelegramBadRequest:
            pass  # Сообщение уже могло быть удалено.


_PROFILE_RENDER_FALLBACK = "Карточки профилей временно недоступны, попробуй позже."


async def _render_profile_error(title: str, subtitle: str, tag: str) -> bytes:
    return await asyncio.to_thread(render_error_card, title, subtitle, tag)


async def profile_flow_linked(tg_id: int, *, own: bool) -> bytes:
    """Профиль, привязанный к Telegram автора команды или ответа."""
    tag = "АККАУНТ" if own else "ЧЕКНУТЬ"

    status, acc = await game_api_get(f"/bot/account?tg_id={tg_id}", bot_secret=True)
    if status == 404:
        return await _render_profile_error(
            "АККАУНТ НЕ ПРИВЯЗАН",
            (
                "Привяжи игру в боте @noominora_bot и попробуй снова"
                if own else "У этого пользователя нет привязанного игрового аккаунта"
            ),
            tag,
        )
    if status != 200 or not isinstance(acc, dict) or not acc.get("account_id"):
        return await _render_profile_error("СЕРВЕР ИГРЫ НЕ ОТВЕЧАЕТ", "Попробуй чуть позже", tag)

    account_id = int(acc["account_id"])
    try:
        await redis_client.set(f"bot:tglink:{account_id}", str(tg_id), ex=TG_LINK_TTL)
    except Exception:
        pass

    if acc.get("banned"):
        title = "АККАУНТ ЗАБАНЕН" if own else "ПРОФИЛЬ НЕДОСТУПЕН"
        return await _render_profile_error(title, "", tag)

    status2, player = await game_api_get(f"/players/{account_id}")
    if status2 == 404:
        return await _render_profile_error("ИГРОК НЕ НАЙДЕН", "Аккаунт удалён из игры", tag)
    if status2 == 403:
        return await _render_profile_error(
            "ПРОФИЛЬ СКРЫТ",
            (
                "Открой профиль в настройках игры и попробуй снова"
                if own else "Владелец скрыл этот профиль настройками приватности"
            ),
            tag,
        )
    if status2 != 200 or not isinstance(player, dict):
        return await _render_profile_error("СЕРВЕР ИГРЫ НЕ ОТВЕЧАЕТ", "Попробуй чуть позже", tag)

    avatar = await _avatar_bytes(tg_id)
    return await asyncio.to_thread(render_profile_card, player, avatar)


async def profile_flow_check(target_id: int) -> bytes:
    """«Чекнуть <id>» — чужой профиль по ID в игре."""
    tag = f"ЧЕКНУТЬ {target_id}"

    status, player = await game_api_get(f"/players/{target_id}")
    if status == 404:
        return await _render_profile_error(
            "ИГРОК НЕ НАЙДЕН", f"Проверь ID: чекнуть {target_id}", tag
        )
    if status == 403:
        return await _render_profile_error(
            "ПРОФИЛЬ ЗАКРЫТ", "Владелец скрыл этот профиль настройками приватности", tag
        )
    if status != 200 or not isinstance(player, dict):
        return await _render_profile_error("СЕРВЕР ИГРЫ НЕ ОТВЕЧАЕТ", "Попробуй чуть позже", tag)

    avatar = await _avatar_for_account(target_id)
    return await asyncio.to_thread(render_profile_card, player, avatar)


@router.message(
    F.chat.type.in_({"private", "group", "supergroup"}),
    F.text,
    StateFilter(None),  # не мешаем FSM-меню (шагам добавления триггеров)
    _is_profile_command,
)
async def profile_commands(message: Message, profile_command: tuple[str, int | None]):
    if not _profile_cooldown_ok(message.from_user.id):
        return

    kind, target = profile_command
    own = kind == "own"
    telegram_user = message.from_user if own else None
    replied = message.reply_to_message
    if kind == "check" and target is None and replied and replied.sender_chat is None:
        if replied.from_user and not replied.from_user.is_bot:
            telegram_user = replied.from_user

    if telegram_user is None and target is None:
        if not PROFILE_RENDER_OK:
            await message.answer(_PROFILE_RENDER_FALLBACK)
            return
        png = await _render_profile_error(
            "УКАЖИ ID ИГРОКА",
            "Ответь «Чекнуть» на сообщение игрока или отправь «Чекнуть 90000».",
            "ЧЕКНУТЬ",
        )
        await _send_card_png(message, png)
        return

    send_status = message.answer if message.chat.type == "private" else message.reply
    loading = await send_status("◈ <b>Загружаю профиль</b>")
    try:
        if PROFILE_RENDER_OK:
            if telegram_user is not None:
                png = await profile_flow_linked(telegram_user.id, own=own)
            else:
                png = await profile_flow_check(target)
            await _send_card_png(message, png, loading)
            return
    except Exception:
        logging.exception("Не удалось показать карточку профиля")
    await loading.edit_text(_PROFILE_RENDER_FALLBACK)


_PREMIUM_RE = re.compile(
    r"[/!.]?(выдать|забрать)\s+прем(?:иум)?(?:\s+(.+))?", re.IGNORECASE | re.DOTALL
)


async def _is_premium_command(message: Message):
    match = _PREMIUM_RE.fullmatch((message.text or "").strip())
    if match is None:
        return False
    action, account_id = match.groups()
    return {"enable": action.casefold() == "выдать", "account_id_text": (account_id or "").strip()}


@router.message(
    F.chat.type.in_({"private", "group", "supergroup"}),
    F.text,
    StateFilter(None),
    _is_premium_command,
)
async def premium_command(message: Message, enable: bool, account_id_text: str):
    user = message.from_user
    if (
        user is None or user.is_bot or message.sender_chat is not None
        or user.id not in PREMIUM_ADMIN_IDS
    ):
        await message.reply("× <b>Управление премиумом доступно администраторам.</b>")
        return

    if not re.fullmatch(r"[0-9]{1,12}", account_id_text) or int(account_id_text) == 0:
        example = "Выдать прем 19" if enable else "Забрать прем 19"
        await message.reply(f"◈ <b>Укажи игровой ID</b>\nПример <code>{example}</code>")
        return

    if not GAME_ADMIN_TOKEN or not GAMESERVER_API.startswith("https://"):
        await message.reply("! <b>Управление премиумом пока не настроено.</b>")
        return

    account_id = int(account_id_text)
    action = "⊕ Выдаю премиум" if enable else "⊖ Снимаю премиум"
    detail = f"Игровой ID <code>#{account_id}</code>"
    progress = await message.reply(f"<b>{action}</b>\n{detail}")

    status = await _set_game_premium(account_id, enable)
    if status == 200:
        title = "Премиум выдан" if enable else "Премиум снят"
    elif status == 404:
        title = "Аккаунт не найден"
    elif status in (401, 403):
        title = "Доступ отклонён"
    else:
        title = "Результат не подтверждён"

    if PROFILE_RENDER_OK:
        try:
            png = await asyncio.to_thread(render_premium_card, title, account_id, enable, status == 200)
            await _send_card_png(message, png, progress, filename="premium.png")
            return
        except Exception:
            logging.exception("Не удалось показать карточку премиума для ID %s", account_id)
    await progress.edit_text(f"<b>{title}</b>\n{detail}")


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
        "<b>Шаг 1 из 3 — Фраза-триггер</b>\n\n"
        "Отправь слово или фразу, на которую бот должен отвечать в чате.\n"
        "<i>Для «Точного» и «Содержит» регистр букв не имеет значения.</i>"
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

    phrase = message.text.strip()
    await state.update_data(phrase=phrase)
    await state.set_state(MenuState.waiting_type)

    text = (
        "<b>Шаг 2 из 3 — Тип совпадения</b>\n\n"
        f"Фраза: <code>{html.escape(phrase)}</code>\n\n"
        "Выбери, как бот должен находить эту фразу в сообщении:"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎯 Точное совпадение", callback_data="addtype:exact")],
            [InlineKeyboardButton(text="🧩 Содержит в тексте", callback_data="addtype:contains")],
            [InlineKeyboardButton(text="⚙️ Regex-шаблон", callback_data="addtype:regex")],
            [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="menu:home")],
        ]
    )
    new_id = await safe_edit_menu(message.chat.id, menu_msg_id, text, kb)
    await state.update_data(menu_msg_id=new_id)


@router.callback_query(F.from_user.id == ADMIN_ID, F.data.startswith("addtype:"))
async def cb_add_type(call: CallbackQuery, state: FSMContext):
    if await state.get_state() != MenuState.waiting_type.state:
        await call.answer("Сессия устарела, начните заново", show_alert=True)
        return

    match_type = call.data.split(":", 1)[1]
    if match_type not in VALID_MATCH_TYPES:
        await call.answer("Неизвестный тип", show_alert=True)
        return

    data = await state.get_data()
    phrase = (data.get("phrase") or "").strip()
    menu_msg_id = data.get("menu_msg_id") or call.message.message_id

    if match_type in ("exact", "contains"):
        phrase = phrase.lower()

    if match_type == "regex":
        try:
            re.compile(phrase, re.IGNORECASE)
        except re.error as e:
            await call.answer(f"Ошибка в regex: {e}", show_alert=True)
            return

    await state.update_data(phrase=phrase, match_type=match_type)
    await state.set_state(MenuState.waiting_answer)

    type_label = MATCH_TYPE_LABELS[match_type]
    if match_type == "exact":
        hint = "Бот ответит, если сообщение <b>полностью совпадёт</b> с фразой."
    elif match_type == "contains":
        hint = "Бот ответит, если фраза <b>встретится внутри</b> сообщения."
    else:
        hint = "Поиск по регулярному выражению <b>без учёта регистра</b>."

    text = (
        f"<b>Шаг 3 из 3 — Ответ на «{html.escape(phrase)}»</b>\n"
        f"Тип: <b>{type_label}</b>\n"
        f"{hint}\n\n"
        "Отправь готовое сообщение: текст (жирный, курсив, ссылки сохранятся), фото, видео, гифку или пост с кнопками.\n\n"
        "Чтобы добавить свои кнопки, допиши в конце:\n"
        "<code>[Скачать - @noominora_bot]</code>\n"
        "В один ряд через черту: <code>[Кнопка 1 - https://ya.ru] | [Кнопка 2 - @durov]</code>"
    )
    new_id = await safe_edit_menu(call.message.chat.id, menu_msg_id, text, back_menu_kb())
    await state.update_data(menu_msg_id=new_id)
    await call.answer()


@router.message(F.chat.type == "private", F.from_user.id == ADMIN_ID, MenuState.waiting_answer)
async def step_answer(message: Message, state: FSMContext):
    data = await state.get_data()
    phrase = data["phrase"]
    match_type = data.get("match_type", "exact")
    menu_msg_id = data.get("menu_msg_id")

    raw_html = message.html_text if message.text else (message.html_caption if message.caption else "")
    clean_html, parsed_rows = extract_buttons_from_html(raw_html)
    existing_rows = serialize_existing_kb(message.reply_markup)
    final_rows = parsed_rows or existing_rows
    media_type, file_id = extract_media_info(message)

    existing = await get_trigger(phrase) or {}
    item = dict(existing)
    item.update(
        {
            "media_type": media_type,
            "file_id": file_id,
            "html_text": clean_html,
            "buttons": final_rows,
            "match_type": match_type,
        }
    )
    if match_type == "regex":
        item["pattern"] = phrase
    else:
        item.pop("pattern", None)

    await save_trigger_db(phrase, item)

    try:
        await message.delete()
    except Exception:
        pass

    await state.clear()
    status_text = (
        f"✅ Заготовка для <b>«{html.escape(phrase)}»</b> сохранена!\n"
        f"Совпадение: <b>{MATCH_TYPE_LABELS.get(match_type, match_type)}</b> | Медиа: <b>{media_type}</b> | Кнопок: <b>{sum(len(r) for r in final_rows)}</b>\n\n"
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
        match_type = get_item_match_type(phrase, info)
        type_short = {"exact": "Т", "contains": "С", "regex": "R"}[match_type]
        label = f"{type_short} · {phrase} [{info.get('media_type', 'text')}, кнопок: {btn_count}]"
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


async def show_trigger_card(chat_id: int, message_id: int, idx: int) -> bool:
    db = await get_all_triggers()
    keys = sorted(list(db.keys()))
    if idx < 0 or idx >= len(keys):
        return False

    phrase = keys[idx]
    info = db[phrase]
    match_type = get_item_match_type(phrase, info)
    preview_text = info.get("html_text") or "<i>(без текста, только медиа)</i>"
    if len(preview_text) > 500:
        preview_text = preview_text[:500] + "..."

    pattern_line = ""
    if match_type == "regex":
        pattern = get_item_pattern(phrase, info)
        if pattern not in (phrase, legacy_regex_pattern(phrase)):
            pattern_line = f"<b>Шаблон:</b> <code>{html.escape(pattern)}</code>\n"

    text = (
        f"<b>Триггер:</b> <code>{html.escape(phrase)}</code>\n"
        f"<b>Совпадение:</b> <b>{MATCH_TYPE_LABELS[match_type]}</b>\n"
        f"{pattern_line}"
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
            [
                InlineKeyboardButton(
                    text=f"🔁 Тип: {MATCH_TYPE_LABELS[match_type]}",
                    callback_data=f"menu:type:{idx}",
                )
            ],
            [InlineKeyboardButton(text="⬅️ К списку", callback_data="menu:list:0")],
        ]
    )
    await safe_edit_menu(chat_id, message_id, text, kb)
    return True


@router.callback_query(F.from_user.id == ADMIN_ID, F.data.startswith("menu:item:"))
async def cb_item(call: CallbackQuery):
    idx = int(call.data.split(":")[2])
    if not await show_trigger_card(call.message.chat.id, call.message.message_id, idx):
        await call.answer("Фраза не найдена", show_alert=True)
        return
    await call.answer()


@router.callback_query(F.from_user.id == ADMIN_ID, F.data.startswith("menu:type:"))
async def cb_cycle_type(call: CallbackQuery):
    idx = int(call.data.split(":")[2])
    db = await get_all_triggers()
    keys = sorted(list(db.keys()))
    if idx < 0 or idx >= len(keys):
        await call.answer("Фраза не найдена", show_alert=True)
        return

    phrase = keys[idx]
    item = dict(db[phrase])
    current = get_item_match_type(phrase, item)
    order = ["exact", "contains", "regex"]
    new_type = order[(order.index(current) + 1) % len(order)]

    item["match_type"] = new_type
    if new_type == "regex":
        item["pattern"] = item.get("pattern") or legacy_regex_pattern(phrase)

    await save_trigger_db(phrase, item)
    await show_trigger_card(call.message.chat.id, call.message.message_id, idx)
    await call.answer(f"Тип изменён: {MATCH_TYPE_LABELS[new_type]}")


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


# --- АВТООТВЕТЫ: МЕДИА-РЕАКЦИИ И ПОСТЫ КАНАЛА ---


def normalize_auto_answer(raw: object) -> dict:
    """Приводит вариант автоответа к единому виду, ничего не теряя."""
    if not isinstance(raw, dict):
        return {"media_type": "text", "file_id": None, "html_text": "", "buttons": []}
    buttons = clean_buttons(raw.get("buttons") or [])
    media_type = str(raw.get("media_type") or "text")
    file_id = raw.get("file_id") or None
    if media_type != "text" and not file_id:
        media_type = "text"
    return {
        "media_type": media_type,
        "file_id": file_id,
        "html_text": str(raw.get("html_text") or ""),
        "buttons": buttons,
    }


def _parse_chance(value: object, default: int) -> int:
    try:
        chance = int(value)
    except (TypeError, ValueError):
        return default
    return min(100, max(0, chance))


def normalize_media_config(raw: object) -> dict:
    if not isinstance(raw, dict):
        raw = {}
    answers_raw = raw.get("answers")
    answers = (
        [normalize_auto_answer(a) for a in answers_raw]
        if isinstance(answers_raw, list)
        else []
    )
    return {
        "enabled": bool(raw.get("enabled", True)),
        "chance": _parse_chance(raw.get("chance", 50), 50),
        "answers": answers,
    }


def default_media_config() -> dict:
    return {"enabled": True, "chance": 50, "answers": []}


async def get_media_config(media_type: str) -> dict:
    raw = await redis_client.hget(MEDIA_REPLIES_KEY, media_type)
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return normalize_media_config(data)
        except Exception:
            pass
    return default_media_config()


async def save_media_config(media_type: str, config: dict) -> None:
    await redis_client.hset(
        MEDIA_REPLIES_KEY,
        media_type,
        json.dumps(normalize_media_config(config), ensure_ascii=False),
    )


async def get_all_media_configs() -> dict:
    raw = await redis_client.hgetall(MEDIA_REPLIES_KEY)
    result = {}
    for media_type in AUTO_MEDIA_TYPES:
        value = raw.get(media_type)
        config = None
        if value:
            try:
                data = json.loads(value)
                if isinstance(data, dict):
                    config = normalize_media_config(data)
            except Exception:
                config = None
        result[media_type] = config or default_media_config()
    return result


def normalize_channel_config(raw: object) -> dict:
    if not isinstance(raw, dict):
        raw = {}
    answers_raw = raw.get("answers")
    answers = (
        [normalize_auto_answer(a) for a in answers_raw]
        if isinstance(answers_raw, list)
        else []
    )
    return {
        "enabled": bool(raw.get("enabled", True)),
        "chance": _parse_chance(raw.get("chance", 100), 100),
        "in_groups": bool(raw.get("in_groups", True)),
        "in_channels": bool(raw.get("in_channels", False)),
        "answers": answers,
    }


def decode_channel_config(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return normalize_channel_config(data) if isinstance(data, dict) else None


async def get_channel_config(chat_id: int | str | None = None) -> dict:
    if chat_id is not None:
        raw = await redis_client.hget(CHANNEL_CHAT_CONFIGS_KEY, str(chat_id))
        override = decode_channel_config(raw)
        if override is not None:
            return override
    return decode_channel_config(await redis_client.get(CHANNEL_REPLY_KEY)) or normalize_channel_config(None)


async def update_channel_config(chat_id: str | None, update: Callable[[dict], None]) -> dict:
    async def apply(pipe):
        config = None
        if chat_id is not None:
            config = decode_channel_config(await pipe.hget(CHANNEL_CHAT_CONFIGS_KEY, chat_id))
        if config is None:
            config = decode_channel_config(await pipe.get(CHANNEL_REPLY_KEY)) or normalize_channel_config(None)

        update(config)
        config = normalize_channel_config(config)
        raw = json.dumps(config, ensure_ascii=False)
        pipe.multi()
        if chat_id is None:
            pipe.set(CHANNEL_REPLY_KEY, raw)
        else:
            pipe.hset(CHANNEL_CHAT_CONFIGS_KEY, chat_id, raw)
        return config

    keys = (CHANNEL_REPLY_KEY,) if chat_id is None else (CHANNEL_REPLY_KEY, CHANNEL_CHAT_CONFIGS_KEY)
    return await redis_client.transaction(apply, *keys, value_from_callable=True)


async def remember_channel_chat(message: Message):
    title = message.chat.title
    if title:
        await redis_client.hset(CHANNEL_CHAT_TITLES_KEY, str(message.chat.id), title)


def normalize_settings(raw: object) -> dict:
    if not isinstance(raw, dict):
        raw = {}

    def clamp(value: object, default: int, lo: int, hi: int) -> int:
        try:
            value = int(value)
        except (TypeError, ValueError):
            return default
        return min(hi, max(lo, value))

    return {
        "ignore_bots": bool(raw.get("ignore_bots", True)),
        "cooldown": clamp(raw.get("cooldown", 0), 0, 0, 3600),
        "delay_min": clamp(raw.get("delay_min", 0), 0, 0, 120),
        "delay_max": clamp(raw.get("delay_max", 0), 0, 0, 120),
    }


async def get_settings() -> dict:
    raw = await redis_client.get(SETTINGS_KEY)
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return normalize_settings(data)
        except Exception:
            pass
    return normalize_settings(None)


async def save_settings_db(config: dict) -> None:
    await redis_client.set(
        SETTINGS_KEY,
        json.dumps(normalize_settings(config), ensure_ascii=False),
    )


async def bump_stat(kind: str) -> None:
    try:
        await redis_client.hincrby(STATS_KEY, kind, 1)
    except Exception:
        pass


async def get_stats() -> dict:
    raw = await redis_client.hgetall(STATS_KEY)

    def num(key: str) -> int:
        try:
            return int(raw.get(key, 0))
        except (TypeError, ValueError):
            return 0

    return {"triggers": num("triggers"), "media": num("media"), "channel": num("channel")}


async def reset_stats_db() -> None:
    await redis_client.delete(STATS_KEY)


async def cooldown_ok(chat_id: int, cooldown_seconds: int) -> bool:
    """Не даём боту отвечать слишком часто в одном чате."""
    if cooldown_seconds <= 0:
        return True
    key = f"{LAST_REPLY_PREFIX}:{chat_id}"
    last = await redis_client.get(key)
    if last:
        try:
            if time.time() - float(last) < cooldown_seconds:
                return False
        except (TypeError, ValueError):
            pass
    await redis_client.set(key, str(time.time()), ex=max(cooldown_seconds * 2, 60))
    return True


def is_channel_post(message: Message) -> bool:
    """True, если сообщение опубликовано от имени канала (пост ТГК в группе)."""
    sender = message.sender_chat
    if sender is None:
        return False
    if getattr(message, "is_automatic_forward", None) or getattr(message, "automatic_forward", None):
        return True
    return sender.type == "channel"


def pick_random_answer(config: dict) -> dict | None:
    answers = [
        a
        for a in (config.get("answers") or [])
        if a.get("file_id") or (a.get("html_text") or "").strip() or a.get("buttons")
    ]
    if not answers:
        return None
    return random.choice(answers)


def roll_chance(chance: int) -> bool:
    return random.randint(1, 100) <= chance


async def maybe_media_reply(message: Message, settings: dict):
    """Случайная реакция на стикер, фото, видео, ГС и другой медиа."""
    media_type, _ = extract_media_info(message)
    if media_type == "text":
        return
    config = await get_media_config(media_type)
    if not config["enabled"] or not config["answers"]:
        return
    if not roll_chance(config["chance"]):
        return
    if not await cooldown_ok(message.chat.id, settings["cooldown"]):
        return
    answer = pick_random_answer(config)
    if not answer:
        return
    low = min(settings["delay_min"], settings["delay_max"])
    high = max(settings["delay_min"], settings["delay_max"])
    if high > 0:
        await asyncio.sleep(random.uniform(low, high))
    try:
        await send_trigger_reply(message.chat.id, answer, reply_to_message_id=message.message_id)
        await bump_stat("media")
    except Exception as e:
        print(f"Ошибка медиа-реакции: {e}")


async def handle_channel_post(message: Message, settings: dict | None = None):
    """Ответ на пост от Telegram-канала: в группе или в самом канале."""
    settings = settings or await get_settings()
    config = await get_channel_config(message.chat.id)
    if not config["enabled"] or not config["answers"]:
        return
    if message.chat.type in ("group", "supergroup"):
        if not config["in_groups"]:
            return
    elif message.chat.type == "channel":
        if not config["in_channels"]:
            return
    else:
        return
    if not roll_chance(config["chance"]):
        return
    if not await cooldown_ok(message.chat.id, settings["cooldown"]):
        return
    answer = pick_random_answer(config)
    if not answer:
        return
    try:
        await send_trigger_reply(message.chat.id, answer, reply_to_message_id=message.message_id)
        await bump_stat("channel")
    except Exception as e:
        print(f"Ошибка ответа на пост канала: {e}")


@router.channel_post()
async def channel_chat_post(message: Message):
    """Посты в самом канале (если бот — админ канала)."""
    await remember_channel_chat(message)
    await handle_channel_post(message)


# --- ОТВЕТЫ В ГРУППАХ ---


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def group_auto_reply(message: Message):
    settings = await get_settings()

    # Пост от Telegram-канала: триггеры приоритетнее, иначе — автоответ канала
    if is_channel_post(message):
        await remember_channel_chat(message)
        incoming = message.text or message.caption
        if incoming:
            db = await get_all_triggers()
            found = find_matching_trigger(incoming, db)
            if found:
                _, item = found
                try:
                    await send_trigger_reply(
                        message.chat.id, item, reply_to_message_id=message.message_id
                    )
                    await bump_stat("triggers")
                except Exception as e:
                    print(f"Ошибка при отправке ответа в чат: {e}")
                return
        await handle_channel_post(message, settings)
        return

    # Телеграм может использовать системного отправителя у постов канала.
    if settings["ignore_bots"] and message.from_user and message.from_user.is_bot:
        return

    incoming = message.text or message.caption
    if incoming:
        db = await get_all_triggers()
        found = find_matching_trigger(incoming, db)
        if found:
            _, item = found
            try:
                await send_trigger_reply(
                    message.chat.id, item, reply_to_message_id=message.message_id
                )
                await bump_stat("triggers")
            except Exception as e:
                print(f"Ошибка при отправке ответа в чат: {e}")
            return

    # Триггер не сработал — пробуем случайную реакцию на медиа
    await maybe_media_reply(message, settings)


# --- ВЕБ-ИНТЕРФЕЙС (МИНИМАЛИЗМ: ЧЁРНЫЙ ФОН, БЕЛЫЕ КНОПКИ, UNBOUNDED, LUCIDE ICONS) ---

WEB_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TRIGGER STUDIO — панель</title>
<meta name="theme-color" content="#050505">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='10' fill='%23050505'/%3E%3Cpath d='M9 12h14M9 16h9M9 20h12' stroke='%23f5f3ee' stroke-width='2' stroke-linecap='round'/%3E%3Ccircle cx='24' cy='24' r='3' fill='%239ae6a4'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Unbounded:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #050505;
    --ink: #f5f3ee;
    --ink-soft: #d2d0c9;
    --muted: #8b8981;
    --dim: #5f5d57;
    --line: rgba(255, 255, 255, 0.085);
    --line-strong: rgba(255, 255, 255, 0.18);
    --glass: rgba(255, 255, 255, 0.036);
    --glass-2: rgba(255, 255, 255, 0.065);
    --green: #9ae6a4;
    --violet: #c3c7ff;
    --danger: #ff9b9f;
    --font-display: 'Unbounded', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    --font-mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
    --radius: 22px;
  }

  * { box-sizing: border-box; }

  [hidden] { display: none !important; }

  html { scroll-behavior: smooth; }

  body {
    margin: 0;
    min-height: 100vh;
    background: var(--bg);
    color: var(--ink);
    font-family: var(--font-display);
    font-size: 12px;
    letter-spacing: -0.01em;
    -webkit-font-smoothing: antialiased;
    overflow-x: hidden;
  }

  body.locked { overflow: hidden; }

  ::selection { background: var(--ink); color: #050505; }

  button, input, textarea, select { font: inherit; }
  .channel-picker select {
    width: 100%;
    min-width: 0;
    padding: 9px 12px;
    color: inherit;
    background: #131313;
    border: 1px solid rgba(255, 255, 255, .12);
    border-radius: 10px;
    color-scheme: dark;
  }
  .channel-picker summary { cursor: pointer; font-size: 12px; }
  .channel-picker__form {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 8px;
    margin-top: 8px;
  }
  .channel-picker [hidden] { display: none; }

  button { color: inherit; }

  a { color: inherit; }

  code, .mono { font-family: var(--font-mono); }

  .muted { color: var(--muted); }

  .liquid {
    position: fixed;
    inset: 0;
    z-index: 0;
    overflow: hidden;
    pointer-events: none;
  }

  .liquid__blob {
    position: absolute;
    width: 56vw;
    height: 56vw;
    border-radius: 50%;
    filter: blur(110px);
    opacity: 0.13;
  }

  .liquid__blob--1 {
    top: -24%;
    left: -14%;
    background: radial-gradient(circle, #bff7c8, transparent 64%);
  }

  .liquid__blob--2 {
    right: -18%;
    bottom: -30%;
    background: radial-gradient(circle, #aab5ff, transparent 64%);
    opacity: 0.1;
  }

  .grain {
    position: fixed;
    inset: 0;
    z-index: 2;
    pointer-events: none;
    opacity: 0.035;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='3'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='.55'/%3E%3C/svg%3E");
  }

  .glass {
    background: linear-gradient(180deg, var(--glass-2), var(--glass));
    border: 1px solid var(--line);
    box-shadow: 0 30px 90px rgba(0, 0, 0, 0.42);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
  }

  .app {
    position: relative;
    z-index: 1;
    display: grid;
    grid-template-columns: 256px minmax(0, 1fr);
    gap: 14px;
    max-width: 1480px;
    min-height: 100vh;
    margin: 0 auto;
    padding: 14px;
    transition: filter 0.3s ease, opacity 0.3s ease;
  }

  body.locked .app {
    filter: blur(10px);
    opacity: 0.45;
    pointer-events: none;
  }

  /* ============ AUTH ============ */
  .auth {
    position: fixed;
    inset: 0;
    z-index: 200;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
    background: radial-gradient(900px 600px at 50% 0%, rgba(154, 230, 164, 0.06), transparent 60%), #050505;
  }

  .auth__card {
    width: 100%;
    max-width: 410px;
    border-radius: 26px;
    padding: 34px 30px 28px;
    animation: rise 0.5s cubic-bezier(0.2, 0.8, 0.2, 1) both;
  }

  .auth__mark {
    width: 50px;
    height: 50px;
    border-radius: 15px;
    background: var(--ink);
    color: #050505;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    margin-bottom: 24px;
  }

  .auth__mark svg { width: 23px; height: 23px; }

  .auth__card h2 {
    margin: 8px 0 10px;
    font-size: 25px;
    font-weight: 600;
    letter-spacing: -0.05em;
  }

  .auth__card > p {
    margin: 0 0 24px;
    font-size: 10.5px;
    line-height: 1.7;
    color: var(--muted);
  }

  .auth__card > p code {
    color: var(--ink-soft);
    background: rgba(255, 255, 255, 0.06);
    padding: 2px 6px;
    border-radius: 6px;
    font-size: 10px;
  }

  .auth__hint {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 7px;
    margin-top: 18px;
    font-size: 8.5px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--dim);
  }

  .auth__hint svg { width: 11px; height: 11px; }
  .auth.shake { animation: shake 0.4s ease; }

  /* ============ SIDEBAR ============ */
  .sidebar {
    position: sticky;
    top: 14px;
    height: calc(100vh - 28px);
    display: flex;
    flex-direction: column;
    gap: 22px;
    padding: 18px;
    border-radius: 26px;
  }

  .sidebar__top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }

  .brand {
    display: flex;
    align-items: center;
    gap: 11px;
    min-width: 0;
  }

  .brand__mark {
    width: 38px;
    height: 38px;
    flex: 0 0 38px;
    border-radius: 12px;
    background: var(--ink);
    color: #050505;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .brand__mark svg { width: 19px; height: 19px; }

  .brand__text { display: flex; flex-direction: column; gap: 2px; line-height: 1; }

  .brand__text b {
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.08em;
  }

  .brand__text span {
    font-size: 7.5px;
    letter-spacing: 0.22em;
    color: var(--muted);
  }

  .nav {
    display: flex;
    flex-direction: column;
    gap: 4px;
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    scrollbar-width: none;
  }

  .nav::-webkit-scrollbar { display: none; }

  .nav__group {
    margin: 14px 10px 7px;
    font-size: 7.5px;
    font-weight: 500;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    color: var(--dim);
  }

  .nav__group:first-child { margin-top: 0; }

  .nav__link {
    display: flex;
    align-items: center;
    gap: 10px;
    width: 100%;
    padding: 10px 11px;
    border: 1px solid transparent;
    border-radius: 12px;
    background: transparent;
    color: var(--muted);
    font-size: 10.5px;
    text-align: left;
    cursor: pointer;
    transition: 0.16s ease;
  }

  .nav__link svg { width: 15px; height: 15px; flex: 0 0 15px; }

  .nav__link:hover {
    background: rgba(255, 255, 255, 0.04);
    color: var(--ink-soft);
  }

  .nav__link.is-active {
    background: var(--ink);
    border-color: var(--ink);
    color: #050505;
    box-shadow: 0 10px 30px rgba(255, 255, 255, 0.08);
  }

  .nav__badge {
    margin-left: auto;
    min-width: 21px;
    padding: 2px 6px;
    border-radius: 99px;
    background: rgba(255, 255, 255, 0.07);
    border: 1px solid var(--line);
    font-family: var(--font-mono);
    font-size: 8px;
    text-align: center;
    line-height: 1.6;
    color: var(--ink-soft);
  }

  .nav__link.is-active .nav__badge {
    background: rgba(0, 0, 0, 0.12);
    border-color: rgba(0, 0, 0, 0.12);
    color: #050505;
  }

  .sidebar__foot { display: flex; flex-direction: column; gap: 8px; }

  .storage {
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 10px 11px;
    border: 1px solid var(--line);
    border-radius: 13px;
    background: rgba(0, 0, 0, 0.18);
  }

  .storage__dot {
    width: 6px;
    height: 6px;
    flex: 0 0 6px;
    border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 0 4px rgba(154, 230, 164, 0.08), 0 0 14px rgba(154, 230, 164, 0.7);
  }

  .storage div { display: flex; flex-direction: column; gap: 2px; min-width: 0; }

  .storage b { font-size: 9px; font-weight: 500; }

  .storage span { font-size: 7.5px; color: var(--muted); letter-spacing: 0.04em; }

  .storage svg { width: 13px; height: 13px; margin-left: auto; color: var(--dim); }

  .side-logout {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 9px 11px;
    border: 0;
    border-radius: 11px;
    background: transparent;
    color: var(--dim);
    font-size: 9px;
    cursor: pointer;
    transition: 0.16s;
  }

  .side-logout:hover { color: var(--danger); background: rgba(255, 155, 159, 0.06); }

  .side-logout svg { width: 14px; height: 14px; }

  /* ============ MAIN / TOPBAR ============ */
  .main {
    display: flex;
    flex-direction: column;
    gap: 14px;
    min-width: 0;
  }

  .topbar {
    position: sticky;
    top: 14px;
    z-index: 30;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 9px 10px;
    border-radius: 18px;
  }

  .icon-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 34px;
    height: 34px;
    flex: 0 0 34px;
    border: 1px solid var(--line);
    border-radius: 11px;
    background: rgba(255, 255, 255, 0.03);
    color: var(--muted);
    cursor: pointer;
    transition: 0.16s;
  }

  .icon-btn:hover {
    border-color: var(--line-strong);
    background: rgba(255, 255, 255, 0.07);
    color: var(--ink);
  }

  .icon-btn svg { width: 15px; height: 15px; }

  .icon-btn--danger:hover {
    border-color: rgba(255, 155, 159, 0.4);
    background: rgba(255, 155, 159, 0.08);
    color: var(--danger);
  }

  .topbar__burger, .sidebar__close { display: none; }

  .topbar__search {
    display: flex;
    align-items: center;
    gap: 9px;
    flex: 1;
    min-width: 0;
    height: 36px;
    padding: 0 12px;
    border: 1px solid var(--line);
    border-radius: 12px;
    background: rgba(0, 0, 0, 0.2);
  }

  .topbar__search svg { width: 14px; height: 14px; color: var(--dim); flex: 0 0 14px; }

  .topbar__search input {
    flex: 1;
    min-width: 0;
    border: 0;
    outline: none;
    background: transparent;
    color: var(--ink);
    font-size: 10.5px;
  }

  .topbar__search input::placeholder { color: var(--dim); }

  .topbar__right { display: flex; align-items: center; gap: 7px; }

  .pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 5px 9px;
    border: 1px solid var(--line);
    border-radius: 99px;
    background: rgba(255, 255, 255, 0.03);
    font-size: 8px;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--muted);
    white-space: nowrap;
  }

  .pill i {
    width: 4px;
    height: 4px;
    border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 10px var(--green);
  }

  .pill--live { color: var(--ink-soft); }

  .pill--exact { color: var(--ink-soft); background: rgba(255, 255, 255, 0.055); }
  .pill--contains { color: var(--green); background: rgba(154, 230, 164, 0.09); border-color: rgba(154, 230, 164, 0.18); }
  .pill--regex { color: var(--violet); background: rgba(195, 199, 255, 0.09); border-color: rgba(195, 199, 255, 0.18); }
  .pill--media { color: var(--muted); }

  .topbar__count {
    font-size: 9px;
    color: var(--muted);
    padding: 0 4px;
    white-space: nowrap;
  }

  .topbar__count b { color: var(--ink); font-family: var(--font-mono); font-weight: 500; }

  /* ============ VIEWS ============ */
  .view {
    display: none;
    flex-direction: column;
    gap: 16px;
    animation: fade 0.25s ease both;
  }

  .view.is-active { display: flex; }

  .page-head {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
    padding: 10px 4px 2px;
  }

  .eyebrow {
    display: block;
    margin-bottom: 9px;
    font-size: 7.5px;
    font-weight: 500;
    letter-spacing: 0.24em;
    text-transform: uppercase;
    color: var(--dim);
  }

  .page-head h1 {
    margin: 0;
    font-size: clamp(26px, 2.5vw, 36px);
    font-weight: 600;
    letter-spacing: -0.06em;
    line-height: 1;
  }

  .page-head p {
    margin: 10px 0 0;
    max-width: 440px;
    font-size: 10px;
    line-height: 1.7;
    color: var(--muted);
  }

  .page-head__actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }

  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 7px;
    padding: 10px 14px;
    border: 1px solid transparent;
    border-radius: 12px;
    background: transparent;
    font-size: 10px;
    font-weight: 500;
    cursor: pointer;
    white-space: nowrap;
    transition: 0.16s ease;
  }

  .btn svg { width: 14px; height: 14px; }

  .btn--light {
    background: var(--ink);
    border-color: var(--ink);
    color: #050505;
    box-shadow: 0 12px 30px rgba(255, 255, 255, 0.07);
  }

  .btn--light:hover { background: #ffffff; transform: translateY(-1px); }

  .btn--glass {
    background: rgba(255, 255, 255, 0.04);
    border-color: var(--line);
    color: var(--ink-soft);
  }

  .btn--glass:hover { border-color: var(--line-strong); background: rgba(255, 255, 255, 0.075); }

  .btn--ghost { color: var(--muted); border-color: transparent; }
  .btn--ghost:hover { color: var(--ink); background: rgba(255, 255, 255, 0.05); }

  .btn--sm { padding: 8px 11px; font-size: 9px; border-radius: 10px; }
  .btn--xs { padding: 6px 9px; font-size: 8px; border-radius: 9px; }
  .btn--block { width: 100%; }

  .btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none !important; }

  /* ============ METRICS ============ */
  .metric-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 10px;
  }

  .metric {
    position: relative;
    overflow: hidden;
    padding: 16px 17px;
    border-radius: 18px;
  }

  .metric::after {
    content: "";
    position: absolute;
    right: -30px;
    bottom: -50px;
    width: 120px;
    height: 120px;
    border-radius: 50%;
    background: radial-gradient(circle, rgba(255, 255, 255, 0.08), transparent 70%);
    pointer-events: none;
  }

  .metric__top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 18px;
  }

  .metric__label {
    font-size: 8px;
    letter-spacing: 0.13em;
    text-transform: uppercase;
    color: var(--muted);
  }

  .metric__icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 27px;
    height: 27px;
    border: 1px solid var(--line);
    border-radius: 9px;
    background: rgba(255, 255, 255, 0.03);
    color: var(--muted);
  }

  .metric__icon svg { width: 13px; height: 13px; }

  .metric__value {
    font-size: 31px;
    font-weight: 500;
    letter-spacing: -0.07em;
    line-height: 1;
  }

  .metric__foot {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-top: 9px;
    font-size: 8px;
    color: var(--dim);
  }

  .metric__foot b { font-weight: 500; color: var(--ink-soft); }

  .metric--green .metric__value { color: var(--green); }
  .metric--violet .metric__value { color: var(--violet); }

  /* ============ PANELS / GRIDS ============ */
  .grid-2 {
    display: grid;
    grid-template-columns: 1.1fr 0.9fr;
    gap: 10px;
    align-items: stretch;
  }

  .panel {
    padding: 19px;
    border-radius: var(--radius);
  }

  .panel__head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 17px;
  }

  .panel__title { font-size: 12px; font-weight: 500; letter-spacing: -0.02em; }

  .panel__sub { margin-top: 5px; font-size: 8.5px; color: var(--muted); }

  .page-subhead {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 4px 4px 0;
    margin: 0;
  }

  .bars { display: flex; flex-direction: column; gap: 14px; }

  .bar-row__top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 7px;
    font-size: 9px;
    color: var(--muted);
  }

  .bar-row__top b { font-family: var(--font-mono); font-weight: 500; color: var(--ink-soft); }

  .bar {
    height: 4px;
    border-radius: 99px;
    background: rgba(255, 255, 255, 0.065);
    overflow: hidden;
  }

  .bar i {
    display: block;
    height: 100%;
    width: 0;
    border-radius: 99px;
    background: linear-gradient(90deg, rgba(255, 255, 255, 0.35), var(--ink));
    transition: width 0.5s cubic-bezier(0.2, 0.8, 0.2, 1);
  }

  .bar--contains i { background: linear-gradient(90deg, rgba(154, 230, 164, 0.3), var(--green)); }
  .bar--regex i { background: linear-gradient(90deg, rgba(195, 199, 255, 0.3), var(--violet)); }

  .stat-list { display: flex; flex-direction: column; }

  .stat-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 10px 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.055);
    font-size: 9px;
    color: var(--muted);
  }

  .stat-row:last-child { border-bottom: 0; padding-bottom: 0; }

  .stat-row b { font-family: var(--font-mono); font-weight: 500; color: var(--ink); }

  .stat-row .ok { display: inline-flex; align-items: center; gap: 6px; color: var(--green); }
  .stat-row .ok i { width: 4px; height: 4px; border-radius: 50%; background: var(--green); }

  /* ============ CARDS ============ */
  .project-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(245px, 1fr));
    gap: 10px;
  }

  .trigger {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: 10px;
    min-height: 156px;
    padding: 15px;
    border-radius: 17px;
    transition: transform 0.18s ease, border-color 0.18s ease, background 0.18s ease;
  }

  .trigger:hover {
    transform: translateY(-2px);
    border-color: var(--line-strong);
    background: linear-gradient(180deg, rgba(255, 255, 255, 0.07), rgba(255, 255, 255, 0.035));
  }

  .trigger__top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  .trigger__badges { display: flex; align-items: center; gap: 5px; flex-wrap: wrap; }

  .trigger__actions { display: flex; align-items: center; gap: 4px; opacity: 0.55; transition: 0.16s; }
  .trigger:hover .trigger__actions { opacity: 1; }

  .trigger__actions .icon-btn { width: 27px; height: 27px; flex-basis: 27px; border-radius: 8px; }
  .trigger__actions .icon-btn svg { width: 12px; height: 12px; }

  .trigger__phrase {
    margin: 0;
    font-size: 13px;
    font-weight: 500;
    letter-spacing: -0.035em;
    line-height: 1.35;
    word-break: break-word;
  }

  .trigger__pattern {
    display: flex;
    align-items: flex-start;
    gap: 6px;
    font-family: var(--font-mono);
    font-size: 8.5px;
    line-height: 1.6;
    color: var(--violet);
    word-break: break-all;
  }

  .trigger__pattern svg { width: 11px; height: 11px; flex: 0 0 11px; margin-top: 1px; }

  .trigger__text {
    margin: 0;
    display: -webkit-box;
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
    font-size: 9.5px;
    line-height: 1.65;
    color: var(--muted);
    word-break: break-word;
  }

  .trigger__chips { display: flex; flex-wrap: wrap; gap: 4px; }

  .chip-link {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    max-width: 100%;
    padding: 4px 7px;
    border: 1px solid var(--line);
    border-radius: 7px;
    background: rgba(0, 0, 0, 0.18);
    font-size: 8px;
    color: var(--ink-soft);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .chip-link svg { width: 10px; height: 10px; color: var(--dim); flex: 0 0 10px; }

  .trigger__foot {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-top: auto;
    padding-top: 9px;
    border-top: 1px solid rgba(255, 255, 255, 0.055);
    font-size: 8px;
    color: var(--dim);
  }

  .trigger__foot b { color: var(--muted); font-weight: 500; }

  /* ============ FILTERS / LIST ============ */
  .filters {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
    padding: 0 2px;
  }

  .chips { display: flex; align-items: center; gap: 5px; flex-wrap: wrap; }

  .chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 7px 10px;
    border: 1px solid var(--line);
    border-radius: 99px;
    background: rgba(255, 255, 255, 0.025);
    color: var(--muted);
    font-size: 8.5px;
    cursor: pointer;
    transition: 0.16s;
  }

  .chip:hover { border-color: var(--line-strong); color: var(--ink-soft); }

  .chip.is-active {
    background: var(--ink);
    border-color: var(--ink);
    color: #050505;
  }

  .chip b { font-family: var(--font-mono); font-weight: 500; opacity: 0.65; }
  .chip.is-active b { opacity: 0.55; }

  .list-panel { padding: 17px; }

  .empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 12px;
    padding: 64px 20px;
    text-align: center;
    color: var(--muted);
  }

  .empty__icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 48px;
    height: 48px;
    border: 1px solid var(--line);
    border-radius: 15px;
    background: rgba(255, 255, 255, 0.03);
    color: var(--dim);
  }

  .empty__icon svg { width: 20px; height: 20px; }

  .empty b { font-size: 11px; font-weight: 500; color: var(--ink-soft); }
  .empty span { font-size: 9px; line-height: 1.7; max-width: 280px; }

  /* ============ EDITOR MODAL ============ */
  .modal {
    position: fixed;
    inset: 0;
    z-index: 150;
    display: none;
    align-items: center;
    justify-content: center;
    padding: 18px;
  }

  .modal.is-open { display: flex; }

  .modal__backdrop {
    position: absolute;
    inset: 0;
    background: rgba(0, 0, 0, 0.72);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    animation: fade 0.2s ease both;
  }

  .modal__card {
    position: relative;
    z-index: 1;
    display: flex;
    flex-direction: column;
    width: 100%;
    max-width: 1080px;
    max-height: calc(100vh - 36px);
    border-radius: 26px;
    overflow: hidden;
    animation: rise 0.3s cubic-bezier(0.2, 0.8, 0.2, 1) both;
  }

  .modal__head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 14px;
    padding: 20px 22px 16px;
    border-bottom: 1px solid var(--line);
  }

  .modal__head h2 {
    margin: 0;
    font-size: 22px;
    font-weight: 600;
    letter-spacing: -0.05em;
  }

  .modal__head .eyebrow { margin-bottom: 7px; }

  .modal__body {
    padding: 18px 22px;
    overflow-y: auto;
    scrollbar-width: thin;
    scrollbar-color: rgba(255, 255, 255, 0.15) transparent;
  }

  .modal__body::-webkit-scrollbar { width: 5px; }
  .modal__body::-webkit-scrollbar-thumb { background: rgba(255, 255, 255, 0.13); border-radius: 9px; }

  .editor-grid {
    display: grid;
    grid-template-columns: minmax(0, 1.08fr) minmax(0, 0.92fr);
    gap: 22px;
    align-items: start;
  }

  .modal__foot {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 8px;
    padding: 14px 22px;
    border-top: 1px solid var(--line);
    background: rgba(0, 0, 0, 0.22);
  }

  /* ============ FIELDS ============ */
  .field { display: block; margin-bottom: 17px; }

  .field__label {
    display: block;
    margin-bottom: 8px;
    font-size: 8px;
    font-weight: 500;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--muted);
  }

  .field__hint {
    display: block;
    margin-top: 7px;
    font-size: 8px;
    line-height: 1.6;
    color: var(--dim);
  }

  .field__hint code { color: var(--violet); font-size: 7.5px; }

  .input-wrap {
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 0 12px;
    border: 1px solid var(--line);
    border-radius: 12px;
    background: rgba(0, 0, 0, 0.24);
    transition: border-color 0.16s, background 0.16s;
  }

  .input-wrap svg { width: 14px; height: 14px; color: var(--dim); flex: 0 0 14px; }

  .input-wrap:focus-within { border-color: var(--line-strong); background: rgba(0, 0, 0, 0.34); }

  input[type="text"],
  input[type="password"],
  textarea {
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 12px;
    outline: none;
    background: rgba(0, 0, 0, 0.24);
    color: var(--ink);
    padding: 12px 13px;
    font-size: 10.5px;
    font-weight: 400;
    transition: border-color 0.16s, background 0.16s;
  }

  .input-wrap input { border: 0; background: transparent; padding: 12px 0; }

  input:focus, textarea:focus { border-color: var(--line-strong); background: rgba(0, 0, 0, 0.34); }

  .input-wrap input:focus { background: transparent; }

  input::placeholder, textarea::placeholder { color: var(--dim); }

  textarea {
    min-height: 128px;
    resize: vertical;
    line-height: 1.65;
    font-family: var(--font-mono);
    font-size: 10px;
  }

  .segmented {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 3px;
    padding: 3px;
    border: 1px solid var(--line);
    border-radius: 13px;
    background: rgba(0, 0, 0, 0.24);
  }

  .segmented button {
    padding: 9px 8px;
    border: 0;
    border-radius: 10px;
    background: transparent;
    color: var(--muted);
    font-size: 9px;
    cursor: pointer;
    transition: 0.16s;
  }

  .segmented button:hover { color: var(--ink-soft); background: rgba(255, 255, 255, 0.04); }

  .segmented button.is-active {
    background: var(--ink);
    color: #050505;
    box-shadow: 0 8px 20px rgba(255, 255, 255, 0.08);
  }

  .toolbar {
    display: flex;
    align-items: center;
    gap: 4px;
    flex-wrap: wrap;
    margin-bottom: 8px;
  }

  .tool-btn {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 6px 8px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: rgba(0, 0, 0, 0.18);
    color: var(--muted);
    font-size: 8.5px;
    cursor: pointer;
    transition: 0.16s;
  }

  .tool-btn:hover { border-color: var(--line-strong); color: var(--ink); background: rgba(255, 255, 255, 0.05); }
  .tool-btn svg { width: 12px; height: 12px; }

  /* ============ DROPZONE ============ */
  .dropzone {
    position: relative;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 7px;
    padding: 24px 16px;
    border: 1px dashed var(--line-strong);
    border-radius: 14px;
    background: rgba(0, 0, 0, 0.16);
    text-align: center;
    cursor: pointer;
    transition: 0.18s;
  }

  .dropzone:hover, .dropzone.is-drag {
    border-color: rgba(255, 255, 255, 0.35);
    background: rgba(255, 255, 255, 0.04);
  }

  .dropzone svg { width: 21px; height: 21px; color: var(--dim); }

  .dropzone b { font-size: 9px; font-weight: 500; color: var(--ink-soft); word-break: break-all; }
  .dropzone span { font-size: 8px; color: var(--dim); }

  .dropzone input[type="file"] {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    opacity: 0;
    cursor: pointer;
  }

  .media-existing {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    margin-top: 8px;
    padding: 8px 10px;
    border: 1px solid var(--line);
    border-radius: 10px;
    background: rgba(255, 255, 255, 0.025);
    font-size: 8.5px;
    color: var(--muted);
  }

  .media-existing span { display: inline-flex; align-items: center; gap: 6px; min-width: 0; }
  .media-existing svg { width: 12px; height: 12px; flex: 0 0 12px; }
  .media-existing b { color: var(--ink-soft); font-weight: 500; }

  .media-existing button {
    border: 0;
    background: transparent;
    color: var(--danger);
    font-size: 8px;
    cursor: pointer;
    padding: 3px 5px;
    border-radius: 6px;
  }

  .media-existing button:hover { background: rgba(255, 155, 159, 0.08); }

  /* ============ BUTTONS BUILDER ============ */
  .btn-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1.25fr) auto auto;
    gap: 6px;
    align-items: center;
    margin-bottom: 6px;
  }

  .btn-row input[type="text"] { padding: 10px 11px; font-size: 9.5px; }

  .check {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-size: 8px;
    color: var(--muted);
    white-space: nowrap;
    cursor: pointer;
    user-select: none;
  }

  .check input { accent-color: var(--ink); width: 13px; height: 13px; }

  .btn-row .icon-btn { width: 31px; height: 31px; flex-basis: 31px; border-radius: 9px; }

  .add-btn-row {
    width: 100%;
    margin-top: 4px;
    border-style: dashed;
  }

  /* ============ PREVIEW ============ */
  .preview-col {
    position: sticky;
    top: 0;
    display: flex;
    flex-direction: column;
    gap: 9px;
    padding: 15px;
    border: 1px solid var(--line);
    border-radius: 18px;
    background: rgba(0, 0, 0, 0.2);
  }

  .preview-label {
    display: flex;
    align-items: center;
    gap: 7px;
    font-size: 7.5px;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: var(--dim);
  }

  .preview-label svg { width: 12px; height: 12px; }

  .tg {
    background: #0e0e0e;
    border: 1px solid rgba(255, 255, 255, 0.07);
    border-radius: 15px 15px 15px 4px;
    padding: 12px 13px;
    min-height: 64px;
  }

  .tg__media {
    display: flex;
    align-items: center;
    gap: 7px;
    margin-bottom: 8px;
    padding: 8px 9px;
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 9px;
    background: rgba(255, 255, 255, 0.03);
    font-size: 8.5px;
    color: var(--ink-soft);
  }

  .tg__media svg { width: 13px; height: 13px; color: var(--muted); }

  .tg__text {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: 12px;
    line-height: 1.55;
    color: #f1f1f1;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .tg__text a { color: #9ecbff; text-decoration: underline; }
  .tg__text code { background: rgba(255, 255, 255, 0.08); padding: 2px 5px; border-radius: 5px; font-size: 10.5px; }
  .tg__text b, .tg__text strong { font-weight: 700; }
  .tg__text i, .tg__text em { font-style: italic; }

  .tg__kb { display: flex; flex-direction: column; gap: 5px; margin-top: 7px; }
  .tg__kb-line { display: flex; gap: 5px; }

  .tg__kb-item {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 5px;
    padding: 8px 8px;
    border-radius: 8px;
    background: var(--ink);
    color: #050505;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: 10.5px;
    font-weight: 600;
    text-align: center;
    overflow: hidden;
  }

  .tg__kb-item span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .preview-meta {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding-top: 2px;
    font-size: 8px;
    color: var(--dim);
  }

  /* ============ TOAST ============ */
  #toast {
    position: fixed;
    right: 20px;
    bottom: 20px;
    z-index: 300;
    display: flex;
    align-items: center;
    gap: 8px;
    max-width: min(360px, calc(100vw - 40px));
    padding: 12px 15px;
    border-radius: 12px;
    background: var(--ink);
    color: #050505;
    font-size: 9.5px;
    font-weight: 500;
    box-shadow: 0 18px 50px rgba(0, 0, 0, 0.5);
    transform: translateY(120%);
    opacity: 0;
    transition: transform 0.28s cubic-bezier(0.2, 0.8, 0.2, 1), opacity 0.28s;
  }

  #toast svg { width: 15px; height: 15px; flex: 0 0 15px; }

  #toast.show { transform: translateY(0); opacity: 1; }

  #toast.error { background: var(--danger); color: #2a0507; }

  svg.lucide { width: 15px; height: 15px; stroke-width: 1.8; flex-shrink: 0; }

  /* ============ SCROLLBARS ============ */
  ::-webkit-scrollbar { width: 8px; height: 8px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: rgba(255, 255, 255, 0.12); border-radius: 99px; }
  ::-webkit-scrollbar-thumb:hover { background: rgba(255, 255, 255, 0.2); }

  /* ============ AUTO REPLIES ============ */
  input[type="number"] {
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 12px;
    outline: none;
    background: rgba(0, 0, 0, 0.24);
    color: var(--ink);
    padding: 10px 11px;
    font-size: 10.5px;
    transition: border-color 0.16s, background 0.16s;
  }

  input[type="number"]:focus { border-color: var(--line-strong); background: rgba(0, 0, 0, 0.34); }
  input[type="number"]::placeholder { color: var(--dim); }

  .switch { position: relative; display: inline-flex; width: 34px; height: 20px; flex: 0 0 34px; cursor: pointer; }

  .switch input { position: absolute; inset: 0; width: 100%; height: 100%; opacity: 0; margin: 0; cursor: pointer; }

  .switch i {
    display: block;
    width: 100%;
    height: 100%;
    border-radius: 99px;
    background: rgba(255, 255, 255, 0.08);
    border: 1px solid var(--line);
    transition: 0.18s;
  }

  .switch i::after {
    content: "";
    position: absolute;
    top: 3px;
    left: 3px;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: var(--muted);
    transition: 0.18s;
  }

  .switch input:checked + i { background: rgba(154, 230, 164, 0.22); border-color: rgba(154, 230, 164, 0.35); }
  .switch input:checked + i::after { left: 17px; background: var(--green); }

  .chance-row { display: flex; align-items: center; gap: 10px; margin: 11px 0 13px; }
  .chance-row input[type="range"] { flex: 1; accent-color: var(--ink); cursor: pointer; }

  .chance-val {
    font-family: var(--font-mono);
    font-size: 10px;
    min-width: 48px;
    text-align: center;
    color: var(--ink-soft);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 6px 8px;
    background: rgba(0, 0, 0, 0.2);
  }

  .variant-row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 9px;
    border: 1px solid var(--line);
    border-radius: 12px;
    background: rgba(0, 0, 0, 0.16);
    margin-bottom: 7px;
  }

  .variant-row__body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; }

  .variant-row__text {
    font-size: 9.5px;
    color: var(--ink-soft);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .variant-row__meta {
    font-size: 7.5px;
    color: var(--dim);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }

  .variant-row .icon-btn { width: 27px; height: 27px; flex: 0 0 27px; border-radius: 8px; }
  .variant-row .icon-btn svg { width: 12px; height: 12px; }

  .variants-empty {
    padding: 10px 12px;
    border: 1px dashed var(--line);
    border-radius: 12px;
    font-size: 8.5px;
    color: var(--dim);
    text-align: center;
    margin-bottom: 7px;
  }

  .auto-section { display: flex; flex-direction: column; }

  .toggle-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 9px 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.055);
  }

  .toggle-row__label { display: flex; flex-direction: column; gap: 2px; }
  .toggle-row__label b { font-size: 9.5px; font-weight: 500; color: var(--ink-soft); }
  .toggle-row__label span { font-size: 7.5px; color: var(--muted); }

  .num-row { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 7px; }
  .num-cell { display: flex; flex-direction: column; gap: 6px; }
  .num-cell span { font-size: 7.5px; color: var(--muted); letter-spacing: 0.06em; text-transform: uppercase; }
  .num-cell input { text-align: center; font-family: var(--font-mono); }

  /* ============ ANIMATIONS ============ */
  @keyframes fade { from { opacity: 0; } to { opacity: 1; } }

  @keyframes rise {
    from { opacity: 0; transform: translateY(14px) scale(0.985); }
    to { opacity: 1; transform: none; }
  }

  @keyframes shake {
    0%, 100% { transform: translateX(0); }
    25% { transform: translateX(-7px); }
    75% { transform: translateX(7px); }
  }

  /* ============ RESPONSIVE ============ */
  @media (max-width: 1180px) {
    .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .project-grid { grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); }
  }

  @media (max-width: 980px) {
    .app { grid-template-columns: 1fr; }
    .sidebar {
      position: fixed;
      top: 12px;
      bottom: 12px;
      left: 12px;
      z-index: 120;
      width: min(290px, calc(100vw - 40px));
      height: auto;
      transform: translateX(calc(-100% - 20px));
      transition: transform 0.28s cubic-bezier(0.2, 0.8, 0.2, 1);
    }
    .sidebar.is-open { transform: none; }
    .topbar__burger, .sidebar__close { display: inline-flex; }
    .app-scrim {
      position: fixed;
      inset: 0;
      z-index: 110;
      background: rgba(0, 0, 0, 0.65);
      backdrop-filter: blur(4px);
      opacity: 0;
      pointer-events: none;
      transition: 0.25s;
    }
    .app-scrim.is-open { opacity: 1; pointer-events: auto; }
    .grid-2 { grid-template-columns: 1fr; }
    .editor-grid { grid-template-columns: 1fr; }
    .preview-col { position: static; }
  }

  @media (max-width: 620px) {
    .app { padding: 8px; gap: 8px; }
    .topbar { top: 8px; }
    .page-head { padding-top: 6px; }
    .metric-grid { grid-template-columns: 1fr 1fr; gap: 7px; }
    .metric { padding: 13px; }
    .metric__value { font-size: 26px; }
    .metric__top { margin-bottom: 13px; }
    .project-grid { grid-template-columns: 1fr; }
    .modal { padding: 8px; }
    .modal__card { max-height: calc(100vh - 16px); border-radius: 20px; }
    .modal__head, .modal__body, .modal__foot { padding-left: 15px; padding-right: 15px; }
    .btn-row { grid-template-columns: 1fr 1fr auto; }
    .btn-row .check { grid-column: 1 / -1; justify-self: start; }
    .topbar__count, .topbar__right .pill { display: none; }
  }
</style>
</head>
<body class="locked">
<div class="liquid" aria-hidden="true">
  <span class="liquid__blob liquid__blob--1"></span>
  <span class="liquid__blob liquid__blob--2"></span>
</div>
<div class="grain" aria-hidden="true"></div>
<div class="app-scrim" id="app_scrim" onclick="toggleSidebar(false)"></div>

<!-- ЭКРАН ВХОДА -->
<div class="auth" id="auth_overlay">
  <div class="auth__card glass" id="auth_card">
    <div class="auth__mark">
      <i data-lucide="terminal-square" style="width:23px;height:23px;"></i>
    </div>
    <span class="eyebrow">Trigger Studio</span>
    <h2>Доступ в панель</h2>
    <p>Введите пароль из переменной <code>WEB_PASSWORD</code> на Render. Пароль сохранится в этом браузере.</p>
    <label class="field" style="margin-bottom:12px;">
      <span class="field__label">Пароль</span>
      <span class="input-wrap">
        <i data-lucide="lock"></i>
        <input type="password" id="pass_input" placeholder="••••••••" autocomplete="current-password" onkeydown="if(event.key==='Enter') login()">
      </span>
    </label>
    <button class="btn btn--light btn--block" id="login_btn" onclick="login()">
      <span>Войти</span>
      <i data-lucide="arrow-right"></i>
    </button>
    <span class="auth__hint"><i data-lucide="database"></i> Upstash Redis · Render</span>
  </div>
</div>

<!-- ПРИЛОЖЕНИЕ -->
<div class="app" id="app">
  <aside class="sidebar glass" id="sidebar" aria-label="Навигация">
    <div class="sidebar__top">
      <div class="brand">
        <span class="brand__mark">
          <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path d="M4 7h16M4 12h10M4 17h13" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
            <circle cx="19" cy="19" r="2.6" fill="currentColor" stroke="none"/>
          </svg>
        </span>
        <span class="brand__text">
          <b>TRIGGER</b>
          <span>STUDIO</span>
        </span>
      </div>
      <button class="icon-btn sidebar__close" type="button" onclick="toggleSidebar(false)" aria-label="Закрыть меню">
        <i data-lucide="x"></i>
      </button>
    </div>

    <nav class="nav">
      <span class="nav__group">Панель</span>
      <button class="nav__link is-active" type="button" data-view="overview">
        <i data-lucide="layout-dashboard"></i>
        <span>Обзор</span>
        <span class="nav__badge" id="nav_total">0</span>
      </button>
      <button class="nav__link" type="button" data-view="triggers">
        <i data-lucide="layers"></i>
        <span>Заготовки</span>
        <span class="nav__badge" id="nav_total_2">0</span>
      </button>
      <button class="nav__link" type="button" data-view="auto">
        <i data-lucide="zap"></i>
        <span>Автоответы</span>
        <span class="nav__badge" id="nav_auto">0</span>
      </button>

      <span class="nav__group">Действия</span>
      <button class="nav__link" type="button" onclick="openEditor()">
        <i data-lucide="plus"></i>
        <span>Новая заготовка</span>
      </button>
      <button class="nav__link" type="button" onclick="exportBackup()">
        <i data-lucide="download"></i>
        <span>Скачать бэкап</span>
      </button>
      <button class="nav__link" type="button" onclick="loadTriggers(true)">
        <i data-lucide="refresh-cw"></i>
        <span>Обновить базу</span>
      </button>
    </nav>

    <div class="sidebar__foot">
      <div class="storage">
        <span class="storage__dot"></span>
        <div>
          <b>Upstash Redis</b>
          <span>синхронизировано</span>
        </div>
        <i data-lucide="database"></i>
      </div>
      <button class="side-logout" type="button" onclick="logout()">
        <i data-lucide="log-out"></i>
        <span>Выйти из панели</span>
      </button>
    </div>
  </aside>

  <div class="main">
    <header class="topbar glass">
      <button class="icon-btn topbar__burger" type="button" onclick="toggleSidebar(true)" aria-label="Открыть меню">
        <i data-lucide="menu"></i>
      </button>
      <label class="topbar__search">
        <i data-lucide="search"></i>
        <input type="search" id="top_search" placeholder="Поиск по фразам, тексту и regex" aria-label="Поиск" oninput="onSearch(this.value)">
      </label>
      <div class="topbar__right">
        <span class="pill pill--live"><i></i> Live</span>
        <span class="topbar__count"><b id="top_total">0</b> фраз</span>
        <button class="icon-btn" type="button" onclick="loadTriggers(true)" title="Обновить">
          <i data-lucide="refresh-cw"></i>
        </button>
      </div>
    </header>

    <!-- ОБЗОР -->
    <section class="view is-active" data-view="overview">
      <div class="page-head">
        <div>
          <span class="eyebrow">Dashboard</span>
          <h1>Обзор</h1>
          <p>Все заготовки, типы совпадений и состояние базы. Бот отвечает в группах автоматически.</p>
        </div>
        <div class="page-head__actions">
          <button class="btn btn--glass btn--sm" type="button" onclick="loadTriggers(true)">
            <i data-lucide="refresh-cw"></i>
            <span>Обновить</span>
          </button>
          <button class="btn btn--light btn--sm" type="button" onclick="openEditor()">
            <span>Новая заготовка</span>
            <i data-lucide="plus"></i>
          </button>
        </div>
      </div>

      <div class="metric-grid">
        <article class="metric glass">
          <div class="metric__top">
            <span class="metric__label">Всего фраз</span>
            <span class="metric__icon"><i data-lucide="layers"></i></span>
          </div>
          <div class="metric__value" id="metric_total">0</div>
          <div class="metric__foot"><b id="metric_total_media">0</b> с медиа</div>
        </article>
        <article class="metric glass metric--green">
          <div class="metric__top">
            <span class="metric__label">Содержит</span>
            <span class="metric__icon"><i data-lucide="text-search"></i></span>
          </div>
          <div class="metric__value" id="metric_contains">0</div>
          <div class="metric__foot">фраза внутри сообщения</div>
        </article>
        <article class="metric glass metric--violet">
          <div class="metric__top">
            <span class="metric__label">Regex</span>
            <span class="metric__icon"><i data-lucide="regex"></i></span>
          </div>
          <div class="metric__value" id="metric_regex">0</div>
          <div class="metric__foot">шаблоны и старые записи</div>
        </article>
        <article class="metric glass">
          <div class="metric__top">
            <span class="metric__label">Кнопок</span>
            <span class="metric__icon"><i data-lucide="mouse-pointer-click"></i></span>
          </div>
          <div class="metric__value" id="metric_buttons">0</div>
          <div class="metric__foot">во всех заготовках</div>
        </article>
      </div>

      <div class="grid-2">
        <section class="panel glass">
          <div class="panel__head">
            <div>
              <div class="panel__title">Типы совпадений</div>
              <div class="panel__sub">Как бот находит фразы в сообщениях</div>
            </div>
            <span class="pill"><i></i> Live</span>
          </div>
          <div class="bars" id="type_bars"></div>
        </section>

        <section class="panel glass">
          <div class="panel__head">
            <div>
              <div class="panel__title">Состояние базы</div>
              <div class="panel__sub">Данные хранятся в Upstash Redis</div>
            </div>
            <span class="pill pill--media">Render</span>
          </div>
          <div class="stat-list">
            <div class="stat-row"><span>Фраз в базе</span><b id="stat_phrases">0</b></div>
            <div class="stat-row"><span>С медиафайлом</span><b id="stat_media">0</b></div>
            <div class="stat-row"><span>Кнопок</span><b id="stat_buttons">0</b></div>
            <div class="stat-row"><span>Медиа-реакций</span><b id="stat_auto_media">0</b></div>
            <div class="stat-row"><span>Ответов на посты канала</span><b id="stat_auto_channel">0</b></div>
            <div class="stat-row"><span>Подключение</span><span class="ok"><i></i> Online</span></div>
          </div>
          <button class="btn btn--glass btn--sm btn--block" type="button" style="margin-top:14px;" onclick="exportBackup()">
            <i data-lucide="download"></i>
            <span>Скачать JSON-бэкап</span>
          </button>
        </section>
      </div>

      <div class="panel__head page-subhead">
        <div class="panel__title">Заготовки</div>
        <button class="btn btn--ghost btn--xs" type="button" onclick="switchView('triggers')">Все заготовки</button>
      </div>
      <div class="project-grid" id="overview_list"></div>
    </section>

    <!-- ЗАГОТОВКИ -->
    <section class="view" data-view="triggers">
      <div class="page-head">
        <div>
          <span class="eyebrow">Database</span>
          <h1>Заготовки</h1>
          <p>Ответы, которые бот отправляет в группах. Нажмите на карточку, чтобы изменить текст, медиа или тип совпадения.</p>
        </div>
        <div class="page-head__actions">
          <button class="btn btn--light btn--sm" type="button" onclick="openEditor()">
            <span>Новая заготовка</span>
            <i data-lucide="plus"></i>
          </button>
        </div>
      </div>

      <div class="filters">
        <div class="chips" id="type_filters">
          <button class="chip is-active" type="button" data-type="all">Все <b>0</b></button>
          <button class="chip" type="button" data-type="exact">Точные <b>0</b></button>
          <button class="chip" type="button" data-type="contains">Содержит <b>0</b></button>
          <button class="chip" type="button" data-type="regex">Regex <b>0</b></button>
        </div>
        <span class="pill" id="shown_count">0 из 0</span>
      </div>

      <section class="panel glass list-panel">
        <div class="project-grid" id="triggers_grid"></div>
        <div class="empty" id="triggers_empty" hidden>
          <span class="empty__icon"><i data-lucide="inbox"></i></span>
          <b>Ничего не найдено</b>
          <span>Измените поиск, выберите другой тип или добавьте новую заготовку.</span>
          <button class="btn btn--light btn--sm" type="button" onclick="openEditor()">
            <i data-lucide="plus"></i>
            <span>Добавить заготовку</span>
          </button>
        </div>
      </section>
    </section>

    <!-- АВТООТВЕТЫ -->
    <section class="view" data-view="auto">
      <div class="page-head">
        <div>
          <span class="eyebrow">Auto replies</span>
          <h1>Автоответы</h1>
          <p>Случайные реакции на стикеры, фото, видео и голосовые, а также мгновенные ответы на посты канала. Для каждого типа — свой шанс срабатывания и список вариантов, из которых бот выбирает случайный.</p>
        </div>
        <div class="page-head__actions">
          <button class="btn btn--glass btn--sm" type="button" onclick="loadAuto(true)">
            <i data-lucide="refresh-cw"></i>
            <span>Обновить</span>
          </button>
        </div>
      </div>

      <section class="panel glass">
        <div class="panel__head">
          <div>
            <div class="panel__title">Реакции на медиа</div>
            <div class="panel__sub">Если в чате появляется стикер, фото, видео или ГС — бот с заданной вероятностью отвечает случайным вариантом</div>
          </div>
          <span class="pill pill--contains"><i></i> Random</span>
        </div>
        <div class="project-grid" id="media_grid"></div>
      </section>

      <div class="grid-2">
        <section class="panel glass">
          <div class="panel__head">
            <div>
              <div class="panel__title">Посты канала</div>
              <div class="panel__sub">Бот первым отвечает на пост, опубликованный от имени канала</div>
            </div>
            <span class="pill pill--regex"><i></i> ТГК</span>
          </div>
          <div id="channel_panel"></div>
        </section>

        <section class="panel glass">
          <div class="panel__head">
            <div>
              <div class="panel__title">Поведение и защита</div>
              <div class="panel__sub">Чтобы бот не спамил и не зацикливался с другими ботами</div>
            </div>
            <span class="pill"><i></i> Shield</span>
          </div>
          <div id="settings_panel"></div>
        </section>
      </div>
    </section>
  </div>
</div>

<!-- РЕДАКТОР -->
<div class="modal" id="editor_modal" aria-hidden="true">
  <div class="modal__backdrop" onclick="closeEditor()"></div>
  <div class="modal__card glass" role="dialog" aria-modal="true" aria-labelledby="editor_title">
    <div class="modal__head">
      <div>
        <span class="eyebrow" id="editor_eyebrow">New trigger</span>
        <h2 id="editor_title">Новая заготовка</h2>
      </div>
      <button class="icon-btn" type="button" onclick="closeEditor()" aria-label="Закрыть">
        <i data-lucide="x"></i>
      </button>
    </div>

    <div class="modal__body">
      <div class="editor-grid">
        <div>
          <label class="field">
            <span class="field__label">Фраза-триггер</span>
            <span class="input-wrap">
              <i data-lucide="message-square"></i>
              <input type="text" id="phrase" placeholder="Например: как скачать" autocomplete="off" spellcheck="false" oninput="onPhraseInput()">
            </span>
            <span class="field__hint" id="phrase_hint">Регистр букв не важен.</span>
          </label>

          <label class="field" id="pattern_field" hidden>
            <span class="field__label">Regex-шаблон</span>
            <span class="input-wrap">
              <i data-lucide="regex"></i>
              <input type="text" id="regex_pattern" placeholder="(скачать|где)\s+игру" autocomplete="off" spellcheck="false">
            </span>
            <span class="field__hint">Бот ищет по этому шаблону. Фраза выше — название записи в панели.</span>
          </label>

          <div class="field">
            <span class="field__label">Тип совпадения</span>
            <div class="segmented" id="type_segmented">
              <button type="button" data-type="exact" onclick="setType('exact')">Точное</button>
              <button type="button" data-type="contains" onclick="setType('contains')">Содержит</button>
              <button type="button" data-type="regex" onclick="setType('regex')">Regex</button>
            </div>
            <span class="field__hint" id="type_hint">Бот ответит, если фраза встретится внутри сообщения.</span>
          </div>

          <label class="field">
            <span class="field__label">Текст ответа (HTML)</span>
            <div class="toolbar">
              <button type="button" class="tool-btn" onclick="wrapTag('b')" title="Жирный"><i data-lucide="bold"></i> B</button>
              <button type="button" class="tool-btn" onclick="wrapTag('i')" title="Курсив"><i data-lucide="italic"></i> I</button>
              <button type="button" class="tool-btn" onclick="wrapTag('u')" title="Подчёркнутый"><i data-lucide="underline"></i> U</button>
              <button type="button" class="tool-btn" onclick="wrapTag('s')" title="Зачёркнутый"><i data-lucide="strikethrough"></i> S</button>
              <button type="button" class="tool-btn" onclick="wrapTag('code')" title="Код"><i data-lucide="code"></i> Code</button>
              <button type="button" class="tool-btn" onclick="wrapTag('tg-spoiler')" title="Спойлер"><i data-lucide="eye-off"></i> Спойлер</button>
              <button type="button" class="tool-btn" onclick="insertLink()" title="Ссылка"><i data-lucide="link"></i> Ссылка</button>
            </div>
            <textarea id="html_text" placeholder="Введите текст ответа..." oninput="updatePreview()"></textarea>
          </label>

          <div class="field">
            <span class="field__label">Медиафайл</span>
            <div class="dropzone" id="dropzone">
              <i data-lucide="upload-cloud"></i>
              <b id="file_name">Файл не выбран</b>
              <span>фото, видео, гиф или документ · до 50 МБ</span>
              <input type="file" id="media_file" onchange="onFileChange()">
            </div>
            <div class="media-existing" id="existing_media_note" hidden>
              <span><i data-lucide="paperclip"></i> В записи: <b id="existing_media_label"></b></span>
              <button type="button" onclick="removeExistingMedia()">Убрать</button>
            </div>
            <input type="hidden" id="existing_media_type" value="text">
            <input type="hidden" id="existing_file_id" value="">
          </div>

          <div class="field" style="margin-bottom:0;">
            <span class="field__label">Кнопки под сообщением</span>
            <div id="buttons_box"></div>
            <button type="button" class="btn btn--glass btn--sm add-btn-row" onclick="addBtnRow()">
              <i data-lucide="plus"></i>
              <span>Добавить кнопку</span>
            </button>
          </div>
        </div>

        <aside class="preview-col">
          <div class="preview-label"><i data-lucide="eye"></i> Предпросмотр сообщения</div>
          <div class="tg">
            <div class="tg__media" id="preview_media" hidden>
              <i data-lucide="paperclip"></i>
              <span id="preview_media_text">file</span>
            </div>
            <div class="tg__text" id="preview_bubble"><span class="muted">Текст ответа появится здесь...</span></div>
          </div>
          <div class="tg__kb" id="preview_kb"></div>
          <div class="preview-meta">
            <span id="preview_type">Содержит</span>
            <span id="preview_buttons">0 кнопок</span>
          </div>
        </aside>
      </div>
    </div>

    <div class="modal__foot">
      <button class="btn btn--glass" type="button" onclick="closeEditor()">Отмена</button>
      <button class="btn btn--light" type="button" id="save_btn" onclick="saveTrigger()">
        <span>Сохранить</span>
        <i data-lucide="check"></i>
      </button>
    </div>
  </div>
</div>

<!-- РЕДАКТОР ВАРИАНТА АВТООТВЕТА -->
<div class="modal" id="answer_modal" aria-hidden="true">
  <div class="modal__backdrop" onclick="closeAnswerEditor()"></div>
  <div class="modal__card glass" role="dialog" aria-modal="true" aria-labelledby="answer_title" style="max-width:760px;">
    <div class="modal__head">
      <div>
        <span class="eyebrow" id="answer_eyebrow">Auto answer</span>
        <h2 id="answer_title">Вариант ответа</h2>
      </div>
      <button class="icon-btn" type="button" onclick="closeAnswerEditor()" aria-label="Закрыть">
        <i data-lucide="x"></i>
      </button>
    </div>

    <div class="modal__body">
      <label class="field">
        <span class="field__label">Текст ответа (HTML)</span>
        <div class="toolbar">
          <button type="button" class="tool-btn" onclick="wrapTag('b', 'answer_text')" title="Жирный"><i data-lucide="bold"></i> B</button>
          <button type="button" class="tool-btn" onclick="wrapTag('i', 'answer_text')" title="Курсив"><i data-lucide="italic"></i> I</button>
          <button type="button" class="tool-btn" onclick="wrapTag('u', 'answer_text')" title="Подчёркнутый"><i data-lucide="underline"></i> U</button>
          <button type="button" class="tool-btn" onclick="wrapTag('code', 'answer_text')" title="Код"><i data-lucide="code"></i> Code</button>
          <button type="button" class="tool-btn" onclick="insertLink('answer_text')" title="Ссылка"><i data-lucide="link"></i> Ссылка</button>
        </div>
        <textarea id="answer_text" placeholder="Введите текст ответа..." style="min-height:96px;"></textarea>
        <span class="field__hint">Оставьте пустым, если вариант должен отправлять только медиа или только кнопки.</span>
      </label>

      <div class="field">
        <span class="field__label">Медиафайл (необязательно)</span>
        <div class="dropzone" id="answer_dropzone">
          <i data-lucide="upload-cloud"></i>
          <b id="answer_file_name">Файл не выбран</b>
          <span>фото, видео, гиф, аудио или документ · до 50 МБ</span>
          <input type="file" id="answer_file" onchange="onAnswerFileChange()">
        </div>
        <div class="media-existing" id="answer_media_note" hidden>
          <span><i data-lucide="paperclip"></i> В варианте: <b id="answer_media_label"></b></span>
          <button type="button" onclick="removeAnswerMedia()">Убрать</button>
        </div>
        <input type="hidden" id="answer_media_type" value="text">
        <input type="hidden" id="answer_file_id" value="">
      </div>

      <div class="field" style="margin-bottom:0;">
        <span class="field__label">Кнопки под сообщением</span>
        <div id="answer_buttons_box"></div>
        <button type="button" class="btn btn--glass btn--sm add-btn-row" onclick="addAnswerBtnRow()">
          <i data-lucide="plus"></i>
          <span>Добавить кнопку</span>
        </button>
      </div>
    </div>

    <div class="modal__foot">
      <button class="btn btn--glass" type="button" onclick="closeAnswerEditor()">Отмена</button>
      <button class="btn btn--light" type="button" id="answer_save_btn" onclick="saveAnswer()">
        <span>Сохранить вариант</span>
        <i data-lucide="check"></i>
      </button>
    </div>
  </div>
</div>

<div id="toast">
  <i data-lucide="check-circle-2"></i>
  <span id="toast_text">Готово</span>
</div>

<script>
let currentDb = {};
let authPass = localStorage.getItem('tg_admin_pass') || '';
let currentView = 'overview';
let activeType = 'all';
let searchQuery = '';
let selectedType = 'contains';
let editingOriginal = '';
let existingMediaType = 'text';
let existingFileId = '';
let toastTimer = null;
let autoData = { media: {}, channel: {}, channel_chats: {}, channel_configs: {}, settings: {}, stats: {} };
let activeChannelScope = 'channel';
let scopeSaveQueue = Promise.resolve();
let editingScope = '';
let editingIndex = -1;
let answerMediaType = 'text';
let answerFileId = '';

const AUTO_MEDIA_TYPES = ['sticker', 'photo', 'video', 'voice', 'animation', 'video_note', 'audio'];
const AUTO_MEDIA_TITLES = {
  sticker: 'Стикеры',
  photo: 'Фото',
  video: 'Видео',
  voice: 'Голосовые (ГС)',
  animation: 'Гифки',
  video_note: 'Видео-кружки',
  audio: 'Аудио'
};

const TYPE_ORDER = ['exact', 'contains', 'regex'];
const TYPE_META = {
  exact: {
    label: 'Точное',
    hint: 'Сообщение должно полностью совпасть с фразой. Регистр букв не важен.',
    phraseHint: 'Например: как скачать'
  },
  contains: {
    label: 'Содержит',
    hint: 'Бот ответит, если фраза встретится внутри сообщения. Регистр букв не важен.',
    phraseHint: 'Например: как скачать'
  },
  regex: {
    label: 'Regex',
    hint: 'Регулярное выражение, поиск без учёта регистра. Например: (скачать|где)\\s+игру',
    phraseHint: 'Например: (скачать|где)\\s+игру'
  }
};

const MEDIA_LABELS = {
  text: 'Текст',
  photo: 'Фото',
  video: 'Видео',
  animation: 'Гифка',
  document: 'Документ',
  sticker: 'Стикер',
  voice: 'Голосовое',
  video_note: 'Кружок'
};

const ICONS = {
  'search': '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  'terminal-square': '<rect x="3" y="3" width="18" height="18" rx="4"/><path d="m7 9 3 3-3 3"/><path d="M13 15h4"/>',
  'lock': '<rect x="4" y="10" width="16" height="11" rx="3"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
  'arrow-right': '<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>',
  'arrow-up-right': '<path d="M7 17 17 7"/><path d="M7 7h10v10"/>',
  'database': '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
  'x': '<path d="M6 6l12 12M18 6 6 18"/>',
  'layout-dashboard': '<rect x="3" y="3" width="7" height="9" rx="2"/><rect x="14" y="3" width="7" height="5" rx="2"/><rect x="14" y="12" width="7" height="9" rx="2"/><rect x="3" y="16" width="7" height="5" rx="2"/>',
  'layers': '<path d="m12 2 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5"/><path d="m3 17 9 5 9-5"/>',
  'plus': '<path d="M12 5v14M5 12h14"/>',
  'download': '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M4 21h16"/>',
  'refresh-cw': '<path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M3 21v-5h5"/><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/>',
  'log-out': '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
  'menu': '<path d="M3 6h18M3 12h18M3 18h18"/>',
  'text-search': '<path d="M21 6H3"/><path d="M10 12H3"/><path d="M10 18H3"/><circle cx="17" cy="15" r="3"/><path d="m21 19-1.5-1.5"/>',
  'regex': '<path d="M17 3v10"/><path d="m12.7 5.5 8.6 5"/><path d="m21.3 5.5-8.6 5"/><circle cx="5" cy="19" r="2"/>',
  'mouse-pointer-click': '<path d="m9 9 5 12 1.8-5.2L21 14Z"/><path d="M7.2 2.2 8 5.1"/><path d="m5.1 8-2.9-.8"/><path d="M14 4.1 12 6"/><path d="m6 12-1.9 2"/>',
  'bold': '<path d="M6 4h8a4 4 0 0 1 0 8H6z"/><path d="M6 12h9a4 4 0 0 1 0 8H6z"/>',
  'check': '<path d="m4 12 5 5L20 6"/>',
  'check-circle-2': '<circle cx="12" cy="12" r="9"/><path d="m8.5 12 2.5 2.5L16 9"/>',
  'code': '<path d="m16 18 6-6-6-6"/><path d="m8 6-6 6 6 6"/>',
  'eye': '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  'eye-off': '<path d="m3 3 18 18"/><path d="M10.6 5.2A10.5 10.5 0 0 1 12 5c6.5 0 10 7 10 7a18 18 0 0 1-3.1 4.1"/><path d="M6.6 6.6C3.7 8.4 2 12 2 12s3.5 7 10 7a10.4 10.4 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
  'inbox': '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="m5.5 5.1-3 6.9a2 2 0 0 0-.2.8V18a2 2 0 0 0 2 2h15.4a2 2 0 0 0 2-2v-5.2a2 2 0 0 0-.2-.8l-3-6.9A2 2 0 0 0 16.7 4H7.3a2 2 0 0 0-1.8 1.1Z"/>',
  'italic': '<path d="M19 4h-9M14 20H5M15 4 9 20"/>',
  'link': '<path d="M10 13a5 5 0 0 0 7.1 0l2.9-2.9a5 5 0 0 0-7.1-7.1L11 4.9"/><path d="M14 11a5 5 0 0 0-7.1 0L4 13.9a5 5 0 0 0 7.1 7.1L13 19.1"/>',
  'link-2': '<path d="M9 17H7a5 5 0 0 1 0-10h2"/><path d="M15 7h2a5 5 0 0 1 0 10h-2"/><path d="M8 12h8"/>',
  'message-square': '<path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4Z"/>',
  'paperclip': '<path d="m21.4 11.6-8.9 8.9a6 6 0 0 1-8.5-8.5l8.9-8.9a4 4 0 0 1 5.7 5.7l-8.9 8.9a2 2 0 0 1-2.8-2.8l8.2-8.2"/>',
  'pencil': '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
  'strikethrough': '<path d="M16 4H9a3 3 0 0 0-2.8 4"/><path d="M14 12a4 4 0 0 1 0 8H6"/><path d="M4 12h16"/>',
  'underline': '<path d="M6 4v6a6 6 0 0 0 12 0V4"/><path d="M4 20h16"/>',
  'upload-cloud': '<path d="M16 16l-4-4-4 4"/><path d="M12 12v9"/><path d="M20.4 18.4A5 5 0 0 0 18 9h-1.3A8 8 0 1 0 4 17.3"/>',
  'trash-2': '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v5M14 11v5"/>',
  'zap': '<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>'
};

function iconSvg(name) {
  const inner = ICONS[name] || ICONS['search'];
  return '<svg class="lucide" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + inner + '</svg>';
}

function refreshIcons(root) {
  const scope = root || document;
  scope.querySelectorAll('i[data-lucide]').forEach(el => {
    const template = document.createElement('template');
    template.innerHTML = iconSvg(el.getAttribute('data-lucide')).trim();
    el.replaceWith(template.content.firstChild);
  });
}

function escapeHtml(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/`/g, '&#096;');
}

function plainText(html) {
  const tmp = document.createElement('div');
  tmp.innerHTML = String(html || '');
  return (tmp.textContent || tmp.innerText || '').replace(/\s+/g, ' ').trim();
}

function normType(item) {
  const type = item && item.match_type;
  return TYPE_ORDER.includes(type) ? type : 'regex';
}

function typeLabel(type) {
  return (TYPE_META[type] || TYPE_META.regex).label;
}

function mediaLabel(type) {
  return MEDIA_LABELS[type] || type || 'Текст';
}

function showToast(message, isError) {
  const toast = document.getElementById('toast');
  document.getElementById('toast_text').textContent = message;
  toast.className = isError ? 'show error' : 'show';
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.className = ''; }, 3200);
}

function lockPanel() {
  document.body.classList.add('locked');
  document.getElementById('auth_overlay').style.display = 'flex';
  setTimeout(() => {
    const input = document.getElementById('pass_input');
    if (input) input.focus();
  }, 80);
}

function unlockPanel() {
  document.body.classList.remove('locked');
  document.getElementById('auth_overlay').style.display = 'none';
}

async function login() {
  const input = document.getElementById('pass_input');
  const value = input.value.trim();
  if (!value) {
    showToast('Введите пароль', true);
    return;
  }
  authPass = value;
  const btn = document.getElementById('login_btn');
  btn.disabled = true;
  const ok = await loadTriggers(true);
  if (ok) await loadAuto(true);
  btn.disabled = false;
  if (ok) {
    localStorage.setItem('tg_admin_pass', authPass);
    unlockPanel();
    showToast('Добро пожаловать');
  } else {
    const card = document.getElementById('auth_card');
    card.classList.remove('shake');
    void card.offsetWidth;
    card.classList.add('shake');
    input.value = '';
    input.focus();
    showToast('Неверный пароль', true);
  }
}

function logout() {
  localStorage.removeItem('tg_admin_pass');
  authPass = '';
  location.reload();
}

async function apiFetch(url, options) {
  const opts = options || {};
  opts.headers = Object.assign({ 'X-Admin-Password': authPass }, opts.headers || {});
  const res = await fetch(url, opts);
  if (res.status === 401) {
    lockPanel();
    throw new Error('unauthorized');
  }
  return res;
}

async function loadTriggers(silent) {
  try {
    const res = await apiFetch('/api/triggers');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    currentDb = await res.json();
    renderAll();
    return true;
  } catch (e) {
    if (!silent) showToast('Не удалось загрузить базу', true);
    return false;
  }
}

function switchView(name) {
  currentView = name;
  document.querySelectorAll('.view').forEach(view => {
    view.classList.toggle('is-active', view.dataset.view === name);
  });
  document.querySelectorAll('.nav__link[data-view]').forEach(link => {
    link.classList.toggle('is-active', link.dataset.view === name);
  });
  toggleSidebar(false);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function toggleSidebar(open) {
  const sidebar = document.getElementById('sidebar');
  const scrim = document.getElementById('app_scrim');
  const shouldOpen = typeof open === 'boolean' ? open : !sidebar.classList.contains('is-open');
  sidebar.classList.toggle('is-open', shouldOpen);
  scrim.classList.toggle('is-open', shouldOpen);
}

function onSearch(value) {
  searchQuery = value.trim().toLowerCase();
  if (currentView !== 'triggers') switchView('triggers');
  renderTriggers();
}

function getItems() {
  return Object.keys(currentDb).map(phrase => {
    return Object.assign({ phrase: phrase }, currentDb[phrase]);
  });
}

function typeCounts(items) {
  const counts = { exact: 0, contains: 0, regex: 0 };
  items.forEach(item => { counts[normType(item)] += 1; });
  return counts;
}

function renderAll() {
  const items = getItems();
  const counts = typeCounts(items);
  const total = items.length;
  const media = items.filter(item => item.file_id && (item.media_type || 'text') !== 'text').length;
  const buttons = items.reduce((sum, item) => {
    return sum + (item.buttons || []).reduce((rowSum, row) => rowSum + row.length, 0);
  }, 0);

  document.getElementById('nav_total').textContent = total;
  document.getElementById('nav_total_2').textContent = total;
  document.getElementById('top_total').textContent = total;
  document.getElementById('metric_total').textContent = total;
  document.getElementById('metric_total_media').textContent = media;
  document.getElementById('metric_contains').textContent = counts.contains;
  document.getElementById('metric_regex').textContent = counts.regex;
  document.getElementById('metric_buttons').textContent = buttons;
  document.getElementById('stat_phrases').textContent = total;
  document.getElementById('stat_media').textContent = media;
  document.getElementById('stat_buttons').textContent = buttons;

  renderBars(counts, total);
  renderFilterCounts(counts);
  renderOverview(items);
  renderTriggers();
  refreshIcons();
}

function renderBars(counts, total) {
  const box = document.getElementById('type_bars');
  box.innerHTML = TYPE_ORDER.map(type => {
    const count = counts[type];
    const percent = total ? Math.round((count / total) * 100) : 0;
    return '<div class="bar-row">' +
      '<div class="bar-row__top"><span>' + typeLabel(type) + '</span><b>' + count + ' · ' + percent + '%</b></div>' +
      '<div class="bar bar--' + type + '"><i style="width:' + percent + '%"></i></div>' +
      '</div>';
  }).join('');
}

function renderFilterCounts(counts) {
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  document.querySelectorAll('#type_filters .chip').forEach(chip => {
    const type = chip.dataset.type;
    const value = type === 'all' ? total : counts[type];
    const badge = chip.querySelector('b');
    if (badge) badge.textContent = value;
  });
}

function triggerCard(item, compact) {
  const type = normType(item);
  const phrase = item.phrase;
  const text = plainText(item.html_text) || (item.file_id ? 'Только медиа, без текста' : 'Без текста');
  const buttons = (item.buttons || []).flat();
  const media = item.file_id && (item.media_type || 'text') !== 'text';
  const pattern = type === 'regex' && item.pattern && item.pattern !== phrase ? item.pattern : '';

  const chips = buttons.slice(0, compact ? 2 : 4).map(btn => {
    return '<span class="chip-link"><i data-lucide="link-2"></i>' + escapeHtml(btn.text) + '</span>';
  }).join('');

  return '<article class="trigger glass">' +
    '<div class="trigger__top">' +
      '<div class="trigger__badges">' +
        '<span class="pill pill--' + type + '">' + typeLabel(type) + '</span>' +
        (media ? '<span class="pill pill--media">' + mediaLabel(item.media_type) + '</span>' : '') +
      '</div>' +
      '<div class="trigger__actions">' +
        (compact ? '' : '<button class="icon-btn" type="button" data-edit="' + escapeAttr(phrase) + '" title="Изменить"><i data-lucide="pencil"></i></button>' +
        '<button class="icon-btn icon-btn--danger" type="button" data-delete="' + escapeAttr(phrase) + '" title="Удалить"><i data-lucide="trash-2"></i></button>') +
      '</div>' +
    '</div>' +
    '<h3 class="trigger__phrase">' + escapeHtml(phrase) + '</h3>' +
    (pattern ? '<div class="trigger__pattern"><i data-lucide="regex"></i><span>' + escapeHtml(pattern) + '</span></div>' : '') +
    '<p class="trigger__text">' + escapeHtml(text) + '</p>' +
    (chips ? '<div class="trigger__chips">' + chips + (buttons.length > (compact ? 2 : 4) ? '<span class="chip-link">+' + (buttons.length - (compact ? 2 : 4)) + '</span>' : '') + '</div>' : '') +
    '<div class="trigger__foot">' +
      '<span><b>' + buttons.length + '</b> кнопок</span>' +
      '<span>' + (media ? mediaLabel(item.media_type) : 'только текст') + '</span>' +
    '</div>' +
  '</article>';
}

function bindCardActions(root) {
  root.querySelectorAll('[data-edit]').forEach(btn => {
    btn.addEventListener('click', () => openEditor(btn.dataset.edit));
  });
  root.querySelectorAll('[data-delete]').forEach(btn => {
    btn.addEventListener('click', () => deleteTrigger(btn.dataset.delete));
  });
  root.querySelectorAll('[data-open]').forEach(btn => {
    btn.addEventListener('click', () => openEditor(btn.dataset.open));
  });
}

function renderOverview(items) {
  const box = document.getElementById('overview_list');
  const preview = items.slice().sort((a, b) => a.phrase.localeCompare(b.phrase, 'ru')).slice(0, 6);
  if (!preview.length) {
    box.innerHTML = '<div class="empty" style="grid-column:1/-1;padding:36px 20px;">' +
      '<span class="empty__icon"><i data-lucide="layers"></i></span>' +
      '<b>База пока пуста</b><span>Добавьте первую заготовку — бот начнёт отвечать в группах.</span>' +
      '</div>';
    return;
  }
  box.innerHTML = preview.map(item => {
    return '<article class="trigger glass" style="min-height:132px;cursor:pointer;" data-open="' + escapeAttr(item.phrase) + '">' +
      '<div class="trigger__top">' +
        '<div class="trigger__badges">' +
          '<span class="pill pill--' + normType(item) + '">' + typeLabel(normType(item)) + '</span>' +
          (item.file_id ? '<span class="pill pill--media">' + mediaLabel(item.media_type) + '</span>' : '') +
        '</div>' +
        '<span class="icon-btn" style="width:27px;height:27px;flex-basis:27px;border-radius:8px;"><i data-lucide="arrow-up-right"></i></span>' +
      '</div>' +
      '<h3 class="trigger__phrase">' + escapeHtml(item.phrase) + '</h3>' +
      '<p class="trigger__text">' + escapeHtml(plainText(item.html_text) || 'Без текста') + '</p>' +
    '</article>';
  }).join('');
  bindCardActions(box);
}

function renderTriggers() {
  const box = document.getElementById('triggers_grid');
  const empty = document.getElementById('triggers_empty');
  const items = getItems().sort((a, b) => a.phrase.localeCompare(b.phrase, 'ru'));
  const filtered = items.filter(item => {
    const type = normType(item);
    if (activeType !== 'all' && type !== activeType) return false;
    if (!searchQuery) return true;
    const haystack = [
      item.phrase,
      plainText(item.html_text),
      item.pattern || '',
      mediaLabel(item.media_type)
    ].join(' ').toLowerCase();
    return haystack.includes(searchQuery);
  });

  document.getElementById('shown_count').textContent = filtered.length + ' из ' + items.length;

  if (!filtered.length) {
    box.innerHTML = '';
    empty.hidden = false;
    refreshIcons();
    return;
  }

  empty.hidden = true;
  box.innerHTML = filtered.map(item => triggerCard(item, false)).join('');
  bindCardActions(box);
  refreshIcons();
}

document.getElementById('type_filters').addEventListener('click', event => {
  const chip = event.target.closest('.chip');
  if (!chip) return;
  activeType = chip.dataset.type;
  document.querySelectorAll('#type_filters .chip').forEach(item => {
    item.classList.toggle('is-active', item === chip);
  });
  renderTriggers();
});

function setType(type) {
  if (!TYPE_META[type]) return;
  selectedType = type;
  document.querySelectorAll('#type_segmented button').forEach(btn => {
    btn.classList.toggle('is-active', btn.dataset.type === type);
  });
  document.getElementById('type_hint').textContent = TYPE_META[type].hint;
  document.getElementById('phrase_hint').textContent = TYPE_META[type].phraseHint;

  const patternField = document.getElementById('pattern_field');
  patternField.hidden = type !== 'regex';
  if (type === 'regex') {
    const patternInput = document.getElementById('regex_pattern');
    if (!patternInput.value.trim()) {
      patternInput.value = document.getElementById('phrase').value.trim();
    }
  }
  updatePreview();
}

function onPhraseInput() {
  if (selectedType !== 'regex') return;
  const patternInput = document.getElementById('regex_pattern');
  if (!patternInput.value.trim()) {
    patternInput.value = document.getElementById('phrase').value.trim();
  }
}

function openEditor(phrase) {
  editingOriginal = typeof phrase === 'string' ? phrase : '';
  const item = editingOriginal ? currentDb[editingOriginal] : null;

  document.getElementById('editor_eyebrow').textContent = item ? 'Edit trigger' : 'New trigger';
  document.getElementById('editor_title').textContent = item ? 'Редактирование' : 'Новая заготовка';

  const phraseInput = document.getElementById('phrase');
  phraseInput.value = item ? editingOriginal : '';
  document.getElementById('html_text').value = item ? (item.html_text || '') : '';

  existingMediaType = item ? (item.media_type || 'text') : 'text';
  existingFileId = item ? (item.file_id || '') : '';
  document.getElementById('existing_media_type').value = existingMediaType;
  document.getElementById('existing_file_id').value = existingFileId;

  const fileInput = document.getElementById('media_file');
  fileInput.value = '';
  document.getElementById('file_name').textContent = 'Файл не выбран';

  const box = document.getElementById('buttons_box');
  box.innerHTML = '';
  if (item && item.buttons) {
    item.buttons.forEach(row => {
      row.forEach((btn, index) => addBtnRow(btn.text, btn.url, index > 0));
    });
  }

  const itemType = item ? normType(item) : 'contains';
  setType(itemType);
  document.getElementById('regex_pattern').value = item
    ? (item.pattern || (itemType === 'regex' ? editingOriginal : ''))
    : '';
  updateMediaNote();
  updatePreview();

  const modal = document.getElementById('editor_modal');
  modal.classList.add('is-open');
  modal.setAttribute('aria-hidden', 'false');
  document.body.classList.add('locked');
  refreshIcons();
  setTimeout(() => phraseInput.focus(), 120);
}

function closeEditor() {
  const modal = document.getElementById('editor_modal');
  modal.classList.remove('is-open');
  modal.setAttribute('aria-hidden', 'true');
  if (document.getElementById('auth_overlay').style.display === 'none') {
    document.body.classList.remove('locked');
  }
  editingOriginal = '';
}

function onFileChange() {
  const file = document.getElementById('media_file').files[0];
  document.getElementById('file_name').textContent = file ? file.name : 'Файл не выбран';
  updateMediaNote();
  updatePreview();
}

function updateMediaNote() {
  const note = document.getElementById('existing_media_note');
  const label = document.getElementById('existing_media_label');
  const hasExisting = existingFileId && existingMediaType && existingMediaType !== 'text';
  note.hidden = !hasExisting;
  label.textContent = mediaLabel(existingMediaType);
}

function removeExistingMedia() {
  existingMediaType = 'text';
  existingFileId = '';
  document.getElementById('existing_media_type').value = 'text';
  document.getElementById('existing_file_id').value = '';
  updateMediaNote();
  updatePreview();
  showToast('Медиа будет удалено после сохранения');
}

function wrapTag(tag, targetId) {
  const ta = document.getElementById(targetId || 'html_text');
  const start = ta.selectionStart;
  const end = ta.selectionEnd;
  const selected = ta.value.substring(start, end) || 'текст';
  ta.setRangeText('<' + tag + '>' + selected + '</' + tag + '>', start, end, 'select');
  ta.focus();
  updatePreview();
}

function insertLink(targetId) {
  const url = prompt('Ссылка (https://... или @username):', 'https://t.me/noominora_bot');
  if (!url) return;
  const cleanUrl = url.startsWith('@') ? 'https://t.me/' + url.slice(1) : url;
  const ta = document.getElementById(targetId || 'html_text');
  const start = ta.selectionStart;
  const end = ta.selectionEnd;
  const selected = ta.value.substring(start, end) || 'Скачать';
  ta.setRangeText('<a href="' + cleanUrl + '">' + selected + '</a>', start, end, 'select');
  ta.focus();
  updatePreview();
}

function addBtnRow(text, url, sameRow) {
  const box = document.getElementById('buttons_box');
  const row = document.createElement('div');
  row.className = 'btn-row';
  row.innerHTML =
    '<input type="text" class="b-text" placeholder="Текст кнопки" oninput="updatePreview()">' +
    '<input type="text" class="b-url" placeholder="https://... или @bot" oninput="updatePreview()">' +
    '<label class="check"><input type="checkbox" class="b-same" onchange="updatePreview()"> В ряд</label>' +
    '<button type="button" class="icon-btn icon-btn--danger" title="Удалить кнопку"><i data-lucide="x"></i></button>';
  row.querySelector('.b-text').value = text || '';
  row.querySelector('.b-url').value = url || '';
  row.querySelector('.b-same').checked = Boolean(sameRow);
  row.querySelector('.icon-btn').addEventListener('click', () => {
    row.remove();
    updatePreview();
  });
  box.appendChild(row);
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
      rows[rows.length - 1].push({ text: text, url: url });
    } else {
      rows.push([{ text: text, url: url }]);
    }
  });
  return rows;
}

function updatePreview() {
  const text = document.getElementById('html_text').value;
  const file = document.getElementById('media_file').files[0];
  const bubble = document.getElementById('preview_bubble');
  const mediaBox = document.getElementById('preview_media');
  const mediaText = document.getElementById('preview_media_text');

  if (file) {
    mediaBox.hidden = false;
    mediaText.textContent = file.name;
  } else if (existingFileId && existingMediaType !== 'text') {
    mediaBox.hidden = false;
    mediaText.textContent = 'Медиа в Telegram: ' + mediaLabel(existingMediaType);
  } else {
    mediaBox.hidden = true;
  }

  if (!text.trim()) {
    bubble.innerHTML = '<span class="muted">Текст ответа появится здесь...</span>';
  } else {
    bubble.innerHTML = text;
  }

  const rows = collectButtonRows();
  const kb = document.getElementById('preview_kb');
  kb.innerHTML = rows.map(row => {
    return '<div class="tg__kb-line">' + row.map(btn => {
      return '<div class="tg__kb-item"><span>' + escapeHtml(btn.text) + '</span><i data-lucide="arrow-up-right"></i></div>';
    }).join('') + '</div>';
  }).join('');

  const count = rows.reduce((sum, row) => sum + row.length, 0);
  document.getElementById('preview_type').textContent = typeLabel(selectedType);
  document.getElementById('preview_buttons').textContent = count + ' кнопок';
  refreshIcons();
}

async function saveTrigger() {
  const phraseInput = document.getElementById('phrase');
  const phrase = phraseInput.value.trim();
  if (!phrase) {
    showToast('Введите фразу-триггер', true);
    phraseInput.focus();
    return;
  }

  const pattern = selectedType === 'regex'
    ? (document.getElementById('regex_pattern').value.trim() || phrase)
    : '';

  if (selectedType === 'regex') {
    try {
      new RegExp(pattern, 'i');
    } catch (e) {
      showToast('Ошибка в regex: ' + e.message, true);
      return;
    }
  }

  const key = selectedType === 'regex' ? phrase : phrase.toLowerCase();
  const conflict = currentDb[key] && key !== editingOriginal;
  if (conflict && !confirm('Заготовка с такой фразой уже есть. Заменить её?')) {
    return;
  }

  const btn = document.getElementById('save_btn');
  btn.disabled = true;

  const form = new FormData();
  form.append('phrase', phrase);
  form.append('original_phrase', editingOriginal || '');
  form.append('match_type', selectedType);
  form.append('pattern', pattern);
  form.append('html_text', document.getElementById('html_text').value);
  form.append('buttons', JSON.stringify(collectButtonRows()));
  form.append('existing_media_type', existingMediaType);
  form.append('existing_file_id', existingFileId);

  const fileInput = document.getElementById('media_file');
  if (fileInput.files.length > 0) {
    form.append('file', fileInput.files[0]);
  }

  try {
    const res = await apiFetch('/api/triggers', { method: 'POST', body: form });
    const body = await res.text();
    if (res.ok) {
      closeEditor();
      await loadTriggers(true);
      showToast('Заготовка сохранена');
    } else {
      showToast(body || 'Ошибка сохранения', true);
    }
  } catch (e) {
    showToast('Не удалось сохранить заготовку', true);
  } finally {
    btn.disabled = false;
  }
}

async function deleteTrigger(phrase) {
  if (!confirm('Удалить заготовку «' + phrase + '»?')) return;
  try {
    const res = await apiFetch('/api/triggers?phrase=' + encodeURIComponent(phrase), { method: 'DELETE' });
    if (res.ok) {
      await loadTriggers(true);
      showToast('Заготовка удалена');
    } else {
      const body = await res.text();
      showToast(body || 'Ошибка удаления', true);
    }
  } catch (e) {
    showToast('Не удалось удалить заготовку', true);
  }
}

async function exportBackup() {
  try {
    const res = await apiFetch('/api/export');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const blob = await res.blob();
    const date = new Date().toISOString().slice(0, 10);
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = 'triggers-backup-' + date + '.json';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 2000);
    showToast('Бэкап скачан');
  } catch (e) {
    showToast('Не удалось скачать бэкап', true);
  }
}

// ===== АВТООТВЕТЫ =====

async function loadAuto(silent) {
  try {
    const res = await apiFetch('/api/auto');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    autoData = await res.json();
    if (activeChannelScope !== 'channel') {
      const id = activeChannelScope.slice('channel@'.length);
      if (!autoData.channel_chats?.[id] && !autoData.channel_configs?.[id]) activeChannelScope = 'channel';
    }
    if (!autoData.media) autoData.media = {};
    renderAuto();
    renderAutoStats();
    return true;
  } catch (e) {
    if (!silent) showToast('Не удалось загрузить автоответы', true);
    return false;
  }
}

function isChannelScope(scope) {
  return scope === 'channel' || scope.startsWith('channel@');
}

function scopeConfig(scope) {
  if (isChannelScope(scope)) {
    const chatConfig = autoData.channel_configs?.[scope.slice('channel@'.length)];
    return chatConfig || autoData.channel || {};
  }
  return (autoData.media && autoData.media[scope]) || {};
}

function answersTotal() {
  let total = (autoData.channel && autoData.channel.answers ? autoData.channel.answers.length : 0);
  [...Object.values(autoData.media || {}), ...Object.values(autoData.channel_configs || {})].forEach(cfg => {
    total += (cfg.answers || []).length;
  });
  return total;
}

function renderAutoStats() {
  const stats = autoData.stats || {};
  const set = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  };
  set('stat_auto_media', stats.media || 0);
  set('stat_auto_channel', stats.channel || 0);
}

function renderAuto() {
  const grid = document.getElementById('media_grid');
  grid.innerHTML = AUTO_MEDIA_TYPES.map(t => autoCardHtml(t, scopeConfig(t))).join('');
  bindAutoCardActions(grid);

  const badge = document.getElementById('nav_auto');
  if (badge) badge.textContent = answersTotal();

  renderChannelPanel();
  renderSettingsPanel();
  renderAutoStats();
  refreshIcons();
}

function variantRowHtml(scope, index, answer) {
  const media = answer.file_id && (answer.media_type || 'text') !== 'text';
  const btns = (answer.buttons || []).reduce((s, r) => s + r.length, 0);
  const meta = [];
  if (media) meta.push(mediaLabel(answer.media_type));
  if (btns) meta.push(btns + ' кноп.');
  const text = plainText(answer.html_text) || (media ? 'Медиа без текста' : 'Без содержимого');
  return '<div class="variant-row">' +
    '<div class="variant-row__body">' +
      '<span class="variant-row__text">' + escapeHtml(text) + '</span>' +
      (meta.length ? '<span class="variant-row__meta">' + meta.map(m => escapeHtml(m)).join(' · ') + '</span>' : '') +
    '</div>' +
    '<button class="icon-btn" type="button" data-edit-answer="' + scope + ':' + index + '" title="Изменить"><i data-lucide="pencil"></i></button>' +
    '<button class="icon-btn icon-btn--danger" type="button" data-del-answer="' + scope + ':' + index + '" title="Удалить"><i data-lucide="trash-2"></i></button>' +
  '</div>';
}

function autoCardHtml(type, cfg) {
  const answers = cfg.answers || [];
  const variants = answers.length
    ? answers.map((a, i) => variantRowHtml(type, i, a)).join('')
    : '<div class="variants-empty">Вариантов пока нет — добавьте первый ответ.</div>';
  return '<article class="trigger glass" style="min-height:0;">' +
    '<div class="trigger__top">' +
      '<div class="trigger__badges"><span class="pill pill--contains">' + AUTO_MEDIA_TITLES[type] + '</span></div>' +
      '<label class="switch" title="Включить / выключить"><input type="checkbox" data-toggle="' + type + '"' + (cfg.enabled ? ' checked' : '') + '><i></i></label>' +
    '</div>' +
    '<div class="chance-row">' +
      '<input type="range" min="0" max="100" step="5" value="' + (cfg.chance || 0) + '" data-chance="' + type + '" onchange="onChanceChange(this)">' +
      '<span class="chance-val" data-chance-val="' + type + '">' + (cfg.chance || 0) + '%</span>' +
    '</div>' +
    '<div class="auto-section" style="margin-bottom:10px;">' + variants + '</div>' +
    '<button class="btn btn--glass btn--sm btn--block" type="button" style="border-style:dashed;" onclick="openAnswerEditor(\'' + type + '\', -1)">' +
      '<i data-lucide="plus"></i><span>Добавить вариант</span>' +
    '</button>' +
  '</article>';
}

function bindAutoCardActions(root) {
  root.querySelectorAll('[data-toggle]').forEach(el => {
    el.addEventListener('change', () => saveScopeConfig(el.dataset.toggle, { enabled: el.checked }));
  });
  root.querySelectorAll('[data-edit-answer]').forEach(btn => {
    btn.addEventListener('click', () => {
      const parts = btn.dataset.editAnswer.split(':');
      openAnswerEditor(parts[0], parseInt(parts[1], 10));
    });
  });
  root.querySelectorAll('[data-del-answer]').forEach(btn => {
    btn.addEventListener('click', () => {
      const parts = btn.dataset.delAnswer.split(':');
      deleteAnswer(parts[0], parseInt(parts[1], 10));
    });
  });
}

function onChanceChange(el) {
  const scope = el.dataset.chance;
  const val = parseInt(el.value, 10);
  const label = document.querySelector('[data-chance-val="' + scope + '"]');
  if (label) label.textContent = val + '%';
  saveScopeConfig(scope, { chance: val });
}

function saveScopeConfig(scope, patch) {
  scopeSaveQueue = scopeSaveQueue.then(() => persistScopeConfig(scope, patch));
  return scopeSaveQueue;
}

async function persistScopeConfig(scope, patch) {
  try {
    const res = await apiFetch('/api/auto/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ scope: scope }, patch))
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const body = await res.json();
    if (scope === 'channel') autoData.channel = body.config;
    else if (isChannelScope(scope)) {
      if (!autoData.channel_configs) autoData.channel_configs = {};
      autoData.channel_configs[scope.slice('channel@'.length)] = body.config;
      if (activeChannelScope === scope) syncChannelState();
    } else autoData.media[scope] = body.config;
    showToast('Настройка сохранена');
    return true;
  } catch (e) {
    showToast('Не удалось сохранить настройку', true);
    loadAuto(true);
    return false;
  }
}

function channelChatLabel(chatId) {
  const title = autoData.channel_chats?.[chatId];
  return title ? title + ' · ' + chatId : 'Чат ' + chatId;
}

function syncChannelState() {
  const status = document.getElementById('channel_state');
  const reset = document.getElementById('channel_reset');
  const separate = !!autoData.channel_configs?.[activeChannelScope.slice('channel@'.length)];
  status.textContent = activeChannelScope === 'channel' ? '' : separate
    ? 'Отдельные настройки этого чата'
    : 'Сейчас действуют общие настройки. Изменения сохранятся только для этого чата.';
  reset.hidden = !separate;
}

async function addChannelChat(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const input = form.querySelector('input');
  const chatId = input.value.trim();
  if (!/^-[1-9][0-9]{0,15}$/.test(chatId) || Math.abs(Number(chatId)) >= 2 ** 52) {
    showToast('Укажите отрицательный Telegram ID группы или канала', true);
    input.focus();
    return;
  }
  const scope = 'channel@' + chatId;
  if (!autoData.channel_chats?.[chatId] && !autoData.channel_configs?.[chatId]) {
    const submit = form.querySelector('button');
    submit.disabled = true;
    if (!await saveScopeConfig(scope, {})) {
      submit.disabled = false;
      return;
    }
  }
  activeChannelScope = scope;
  renderChannelPanel();
  refreshIcons();
}

async function resetChannelChat() {
  const scope = activeChannelScope;
  if (!confirm('Сбросить настройки этого чата и использовать общие?')) return;
  const button = document.getElementById('channel_reset');
  button.disabled = true;
  try {
    await scopeSaveQueue;
    const res = await apiFetch('/api/auto/channel-chat?scope=' + encodeURIComponent(scope), { method: 'DELETE' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    await loadAuto(true);
    showToast('Для чата применяются общие настройки');
  } catch (e) {
    showToast('Не удалось сбросить настройки чата', true);
  } finally {
    button.disabled = false;
  }
}

function renderChannelPanel() {
  const scope = activeChannelScope;
  const cfg = scopeConfig(scope);
  const chatIds = [...new Set([
    ...Object.keys(autoData.channel_chats || {}), ...Object.keys(autoData.channel_configs || {})
  ])];
  chatIds.sort((a, b) => channelChatLabel(a).localeCompare(channelChatLabel(b), 'ru'));
  const chatOptions = chatIds.map(id => '<option value="channel@' + escapeHtml(id) + '"' +
    (scope === 'channel@' + id ? ' selected' : '') + '>' + escapeHtml(channelChatLabel(id)) + '</option>').join('');
  const answers = cfg.answers || [];
  const variants = answers.length
    ? answers.map((a, i) => variantRowHtml(scope, i, a)).join('')
    : '<div class="variants-empty">Вариантов пока нет — добавьте первый ответ.</div>';
  const box = document.getElementById('channel_panel');
  box.innerHTML =
    '<div class="field channel-picker">' +
      '<label class="field__label" for="channel_chat">Чат</label>' +
      '<select id="channel_chat"><option value="channel">Общие настройки</option>' + chatOptions + '</select>' +
      '<span class="field__hint" id="channel_state"></span>' +
      '<details><summary>Добавить чат по Telegram ID</summary>' +
        '<form class="channel-picker__form" id="channel_add">' +
          '<input type="text" inputmode="text" placeholder="-1001234567890" aria-label="Telegram ID чата" required>' +
          '<button type="submit" class="btn btn--glass btn--sm">Добавить</button>' +
        '</form>' +
      '</details>' +
      '<button id="channel_reset" type="button" class="btn btn--glass btn--sm" hidden>Вернуть общие настройки</button>' +
    '</div>' +
    '<div class="toggle-row">' +
      '<div class="toggle-row__label"><b>Отвечать на посты канала</b><span>Бот отвечает случайным вариантом с шансом ниже</span></div>' +
      '<label class="switch"><input type="checkbox" data-ctoggle="enabled"' + (cfg.enabled ? ' checked' : '') + '><i></i></label>' +
    '</div>' +
    '<div class="toggle-row">' +
      '<div class="toggle-row__label"><b>В группах</b><span>Пост канала появился в группе — бот отвечает первым</span></div>' +
      '<label class="switch"><input type="checkbox" data-ctoggle="in_groups"' + (cfg.in_groups ? ' checked' : '') + '><i></i></label>' +
    '</div>' +
    '<div class="toggle-row">' +
      '<div class="toggle-row__label"><b>В самом канале</b><span>Бот комментирует посты внутри канала (нужны права админа)</span></div>' +
      '<label class="switch"><input type="checkbox" data-ctoggle="in_channels"' + (cfg.in_channels ? ' checked' : '') + '><i></i></label>' +
    '</div>' +
    '<div class="chance-row">' +
      '<input type="range" min="0" max="100" step="5" value="' + (cfg.chance || 0) + '" data-chance="' + scope + '" onchange="onChanceChange(this)">' +
      '<span class="chance-val" data-chance-val="' + scope + '">' + (cfg.chance || 0) + '%</span>' +
    '</div>' +
    '<div class="auto-section" style="margin-bottom:10px;">' + variants + '</div>' +
    '<button id="channel_add_answer" class="btn btn--glass btn--sm btn--block" type="button" style="border-style:dashed;">' +
      '<i data-lucide="plus"></i><span>Добавить вариант ответа</span>' +
    '</button>';
  bindAutoCardActions(box);
  box.querySelector('#channel_chat').addEventListener('change', event => {
    activeChannelScope = event.target.value;
    renderChannelPanel();
    refreshIcons();
  });
  box.querySelector('#channel_add').addEventListener('submit', addChannelChat);
  box.querySelector('#channel_reset').addEventListener('click', resetChannelChat);
  box.querySelector('#channel_add_answer').addEventListener('click', () => openAnswerEditor(scope, -1));
  syncChannelState();
  box.querySelectorAll('[data-ctoggle]').forEach(el => {
    el.addEventListener('change', () => {
      const patch = {};
      patch[el.dataset.ctoggle] = el.checked;
      saveScopeConfig(scope, patch);
    });
  });
}

function renderSettingsPanel() {
  const st = autoData.settings || {};
  const box = document.getElementById('settings_panel');
  box.innerHTML =
    '<div class="toggle-row">' +
      '<div class="toggle-row__label"><b>Игнорировать других ботов</b><span>Защита от бесконечных петель между ботами</span></div>' +
      '<label class="switch"><input type="checkbox" id="set_ignore_bots"' + (st.ignore_bots ? ' checked' : '') + '><i></i></label>' +
    '</div>' +
    '<div class="field" style="margin-top:13px;">' +
      '<span class="field__label">Кулдаун автоответов, сек (0 — выключен)</span>' +
      '<input type="number" id="set_cooldown" min="0" max="3600" value="' + (st.cooldown || 0) + '">' +
      '<span class="field__hint">Минимальная пауза между автоответами бота в одном чате — защита от спама.</span>' +
    '</div>' +
    '<div class="field">' +
      '<span class="field__label">Случайная задержка перед медиа-реакцией, сек</span>' +
      '<div class="num-row">' +
        '<div class="num-cell"><span>От</span><input type="number" id="set_delay_min" min="0" max="120" value="' + (st.delay_min || 0) + '"></div>' +
        '<div class="num-cell"><span>До</span><input type="number" id="set_delay_max" min="0" max="120" value="' + (st.delay_max || 0) + '"></div>' +
        '<div class="num-cell"><span>&nbsp;</span><button class="btn btn--light btn--sm" type="button" style="width:100%;" onclick="saveSettings()"><span>Сохранить</span></button></div>' +
      '</div>' +
      '<span class="field__hint">Пауза делает реакции живыми — как будто человек посмотрел медиа и ответил.</span>' +
    '</div>' +
    '<div class="stat-list" style="margin-top:8px;">' +
      '<div class="stat-row"><span>Ответов по триггерам</span><b id="auto_stat_triggers">' + ((autoData.stats || {}).triggers || 0) + '</b></div>' +
      '<div class="stat-row"><span>Медиа-реакций</span><b id="auto_stat_media2">' + ((autoData.stats || {}).media || 0) + '</b></div>' +
      '<div class="stat-row"><span>Ответов на посты канала</span><b id="auto_stat_channel2">' + ((autoData.stats || {}).channel || 0) + '</b></div>' +
    '</div>' +
    '<button class="btn btn--glass btn--sm btn--block" type="button" style="margin-top:12px;" onclick="resetStats()">' +
      '<i data-lucide="trash-2"></i><span>Сбросить счётчики</span>' +
    '</button>';
}

async function saveSettings() {
  const payload = {
    ignore_bots: document.getElementById('set_ignore_bots').checked,
    cooldown: parseInt(document.getElementById('set_cooldown').value || '0', 10) || 0,
    delay_min: parseInt(document.getElementById('set_delay_min').value || '0', 10) || 0,
    delay_max: parseInt(document.getElementById('set_delay_max').value || '0', 10) || 0
  };
  try {
    const res = await apiFetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const body = await res.json();
    autoData.settings = body.settings;
    showToast('Настройки сохранены');
  } catch (e) {
    showToast('Не удалось сохранить настройки', true);
  }
}

async function resetStats() {
  if (!confirm('Обнулить счётчики автоответов?')) return;
  try {
    const res = await apiFetch('/api/stats/reset', { method: 'POST' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const body = await res.json();
    autoData.stats = body.stats;
    renderSettingsPanel();
    renderAutoStats();
    refreshIcons();
    showToast('Счётчики сброшены');
  } catch (e) {
    showToast('Не удалось сбросить счётчики', true);
  }
}

function openAnswerEditor(scope, index) {
  editingScope = scope;
  editingIndex = index;
  const answers = scopeConfig(scope).answers || [];
  const item = (index >= 0 && index < answers.length) ? answers[index] : null;

  const scopeTitle = scope === 'channel' ? 'Посты канала' : isChannelScope(scope)
    ? channelChatLabel(scope.slice('channel@'.length)) : AUTO_MEDIA_TITLES[scope];
  document.getElementById('answer_eyebrow').textContent = item ? 'Edit variant' : 'New variant';
  document.getElementById('answer_title').textContent = (item ? 'Редактирование · ' : 'Новый вариант · ') + scopeTitle;

  document.getElementById('answer_text').value = item ? (item.html_text || '') : '';
  answerMediaType = item ? (item.media_type || 'text') : 'text';
  answerFileId = item ? (item.file_id || '') : '';
  document.getElementById('answer_media_type').value = answerMediaType;
  document.getElementById('answer_file_id').value = answerFileId;
  document.getElementById('answer_file').value = '';
  document.getElementById('answer_file_name').textContent = 'Файл не выбран';

  const box = document.getElementById('answer_buttons_box');
  box.innerHTML = '';
  if (item && item.buttons) {
    item.buttons.forEach(row => {
      row.forEach((btn, idx) => addAnswerBtnRow(btn.text, btn.url, idx > 0));
    });
  }

  updateAnswerMediaNote();
  const modal = document.getElementById('answer_modal');
  modal.classList.add('is-open');
  modal.setAttribute('aria-hidden', 'false');
  document.body.classList.add('locked');
  refreshIcons();
}

function closeAnswerEditor() {
  const modal = document.getElementById('answer_modal');
  modal.classList.remove('is-open');
  modal.setAttribute('aria-hidden', 'true');
  if (document.getElementById('auth_overlay').style.display === 'none' &&
      !document.getElementById('editor_modal').classList.contains('is-open')) {
    document.body.classList.remove('locked');
  }
  editingScope = '';
  editingIndex = -1;
}

function onAnswerFileChange() {
  const file = document.getElementById('answer_file').files[0];
  document.getElementById('answer_file_name').textContent = file ? file.name : 'Файл не выбран';
}

function updateAnswerMediaNote() {
  const note = document.getElementById('answer_media_note');
  const hasExisting = answerFileId && answerMediaType && answerMediaType !== 'text';
  note.hidden = !hasExisting;
  document.getElementById('answer_media_label').textContent = mediaLabel(answerMediaType);
}

function removeAnswerMedia() {
  answerMediaType = 'text';
  answerFileId = '';
  document.getElementById('answer_media_type').value = 'text';
  document.getElementById('answer_file_id').value = '';
  updateAnswerMediaNote();
}

function addAnswerBtnRow(text, url, sameRow) {
  const box = document.getElementById('answer_buttons_box');
  const row = document.createElement('div');
  row.className = 'btn-row';
  row.innerHTML =
    '<input type="text" class="b-text" placeholder="Текст кнопки">' +
    '<input type="text" class="b-url" placeholder="https://... или @bot">' +
    '<label class="check"><input type="checkbox" class="b-same"> В ряд</label>' +
    '<button type="button" class="icon-btn icon-btn--danger" title="Удалить кнопку"><i data-lucide="x"></i></button>';
  row.querySelector('.b-text').value = text || '';
  row.querySelector('.b-url').value = url || '';
  row.querySelector('.b-same').checked = Boolean(sameRow);
  row.querySelector('.icon-btn').addEventListener('click', () => row.remove());
  box.appendChild(row);
  refreshIcons();
}

function collectAnswerButtons() {
  const rows = [];
  document.querySelectorAll('#answer_buttons_box .btn-row').forEach(el => {
    const text = el.querySelector('.b-text').value.trim();
    let url = el.querySelector('.b-url').value.trim();
    const same = el.querySelector('.b-same').checked;
    if (!text || !url) return;
    if (url.startsWith('@')) url = 'https://t.me/' + url.slice(1);
    if (same && rows.length > 0) rows[rows.length - 1].push({ text: text, url: url });
    else rows.push([{ text: text, url: url }]);
  });
  return rows;
}

async function saveAnswer() {
  if (!editingScope) return;
  const btn = document.getElementById('answer_save_btn');
  btn.disabled = true;
  const form = new FormData();
  form.append('scope', editingScope);
  form.append('index', String(editingIndex));
  form.append('html_text', document.getElementById('answer_text').value);
  form.append('buttons', JSON.stringify(collectAnswerButtons()));
  form.append('existing_media_type', answerMediaType);
  form.append('existing_file_id', answerFileId);
  const fileInput = document.getElementById('answer_file');
  if (fileInput.files.length > 0) form.append('file', fileInput.files[0]);
  try {
    const res = await apiFetch('/api/auto/answer', { method: 'POST', body: form });
    const body = await res.text();
    if (res.ok) {
      closeAnswerEditor();
      await loadAuto(true);
      showToast('Вариант сохранён');
    } else {
      showToast(body || 'Ошибка сохранения', true);
    }
  } catch (e) {
    showToast('Не удалось сохранить вариант', true);
  } finally {
    btn.disabled = false;
  }
}

async function deleteAnswer(scope, index) {
  if (!confirm('Удалить этот вариант ответа?')) return;
  try {
    const res = await apiFetch('/api/auto/answer?scope=' + encodeURIComponent(scope) + '&index=' + index, { method: 'DELETE' });
    if (res.ok) {
      await loadAuto(true);
      showToast('Вариант удалён');
    } else {
      showToast('Ошибка удаления', true);
    }
  } catch (e) {
    showToast('Не удалось удалить вариант', true);
  }
}

// ===== КОНЕЦ АВТООТВЕТОВ =====

document.addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    const answerModal = document.getElementById('answer_modal');
    if (answerModal.classList.contains('is-open')) {
      closeAnswerEditor();
      return;
    }
    const modal = document.getElementById('editor_modal');
    if (modal.classList.contains('is-open')) closeEditor();
    else toggleSidebar(false);
  }
});

document.querySelectorAll('.nav__link[data-view]').forEach(link => {
  link.addEventListener('click', () => switchView(link.dataset.view));
});

const dropzone = document.getElementById('dropzone');
['dragenter', 'dragover'].forEach(name => {
  dropzone.addEventListener(name, event => {
    event.preventDefault();
    dropzone.classList.add('is-drag');
  });
});
['dragleave', 'drop'].forEach(name => {
  dropzone.addEventListener(name, event => {
    event.preventDefault();
    dropzone.classList.remove('is-drag');
  });
});
dropzone.addEventListener('drop', event => {
  if (event.dataTransfer.files.length) {
    document.getElementById('media_file').files = event.dataTransfer.files;
    onFileChange();
  }
});

const answerDropzone = document.getElementById('answer_dropzone');
['dragenter', 'dragover'].forEach(name => {
  answerDropzone.addEventListener(name, event => {
    event.preventDefault();
    answerDropzone.classList.add('is-drag');
  });
});
['dragleave', 'drop'].forEach(name => {
  answerDropzone.addEventListener(name, event => {
    event.preventDefault();
    answerDropzone.classList.remove('is-drag');
  });
});
answerDropzone.addEventListener('drop', event => {
  if (event.dataTransfer.files.length) {
    document.getElementById('answer_file').files = event.dataTransfer.files;
    onAnswerFileChange();
  }
});

window.addEventListener('resize', () => {
  if (window.innerWidth > 980) toggleSidebar(false);
});

refreshIcons();
if (authPass) {
  loadTriggers(true).then(ok => {
    if (ok) {
      unlockPanel();
      loadAuto(true);
    }
  });
} else {
  setTimeout(() => document.getElementById('pass_input').focus(), 150);
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


def clean_buttons(raw: object) -> list[list[dict]]:
    result: list[list[dict]] = []
    if not isinstance(raw, list):
        return result
    for row in raw:
        if not isinstance(row, list):
            continue
        clean_row = []
        for btn in row:
            if not isinstance(btn, dict):
                continue
            text = str(btn.get("text", "")).strip()
            url = str(btn.get("url", "")).strip()
            if text and url:
                clean_row.append({"text": text, "url": url})
        if clean_row:
            result.append(clean_row)
    return result


async def handle_get_triggers(request: web.Request):
    check_auth(request)
    return web.json_response(await get_all_triggers())


async def handle_export_triggers(request: web.Request):
    check_auth(request)
    db = await get_all_triggers()
    payload = json.dumps(db, ensure_ascii=False, indent=2)
    return web.Response(
        text=payload,
        content_type="application/json",
        charset="utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="triggers-backup.json"'
        },
    )


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

    raw_phrase = fields.get("phrase", "").strip()
    if not raw_phrase:
        return web.Response(status=400, text="Пустая фраза-триггер")

    original_phrase = fields.get("original_phrase", "").strip()
    existing = None
    if original_phrase:
        existing = await get_trigger(original_phrase)
    if existing is None:
        existing = await get_trigger(raw_phrase) or await get_trigger(raw_phrase.lower())
    existing = existing or {}

    match_type = fields.get("match_type", "").strip()
    if match_type not in VALID_MATCH_TYPES:
        if existing:
            match_type = get_item_match_type(original_phrase or raw_phrase, existing)
        else:
            match_type = "exact"

    phrase = raw_phrase.lower() if match_type in ("exact", "contains") else raw_phrase
    pattern = fields.get("pattern", "").strip()

    if match_type == "regex":
        pattern = pattern or phrase
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return web.Response(status=400, text=f"Ошибка в regex: {e}")

    html_text = fields.get("html_text", "")
    try:
        buttons = clean_buttons(json.loads(fields.get("buttons", "[]")))
    except Exception:
        return web.Response(status=400, text="Некорректный список кнопок")

    media_type = fields.get("existing_media_type", "text") or "text"
    file_id = fields.get("existing_file_id") or None

    if file_bytes and file_name:
        try:
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
        except Exception as e:
            return web.Response(status=502, text=f"Не удалось загрузить медиа: {e}")

    item = dict(existing)
    item.update(
        {
            "media_type": media_type,
            "file_id": file_id,
            "html_text": html_text,
            "buttons": buttons,
            "match_type": match_type,
        }
    )
    if match_type == "regex":
        item["pattern"] = pattern
    else:
        item.pop("pattern", None)

    await save_trigger_db(phrase, item)

    if original_phrase and original_phrase != phrase:
        old_key = original_phrase
        if await get_trigger(old_key) is None and old_key.lower() != old_key:
            old_key = old_key.lower()
        if old_key != phrase:
            await delete_trigger_db(old_key)

    return web.json_response({"ok": True, "phrase": phrase, "match_type": match_type})


async def handle_delete_trigger(request: web.Request):
    check_auth(request)
    phrase = request.query.get("phrase", "").strip()
    if phrase:
        if await get_trigger(phrase) is None and phrase.lower() != phrase:
            phrase = phrase.lower()
        await delete_trigger_db(phrase)
    return web.json_response({"ok": True})


# --- API АВТООТВЕТОВ ---


def parse_auto_scope(scope: str) -> tuple[str, str | None]:
    """Разбирает область автоответа и ключ медиа или чата."""
    scope = (scope or "").strip()
    if scope == "channel":
        return "channel", None
    if scope.startswith("channel@"):
        chat_id = scope.removeprefix("channel@")
        if re.fullmatch(r"-[1-9][0-9]{0,15}", chat_id) and abs(int(chat_id)) < 2**52:
            return "channel", chat_id
    media_type = scope.removeprefix("media:")
    if media_type in AUTO_MEDIA_TYPES:
        return "media", media_type
    return "", None


async def handle_get_auto(request: web.Request):
    check_auth(request)
    return web.json_response(
        {
            "media": await get_all_media_configs(),
            "channel": await get_channel_config(),
            "channel_chats": await redis_client.hgetall(CHANNEL_CHAT_TITLES_KEY),
            "channel_configs": {
                chat_id: config
                for chat_id, raw in (await redis_client.hgetall(CHANNEL_CHAT_CONFIGS_KEY)).items()
                if (config := decode_channel_config(raw)) is not None
            },
            "settings": await get_settings(),
            "stats": await get_stats(),
        }
    )


async def handle_save_auto_config(request: web.Request):
    check_auth(request)
    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="Некорректный JSON")
    if not isinstance(body, dict):
        return web.Response(status=400, text="Некорректный JSON")

    scope, scope_key = parse_auto_scope(str(body.get("scope", "")))
    if not scope:
        return web.Response(status=400, text="Неизвестный scope")

    if scope == "channel":
        patch = {
            field: body[field]
            for field in ("enabled", "chance", "in_groups", "in_channels")
            if field in body
        }
        config = await update_channel_config(scope_key, lambda current: current.update(patch))
        return web.json_response({"ok": True, "config": config})

    media_type = scope_key
    config = await get_media_config(media_type)
    for field in ("enabled", "chance"):
        if field in body:
            config[field] = body[field]
    await save_media_config(media_type, config)
    return web.json_response({"ok": True, "config": await get_media_config(media_type)})


async def handle_reset_channel_chat(request: web.Request):
    check_auth(request)
    scope, chat_id = parse_auto_scope(request.query.get("scope", ""))
    if scope != "channel" or chat_id is None:
        return web.Response(status=400, text="Некорректный ID чата")
    await redis_client.hdel(CHANNEL_CHAT_CONFIGS_KEY, chat_id)
    return web.json_response({"ok": True})


def change_auto_answer(config: dict, index: int, answer: dict | None) -> None:
    answers = config["answers"]
    if 0 <= index < len(answers):
        if answer is None:
            answers.pop(index)
        else:
            answers[index] = answer
    elif answer is not None:
        answers.append(answer)


async def handle_save_auto_answer(request: web.Request):
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

    scope, scope_key = parse_auto_scope(fields.get("scope", ""))
    if not scope:
        return web.Response(status=400, text="Неизвестный scope")

    try:
        index = int(fields.get("index", "-1"))
    except (TypeError, ValueError):
        index = -1

    try:
        buttons = clean_buttons(json.loads(fields.get("buttons", "[]")))
    except Exception:
        return web.Response(status=400, text="Некорректный список кнопок")

    media_type_saved = fields.get("existing_media_type", "text") or "text"
    file_id = fields.get("existing_file_id") or None

    if file_bytes and file_name:
        try:
            inp = BufferedInputFile(file_bytes, filename=file_name)
            if content_type.startswith("image/gif"):
                sent = await bot.send_animation(ADMIN_ID, inp)
                media_type_saved, file_id = "animation", sent.animation.file_id
            elif content_type.startswith("image/"):
                sent = await bot.send_photo(ADMIN_ID, inp)
                media_type_saved, file_id = "photo", sent.photo[-1].file_id
            elif content_type.startswith("video/"):
                sent = await bot.send_video(ADMIN_ID, inp)
                media_type_saved, file_id = "video", sent.video.file_id
            elif content_type.startswith("audio/"):
                sent = await bot.send_audio(ADMIN_ID, inp)
                media_type_saved, file_id = "audio", sent.audio.file_id
            else:
                sent = await bot.send_document(ADMIN_ID, inp)
                media_type_saved, file_id = "document", sent.document.file_id
            try:
                await bot.delete_message(ADMIN_ID, sent.message_id)
            except Exception:
                pass
        except Exception as e:
            return web.Response(status=502, text=f"Не удалось загрузить медиа: {e}")

    if media_type_saved != "text" and not file_id:
        media_type_saved = "text"

    answer = {
        "media_type": media_type_saved,
        "file_id": file_id,
        "html_text": fields.get("html_text", ""),
        "buttons": buttons,
    }

    if scope == "channel":
        await update_channel_config(scope_key, lambda config: change_auto_answer(config, index, answer))
    else:
        config = await get_media_config(scope_key)
        change_auto_answer(config, index, answer)
        await save_media_config(scope_key, config)
    return web.json_response({"ok": True})


async def handle_delete_auto_answer(request: web.Request):
    check_auth(request)
    scope, scope_key = parse_auto_scope(request.query.get("scope", ""))
    if not scope:
        return web.Response(status=400, text="Неизвестный scope")
    try:
        index = int(request.query.get("index", "-1"))
    except (TypeError, ValueError):
        index = -1

    if scope == "channel":
        await update_channel_config(scope_key, lambda config: change_auto_answer(config, index, None))
    else:
        config = await get_media_config(scope_key)
        if 0 <= index < len(config["answers"]):
            change_auto_answer(config, index, None)
            await save_media_config(scope_key, config)
    return web.json_response({"ok": True})


async def handle_save_settings(request: web.Request):
    check_auth(request)
    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="Некорректный JSON")
    if not isinstance(body, dict):
        return web.Response(status=400, text="Некорректный JSON")

    settings = await get_settings()
    for field in ("ignore_bots", "cooldown", "delay_min", "delay_max"):
        if field in body:
            settings[field] = body[field]
    await save_settings_db(settings)
    return web.json_response({"ok": True, "settings": await get_settings()})


async def handle_reset_stats(request: web.Request):
    check_auth(request)
    await reset_stats_db()
    return web.json_response({"ok": True, "stats": await get_stats()})


def build_web_app() -> web.Application:
    app = web.Application(client_max_size=50 * 1024 * 1024)
    app.router.add_get("/", handle_index)
    app.router.add_get("/ping", handle_ping)
    app.router.add_get("/api/triggers", handle_get_triggers)
    app.router.add_get("/api/export", handle_export_triggers)
    app.router.add_post("/api/triggers", handle_save_trigger)
    app.router.add_delete("/api/triggers", handle_delete_trigger)
    app.router.add_get("/api/auto", handle_get_auto)
    app.router.add_post("/api/auto/config", handle_save_auto_config)
    app.router.add_delete("/api/auto/channel-chat", handle_reset_channel_chat)
    app.router.add_post("/api/auto/answer", handle_save_auto_answer)
    app.router.add_delete("/api/auto/answer", handle_delete_auto_answer)
    app.router.add_post("/api/settings", handle_save_settings)
    app.router.add_post("/api/stats/reset", handle_reset_stats)
    return app


async def start_web_server():
    runner = web.AppRunner(build_web_app())
    await runner.setup()
    site = web.TCPSite(runner, WEB_HOST, WEB_PORT)
    await site.start()
    print(f"Веб-панель запущена на порту {WEB_PORT}")


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await migrate_legacy_triggers()
    except Exception as e:
        print(f"Не удалось выполнить миграцию старых записей: {e}")
    await start_web_server()
    try:
        await dp.start_polling(bot)
    finally:
        if _game_session is not None and not _game_session.closed:
            await _game_session.close()
        await bot.session.close()
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
