import os
import re
import json
import time
import logging
from dataclasses import dataclass
from typing import List, Optional, Dict, Any

import numpy as np
from PIL import Image, ImageOps
import easyocr

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ----------------------------
# CONFIG (Railway Variables)
# ----------------------------
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Лимит бесплатных "разборов" в день на человека
FREE_LIMIT_PER_DAY = int(os.getenv("FREE_LIMIT_PER_DAY", "3"))

# Включить OCR (1/0)
USE_OCR = os.getenv("USE_OCR", "1") == "1"

# Таймзона для сброса лимитов (МСК = UTC+3)
TZ_OFFSET_SECONDS = 3 * 3600

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pcfixer")

# OCR reader
READER = easyocr.Reader(["en", "ru"], gpu=False) if USE_OCR else None

# ----------------------------
# "DB" в памяти (для MVP)
# ----------------------------
# Важно: это хранится в памяти контейнера.
# При редеплое/перезапуске может сброситься — но для старта норм.
STATE: Dict[str, Any] = {
    "quota": {},  # user_id -> {"day": "YYYY-MM-DD", "used": int}
    "last": {},   # user_id -> {"photo_msg_id": int, "ocr": str, "case": str}
}

def today_key() -> str:
    # день считаем по МСК
    ts = int(time.time()) + TZ_OFFSET_SECONDS
    return time.strftime("%Y-%m-%d", time.gmtime(ts))

def quota_get(user_id: int) -> Dict[str, Any]:
    q = STATE["quota"].get(str(user_id))
    if not q or q.get("day") != today_key():
        q = {"day": today_key(), "used": 0}
        STATE["quota"][str(user_id)] = q
    return q

def quota_can_use(user_id: int) -> bool:
    q = quota_get(user_id)
    return q["used"] < FREE_LIMIT_PER_DAY

def quota_use(user_id: int) -> None:
    q = quota_get(user_id)
    q["used"] += 1

# ----------------------------
# Knowledge base (правила)
# ----------------------------
@dataclass
class KBItem:
    id: str
    title: str
    patterns: List[str]
    steps: List[str]
    ask: List[str]

KB: List[KBItem] = [
    KBItem(
        id="win_auto_repair",
        title="Windows: автоматическое восстановление / boot-loop",
        patterns=[
            r"automatic repair",
            r"startup repair",
            r"diagnosing your pc",
            r"автоматическое восстановление",
            r"подготовка автоматического восстановления",
        ],
        steps=[
            "1) отключи **все USB** (флешки/хабы), оставь только зарядку.",
            "2) **Устранение неполадок → Доп. параметры → Восстановление системы** (если есть точка).",
            "3) если точек нет: **Удалить последние обновления** (сначала качественные, потом функциональные).",
            "4) **Параметры загрузки → Безопасный режим**. Если загрузился — удали последние драйверы/твики/антивирус.",
            "5) если упор в диск/тома — проверь в BIOS режим **AHCI/RAID/VMD** и не менялся ли он.",
        ],
        ask=[
            "модель ноутбука?",
            "windows 10 или 11?",
            "что было перед проблемой (обновление/игра/резко вырубился)?",
        ],
    ),
    KBItem(
        id="no_bootable",
        title="BIOS/UEFI: не найден загрузочный диск (No bootable device)",
        patterns=[
            r"no bootable device",
            r"boot device not found",
            r"reboot and select proper boot device",
            r"операционная система не найдена",
        ],
        steps=[
            "1) BIOS → Boot: проверь **Windows Boot Manager**. Если есть — поставь **первым**.",
            "2) если диск виден, но Boot Manager пропал — возможно слетела загрузочная запись.",
            "3) выключи ноут → отключи флешки/SD/внешние диски → включи снова.",
            "4) если есть флешка Windows: **Восстановление при загрузке**.",
            "5) если флешки нет — дальше реально упираемся в создание флешки.",
        ],
        ask=[
            "диск виден в BIOS? как называется (NVMe/SSD модель)?",
            "есть ли Windows Boot Manager в Boot меню?",
        ],
    ),
    KBItem(
        id="inaccessible_boot_device",
        title="BSOD: INACCESSIBLE_BOOT_DEVICE",
        patterns=[
            r"inaccessible_boot_device",
            r"недоступное загрузочное устройство",
        ],
        steps=[
            "1) частая причина — режим контроллера (**AHCI/RAID/VMD**). проверь, не менял ли в BIOS.",
            "2) если менял — верни как было.",
            "3) если началось после обновления: Recovery → **Удалить последние обновления**.",
            "4) если есть флешка: восстановление запуска + командная строка (дальше по ситуации).",
        ],
        ask=[
            "intel или amd? есть ли VMD/RAID в BIOS?",
            "менял ли что-то в BIOS перед проблемой?",
        ],
    ),
]

