# -*- coding: utf-8 -*-
"""
Рендер карточки игрока NoomiNora в стиле веб-панели:
чёрный фон #050505, белый текст, Unbounded, зелёно-фиолетовые блобы, зерно.

Без превью скина (по решению владельца) — только статы, топы, премиум, аватар.
"""

import io
import math
import os
from datetime import datetime, timezone

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# --- ПАЛИТРА ВЕБ-ПАНЕЛИ (та же, что в WEB_HTML) ---
BG = (5, 5, 5)
INK = (245, 243, 238)
SOFT = (210, 208, 201)
MUTED = (139, 137, 129)
DIM = (95, 93, 87)
GREEN = (154, 230, 164)
VIOLET = (195, 199, 255)

CARD_W, CARD_H = 1080, 1350
MARGIN = 84

_HERE = os.path.dirname(os.path.abspath(__file__))
_FONT_PATHS = [
    os.path.join(_HERE, "fonts", "Unbounded.ttf"),
    os.path.join(_HERE, "Unbounded.ttf"),
    os.getenv("UNBOUNDED_FONT", ""),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
_FONT_FILE = next((p for p in _FONT_PATHS if p and os.path.exists(p)), None)
_IS_UNBOUNDED = bool(_FONT_FILE and "unbounded" in os.path.basename(_FONT_FILE).lower())

_font_cache: dict = {}


def _load_font(size: int, weight: int = 500) -> ImageFont.FreeTypeFont:
    key = (size, weight)
    if key in _font_cache:
        return _font_cache[key]
    font = ImageFont.truetype(_FONT_FILE, size)
    if _IS_UNBOUNDED:
        try:
            font.set_variation_by_axes([max(200, min(900, weight))])
        except Exception:
            pass
    _font_cache[key] = font
    return font


# --- УТИЛИТЫ ТЕКСТА ---


def _text_w(draw, text, font):
    return draw.textlength(text, font=font)


def _fit_font(draw, text, max_w, size, weight):
    f = _load_font(size, weight)
    while size > 24 and _text_w(draw, text, f) > max_w:
        size -= 2
        f = _load_font(size, weight)
    return f


def _tracked(draw, xy, text, font, fill, tracking=0, anchor="ls"):
    """Текст с межбуквенным интервалом (для надписей-капс)."""
    x, y = xy
    if anchor == "ms":
        total = sum(_text_w(draw, ch, font) + tracking for ch in text) - tracking
        x -= total / 2
    elif anchor == "rs":
        total = sum(_text_w(draw, ch, font) + tracking for ch in text) - tracking
        x -= total
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill, anchor="ls")
        x += _text_w(draw, ch, font) + tracking


# --- ФОРМАТЫ ---