# ----------------------------
# UI helpers
# ----------------------------
def kb_keyboard(can_analyze: bool = True) -> InlineKeyboardMarkup:
    rows = []
    if can_analyze:
        rows.append([InlineKeyboardButton("🧠 Разобрать скрин", callback_data="analyze")])
    rows.append([InlineKeyboardButton("👨‍💻 Живая поддержка", callback_data="live_support")])
    rows.append([InlineKeyboardButton("📌 Как прислать правильно", callback_data="howto")])
    return InlineKeyboardMarkup(rows)

def pretty_header(user_id: int) -> str:
    q = quota_get(user_id)
    left = max(0, FREE_LIMIT_PER_DAY - q["used"])
    return f"🛠️ *PCFixer*\nБесплатных разборов сегодня: *{left}/{FREE_LIMIT_PER_DAY}*"

def preprocess(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    w, h = img.size
    # чуть увеличим для OCR
    img = img.resize((w * 2, h * 2))
    return img

def ocr_text_from_path(path: str) -> str:
    if not READER:
        return ""
    img = Image.open(path)
    img = preprocess(img)
    arr = np.array(img)
    lines = READER.readtext(arr, detail=0)
    text = " ".join(lines).strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text

def match_kb(text: str) -> Optional[KBItem]:
    if not text:
        return None
    for item in KB:
        for pat in item.patterns:
            if re.search(pat, text, re.IGNORECASE):
                return item
    return None

def build_solution(item: KBItem) -> str:
    out = [f"✅ *{item.title}*", ""]
    out += [f"• {s}" for s in item.steps]
    out += ["", "Если не помогло — напиши ответы на 2–3 вопроса, я продолжу."]
    return "\n".join(out)

# ----------------------------
# Handlers
# ----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    msg = (
        f"{pretty_header(uid)}\n\n"
        "Пришли *скрин/фото ошибки* (крупно, без бликов).\n"
        "Я попробую распознать текст и дать шаги.\n\n"
        "⚠️ Не присылай пароли/ключи/номера карт."
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_keyboard(can_analyze=False))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📌 *Как прислать правильно:*\n"
        "1) чтобы текст был читаемым (лучше ближе)\n"
        "2) одним сообщением допиши: *Windows 10/11* и *модель ноутбука*\n"
        "3) что делал перед ошибкой\n\n"
        "Если хочешь — нажми *Живая поддержка*."
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_keyboard(can_analyze=False))

async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *PCFixer* — бот первой линии поддержки.\n"
        "Скрин → распознавание → чеклист действий.\n\n"
        "Команды:\n"
        "/start /help /about",
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    # сохраним message_id фото, чтобы переслать админу при поддержке
    STATE["last"][str(uid)] = {"photo_msg_id": update.message.message_id, "ocr": "", "case": ""}

    # скачиваем файл
    photo = update.message.photo[-1]
    tg_file = await photo.get_file()
    os.makedirs("tmp", exist_ok=True)
    path = f"tmp/{update.message.message_id}.jpg"
    await tg_file.download_to_drive(path)

    # OCR (если включён)
    text = ""
    if USE_OCR:
        try:
            text = ocr_text_from_path(path)
        except Exception as e:
            log.warning("OCR failed: %s", e)
            text = ""

    item = match_kb(text)
    STATE["last"][str(uid)]["ocr"] = text
    STATE["last"][str(uid)]["case"] = item.id if item else ""

    msg = (
        f"{pretty_header(uid)}\n\n"
        "✅ Скрин получил.\n"
        "Нажми *🧠 Разобрать скрин*, и я дам пошаговое решение.\n"
        "Если срочно — жми *Живая поддержка*."
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_keyboard(can_analyze=True))

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if data == "howto":
        await q.edit_message_text(
            "📌 Пришли скрин так, чтобы текст ошибки был читаемый.\n"
            "И допиши: Windows 10/11 + модель ноутбука + что было перед ошибкой.\n\n"
            "Если хочешь — жми «Живая поддержка».",
            reply_markup=kb_keyboard(can_analyze=False)
        )
        return

    if data == "analyze":
        # лимит
        if not quota_can_use(uid):
            await q.edit_message_text(
                f"{pretty_header(uid)}\n\n"
                "⛔️ Лимит бесплатных разборов на сегодня закончился.\n"
                "Хочешь — нажми «Живая поддержка».",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_keyboard(can_analyze=False)
            )
            return

        quota_use(uid)

        last = STATE["last"].get(str(uid), {})
        ocr = (last.get("ocr") or "").strip()
        case_id = last.get("case") or ""

        item = next((x for x in KB if x.id == case_id), None) if case_id else None
        if item:
            await q.edit_message_text(
                f"{pretty_header(uid)}\n\n" + build_solution(item),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_keyboard(can_analyze=False)
            )
            return

        # если не нашли шаблон
        await q.edit_message_text(
            f"{pretty_header(uid)}\n\n"
            "Я не нашёл точное совпадение по ошибке.\n\n"
            "Сделай так:\n"
            "• пришли ещё один скрин ближе (где текст крупнее)\n"
            "ИЛИ\n"
            "• напиши текст ошибки вручную и укажи Windows 10/11.\n\n"
            "Если срочно — «Живая поддержка».",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_keyboard(can_analyze=False)
        )
        return

    if data == "live_support":
        if ADMIN_ID == 0:
            await q.edit_message_text("Админ не настроен (ADMIN_ID).")
            return

        # Сформируем заявку админу
        last = STATE["last"].get(str(uid), {})
        ocr = (last.get("ocr") or "").strip()
        case_id = last.get("case") or "unknown"
        photo_msg_id = last.get("photo_msg_id")

        user = q.from_user
        text = (
            "🆘 *Заявка на поддержку*\n\n"
            f"👤 @{user.username or 'нет'}\n"
            f"🆔 {user.id}\n"
            f"📌 case: `{case_id}`\n\n"
            f"📝 OCR:\n`{ocr[:1200] if ocr else 'нет текста'}`"
        )
        await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode=ParseMode.MARKDOWN)

        # Перешлём фото админу (самый надёжный способ)
        try:
            if photo_msg_id:
                await context.bot.forward_message(
                    chat_id=ADMIN_ID,
                    from_chat_id=q.message.chat_id,
                    message_id=photo_msg_id
                )
        except Exception as e:
            log.warning("forward photo failed: %s", e)

        await q.edit_message_text(
            "✅ Заявка отправлена.\n\n"
            "Пока напиши одним сообщением:\n"
            "• модель ноутбука\n"
            "• Windows 10/11\n"
            "• что делал перед ошибкой\n",
            reply_markup=kb_keyboard(can_analyze=False)
        )
        return

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Если человек прислал текст ошибки — попробуем матчить
    uid = update.effective_user.id
    text = (update.message.text or "").strip().lower()
    if not text:
        return

    item = match_kb(text)
    if item:
        # засчитаем как разбор
        if not quota_can_use(uid):
            await update.message.reply_text(
                f"{pretty_header(uid)}\n\n"
                "⛔️ Лимит бесплатных разборов на сегодня закончился.\n"
                "Жми «Живая поддержка», если срочно.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_keyboard(can_analyze=False)
            )
            return
        quota_use(uid)
        await update.message.reply_text(
            f"{pretty_header(uid)}\n\n" + build_solution(item),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_keyboard(can_analyze=False)
        )
        return

    await update.message.reply_text(
        f"{pretty_header(uid)}\n\n"
        "Понял. Чтобы я точнее попал:\n"
        "• пришли скрин (крупно)\n"
        "• или напиши точный текст ошибки + Windows 10/11\n\n"
        "Если срочно — «Живая поддержка».",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_keyboard(can_analyze=False)
    )

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в переменных окружения")
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("about", about_cmd))

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