def _fmt_hours(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "0"
    s = f"{v:.1f}".replace(".", ",")
    return s


def _fmt_int(v) -> str:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return "0"
    return f"{v:,}".replace(",", " ")


def _fmt_duration(sec) -> str:
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        sec = 0
    if sec < 0:
        sec = 0
    if sec < 60:
        return f"{sec} СЕК"
    if sec < 3600:
        return f"{sec // 60} МИН"
    if sec < 48 * 3600:
        h, m = sec // 3600, (sec % 3600) // 60
        return f"{h} Ч {m} МИН" if m else f"{h} Ч"
    d, h = sec // 86400, (sec % 86400) // 3600
    return f"{d} Д {h} Ч"


def _fmt_date(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt.strftime("%d.%m.%Y")
    except Exception:
        return ""


def _pill(draw, cx_or_x, y_center, text, font, fg, bg_fill, outline=None, pad_x=26, anchor="left") -> int:
    """Скруглённая кнопка. Возвращает ширину."""
    tw = _text_w(draw, text, font)
    th = font.size
    w = int(tw + pad_x * 2)
    h = int(th + 30)
    x0 = int(cx_or_x) if anchor == "left" else int(cx_or_x - w / 2)
    y0 = int(y_center - h / 2)
    draw.rounded_rectangle([x0, y0, x0 + w, y0 + h], radius=h // 2,
                           fill=bg_fill, outline=outline, width=2 if outline else 0)
    draw.text((x0 + w / 2, y_center), text, font=font, fill=fg, anchor="mm")
    return w


# --- ФОН: БЛОБЫ + ЗЕРНО (как .liquid и .grain в веб-панели) ---


def _radial_blob(size: int, color, opacity: float) -> Image.Image:
    grad = Image.radial_gradient("L").resize((size, size), Image.LANCZOS)
    mask = grad.point(lambda v: int(max(0, 255 - v * 1.9) * opacity))
    layer = Image.new("RGBA", (size, size), color + (255,))
    layer.putalpha(mask)
    return layer.filter(ImageFilter.GaussianBlur(size // 8))


def _grain(img: Image.Image, strength: float = 0.10) -> None:
    n = Image.effect_noise((img.width, img.height), 26)
    white_mask = n.point(lambda v: max(0, int((v - 128) * 2 * strength)))
    black_mask = n.point(lambda v: max(0, int((128 - v) * 2 * strength)))
    img.paste(Image.new("RGB", img.size, (255, 255, 255)), (0, 0), white_mask)
    img.paste(Image.new("RGB", img.size, (0, 0, 0)), (0, 0), black_mask)


def _base_canvas() -> Image.Image:
    img = Image.new("RGBA", (CARD_W, CARD_H), BG + (255,))
    img.alpha_composite(_radial_blob(900, GREEN, 0.16), (-330, -430))
    img.alpha_composite(_radial_blob(880, VIOLET, 0.13), (520, 800))
    img.alpha_composite(_radial_blob(420, GREEN, 0.05), (640, -160))
    return img


def _avatar_cover(src: Image.Image, box: int) -> Image.Image:
    w, h = src.size
    side = min(w, h)
    x0, y0 = (w - side) // 2, (h - side) // 2
    src = src.crop((x0, y0, x0 + side, y0 + side)).resize((box, box), Image.LANCZOS)
    mask = Image.new("L", (box * 2, box * 2), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, box * 2 - 1, box * 2 - 1], fill=255)
    mask = mask.resize((box, box), Image.LANCZOS)
    out = Image.new("RGBA", (box, box), (0, 0, 0, 0))
    out.paste(src, (0, 0), mask)
    return out


# --- ГЛАВНЫЙ РЕНДЕР ---


def render_profile_card(player: dict, avatar_bytes: bytes | None = None) -> bytes:
    """player — ответ GET /players/{account_id}. Возвращает PNG bytes."""
    img = _base_canvas()
    draw = ImageDraw.Draw(img)

    name = str(player.get("name") or "ИГРОК")
    account_id = player.get("account_id", 0)
    created = _fmt_date(player.get("created_at") or "")

    # Шапка
    y = 118
    _tracked(draw, (MARGIN, y), "NOOMINORA · ПРОФИЛЬ ИГРОКА", _load_font(21, 500), MUTED, tracking=7)

    y = 196
    name_font = _fit_font(draw, name, CARD_W - MARGIN * 2, 66, 650)
    draw.text((MARGIN, y), name, font=name_font, fill=INK, anchor="ls")
    y += name_font.size + 34

    sub = f"#{account_id}"
    if created:
        sub += f"   ·   В ИГРЕ С {created}"
    _tracked(draw, (MARGIN + 2, y), sub, _load_font(23, 400), MUTED, tracking=3)

    # Аватар (слева) + премиум (справа)
    av_box = 292
    av_x, av_y = MARGIN, 400
    cx, cy = av_x + av_box // 2, av_y + av_box // 2

    if avatar_bytes:
        try:
            photo = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
            av = _avatar_cover(photo, av_box)
            img.alpha_composite(av, (av_x, av_y))
        except Exception:
            avatar_bytes = None

    if not avatar_bytes:
        # Стеклянный круг с первой буквой ника
        draw.ellipse([av_x, av_y, av_x + av_box, av_y + av_box],
                     fill=(255, 255, 255, 14), outline=(255, 255, 255, 46), width=3)
        letter = name[:1].upper()
        draw.text((cx, cy), letter, font=_load_font(112, 600), fill=INK, anchor="mm")

    # Кольцо вокруг аватара
    draw.ellipse([av_x - 9, av_y - 9, av_x + av_box + 9, av_y + av_box + 9],
                 outline=(255, 255, 255, 56), width=3)

    # Премиум
    premium = bool(player.get("premium"))
    rx = av_x + av_box + 56
    if premium:
        font_p = _load_font(25, 600)
        label = "ПРЕМИУМ"
        tw = _text_w(draw, label, font_p)
        pw = int(50 + tw + 26)
        ph = int(font_p.size + 30)
        py0 = cy - 78 - ph // 2
        draw.rounded_rectangle([rx, py0, rx + pw, py0 + ph], radius=ph // 2,
                               fill=(245, 243, 238, 255))
        draw.ellipse([rx + 26, cy - 83, rx + 36, cy - 73], fill=GREEN)
        draw.text((rx + 50, cy - 78), label, font=font_p, fill=(5, 5, 5), anchor="lm")
    else:
        font_p = _load_font(25, 500)
        _pill(draw, rx, cy - 78, "БЕЗ ПРЕМИУМА", font_p, MUTED, (255, 255, 255, 10),
              outline=(255, 255, 255, 56))

    # Регистрация справа
    if created:
        _tracked(draw, (rx + 2, cy + 4), "РЕГИСТРАЦИЯ", _load_font(19, 500), DIM, tracking=5)
        draw.text((rx, cy + 46), created, font=_load_font(30, 500), fill=SOFT, anchor="ls")

    # Метрики: часы / перехваты / лучшая сессия + место в топе
    rows = [
        ("ЧАСЫ", _fmt_hours(player.get("hours")), player.get("rank"), player.get("total")),
        ("ПЕРЕХВАТЫ", _fmt_int(player.get("regrabs")), player.get("regrabs_rank"), player.get("regrabs_total")),
        ("ЛУЧШАЯ СЕССИЯ", _fmt_duration(player.get("longest")), player.get("longest_rank"), player.get("longest_total")),
    ]

    y0, row_h = 812, 148
    font_label = _load_font(20, 500)
    font_pill = _load_font(24, 600)

    for i, (label, value, rank, total) in enumerate(rows):
        ry = y0 + i * row_h
        if i:
            draw.line([MARGIN, ry - 12, CARD_W - MARGIN, ry - 12], fill=(255, 255, 255, 26), width=2)

        _tracked(draw, (MARGIN + 2, ry + 26), label, font_label, MUTED, tracking=6)
        draw.text((MARGIN, ry + 96), value, font=_load_font(52, 600), fill=INK, anchor="ls")

        # Пилюля места в топе
        try:
            rank_i, total_i = int(rank), int(total)
        except (TypeError, ValueError):
            rank_i, total_i = 0, 0
        if rank_i > 0 and total_i > 0:
            pill = f"#{rank_i} ИЗ {total_i}"
            fw = _text_w(draw, pill, font_pill)
            pw = int(fw + 52)
            px1 = CARD_W - MARGIN
            px0 = px1 - pw
            py0 = ry + 40
            draw.rounded_rectangle([px0, py0, px1, py0 + 62], radius=31,
                                   fill=(245, 243, 238, 255))
            draw.text((px1 - 26, py0 + 31), pill, font=font_pill, fill=(5, 5, 5), anchor="rm")
            # акцент топ-3
            if rank_i <= 3:
                draw.ellipse([px0 + 24, py0 + 27, px0 + 34, py0 + 37], fill=(5, 5, 5))

    # Подвал
    _tracked(draw, (CARD_W / 2, CARD_H - 64), "ЧАСЫ · ПЕРЕХВАТЫ · СЕССИИ · NOOMINORA",
             _load_font(16, 500), DIM, tracking=6, anchor="ms")

    _grain(img)
    out = img.convert("RGB")
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def render_error_card(title: str, subtitle: str = "", tag: str = "") -> bytes:
    """Карточка ошибки в том же стиле (игрок не найден / нет привязки / сервер молчит)."""
    img = _base_canvas()
    draw = ImageDraw.Draw(img)

    _tracked(draw, (MARGIN, 130), tag or "NOOMINORA · ПРОФИЛЬ", _load_font(21, 500), MUTED, tracking=7)

    t_font = _fit_font(draw, title, CARD_W - MARGIN * 2, 62, 650)
    draw.text((CARD_W / 2, 620), title, font=t_font, fill=INK, anchor="mm")

    if subtitle:
        s_font = _load_font(24, 400)
        # перенос до 2 строк
        words, lines, cur = subtitle.split(), [], ""
        for w_ in words:
            trial = (cur + " " + w_).strip()
            if _text_w(draw, trial, s_font) > CARD_W - MARGIN * 2 - 40 and cur:
                lines.append(cur)
                cur = w_
            else:
                cur = trial
        if cur:
            lines.append(cur)
        ty = 700
        for ln in lines[:3]:
            draw.text((CARD_W / 2, ty), ln, font=s_font, fill=MUTED, anchor="mm")
            ty += 44

    # белая кнопка-разделитель
    draw.rounded_rectangle([CARD_W / 2 - 120, 780, CARD_W / 2 + 120, 786], radius=3,
                           fill=(245, 243, 238, 70))

    _grain(img)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()


if __name__ == "__main__":
    # Быстрый локальный тест: python profile_card.py
    demo = {
        "account_id": 729, "name": "MarkBroy8352", "hours": 47.4986,
        "regrabs": 168, "longest": 114893, "premium": True,
        "rank": 1, "total": 2260, "regrabs_rank": 337, "regrabs_total": 2260,
        "longest_rank": 1, "longest_total": 2260,
        "created_at": "2026-11-24T15:16:30Z",
    }
    png = render_profile_card(demo, None)
    with open("demo_card.png", "wb") as f:
        f.write(png)
    print("OK demo_card.png", len(png))
