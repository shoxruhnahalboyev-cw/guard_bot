import asyncio
import io
import logging
import os
import re
import os
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command, CommandStart
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BufferedInputFile,
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from dotenv import load_dotenv
from openpyxl import Workbook
from openpyxl.styles import Font

import database as db

# .env faylidan sozlamalarni o'qiymiz
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Xatolar va jarayonni terminalda ko'rish uchun
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ==========================================
# NAZORAT SOZLAMALARI
# ==========================================

# Haqoratli / taqiqlangan so'zlar (kichik harflarda yozing)
BAD_WORDS: set[str] = {
    "axmoq",
    "tentak",
    "eshak",
    "hayvon",
    "iflos",
    "haromi",
    "lanati",
    "ahmoq",
    # O'zingiz xohlagan so'zlarni shu yerga qo'shing
}

# Reklama / nojo'ya matn belgilari (ixtiyoriy kengaytirish mumkin)
SPAM_KEYWORDS: set[str] = {
    "reklama",
    "aksiya",
    "skidka",
    "pul ishlash",
    "kazino",
    "1xbet",
    "stavka",
    "obuna bo'ling",
    "kanalimiz",
}

# Agar guruhda rasm, video, GIF, stiker yoki ovozli xabarlarni ham
# "nojo'ya/keraksiz" deb hisoblab ban qilmoqchi bo'lsangiz True qiling:
BAN_PHOTOS = False
BAN_VIDEOS = False
BAN_ANIMATIONS = False   # GIF
BAN_STICKERS = False
BAN_VOICE = False
BAN_FORWARD_FROM_CHANNELS = True  # Begona kanal/guruhdan forward qilingan reklamalarni bloklash


# ==========================================
# KLAVIATURALAR
# ==========================================

phone_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📱 Raqamni yuborish", request_contact=True)]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

admin_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="📊 Statistika", callback_data="stat")],
        [InlineKeyboardButton(text="👥 Oxirgi 20 ta foydalanuvchi", callback_data="users")],
        [InlineKeyboardButton(text="📥 Hammasini Excel'ga yuklash", callback_data="export")],
    ]
)


# ==========================================
# YORDAMCHI FUNKSIYALAR (MODERATSIYA / BAN)
# ==========================================

URL_REGEX = re.compile(
    r"(https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+|telegram\.dog/\S+|@[a-zA-Z0-9_]{4,})",
    re.IGNORECASE,
)


async def is_chat_admin(chat_id: int, user_id: int) -> bool:
    """Foydalanuvchi asosiy admin yoki shu guruh admini ekanligini tekshiradi."""
    if user_id == ADMIN_ID:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in {
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.ADMINISTRATOR,
        }
    except Exception:
        return False


def find_bad_word(text: str) -> str | None:
    """Matnda taqiqlangan so'z bor-yo'qligini tekshiradi."""
    if not text:
        return None
    normalized = re.sub(r"[^a-zа-яёўқғҳ0-9\s]", " ", text.lower())
    words = normalized.split()
    for word in words:
        for bad in BAD_WORDS:
            if word == bad or (len(bad) >= 4 and word.startswith(bad)):
                return bad
    return None


def has_spam_or_link(message: Message) -> str | None:
    """Havola, reklama yoki taqiqlangan forward xabarni aniqlaydi."""
    combined_text = (message.text or "") + " " + (message.caption or "")

    # 1. Message entities (url, text_link, mention)
    entities = list(message.entities or []) + list(message.caption_entities or [])
    for entity in entities:
        if entity.type in {"url", "text_link"}:
            return "havola (link)"

    # 2. Regex orqali havola yoki @username reklama
    if URL_REGEX.search(combined_text):
        return "havola yoki reklama"

    # 3. Reklama kalit so'zlari
    lower_text = combined_text.lower()
    for kw in SPAM_KEYWORDS:
        if kw in lower_text:
            return f"reklama ({kw})"

    # 4. Begona kanal yoki guruhdan forward qilingan xabarlar
    if BAN_FORWARD_FROM_CHANNELS and message.forward_origin:
        origin_type = getattr(message.forward_origin, "type", None)
        if origin_type in {"channel", "chat"}:
            return "begona manbadan forward reklama"

    return None


def check_inappropriate_media(message: Message) -> str | None:
    """Sozlamaga ko'ra taqiqlangan media turlarini tekshiradi."""
    if BAN_PHOTOS and message.photo:
        return "rasm"
    if BAN_VIDEOS and message.video:
        return "video"
    if BAN_ANIMATIONS and message.animation:
        return "GIF"
    if BAN_STICKERS and message.sticker:
        return "stiker"
    if BAN_VOICE and (message.voice or message.audio):
        return "ovozli/audio xabar"
    return None


async def delete_and_ban(message: Message, reason: str) -> None:
    """Xabarni o'chiradi va foydalanuvchini guruhdan chiqarib, ban qiladi."""
    user = message.from_user
    if not user:
        return

    chat_id = message.chat.id
    user_id = user.id

    # Adminlarni ban qilmaymiz
    if await is_chat_admin(chat_id, user_id):
        return

    # 1. Xabarni o'chirish
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    except Exception as e:
        logger.warning(f"Xabarni o'chirishda xato: {e}")

    # 2. Guruhdan chiqarib, ban qilish (revoke_messages=True -> shu userning barcha xabarlarini ham tozalaydi)
    try:
        await bot.ban_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            revoke_messages=True,
        )
    except TelegramForbiddenError:
        logger.error(
            f"Guruhda ({chat_id}) ban qilish huquqi yo'q! Botni admin qiling."
        )
        try:
            await message.answer(
                "⚠️ Meni guruhda **Admin** qiling (xabar o'chirish va foydalanuvchini bloklash huquqi bilan), "
                "aks holda qoidabuzarlarni ban qila olmayman!",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return
    except TelegramBadRequest as e:
        logger.warning(f"Ban qilishda TelegramBadRequest ({user_id}): {e}")
        return
    except Exception as e:
        logger.error(f"Ban qilishda kutilmagan xato: {e}")
        return

    # 3. Guruhda qisqa va aniq xabar berish
    mention = f"@{user.username}" if user.username else user.first_name
    try:
        await message.answer(
            f"🚫 **{mention}** guruh qoidalarini buzgani (**{reason}**) sababli guruhdan chiqarildi va **BAN** qilindi!",
            parse_mode="Markdown",
        )
    except Exception:
        pass


# ==========================================
# BOT GURUHGA QO'SHILGANDA / HOLATI O'ZGARGANDA
# ==========================================

@dp.my_chat_member()
async def on_bot_member_update(event: ChatMemberUpdated):
    if event.chat.type not in {"group", "supergroup"}:
        return

    new_status = event.new_chat_member.status
    old_status = event.old_chat_member.status

    # Bot guruhga yangi qo'shilganda
    if old_status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED} and new_status in {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
    }:
        if new_status == ChatMemberStatus.ADMINISTRATOR:
            await bot.send_message(
                event.chat.id,
                "👮 **Assalomu alaykum! Men guruh nazoratchisiman.**\n\n"
                "Endi guruhni qattiq nazorat qilaman:\n"
                "• Haqoratli so'z yozganlar -> **BAN**\n"
                "• Reklama va havola tashlaganlar -> **BAN**\n"
                "• Nojo'ya xabar yuborganlar -> **BAN**\n\n"
                "Qoidalar bilan tanishish uchun: /rules",
                parse_mode="Markdown",
            )
        else:
            await bot.send_message(
                event.chat.id,
                "👮 **Assalomu alaykum! Men guruh nazoratchisiman.**\n\n"
                "⚠️ Guruhni to'liq nazorat qilishim, xabarlarni o'chirishim va qoidabuzarlarni **BAN** qilishim uchun "
                "meni **Administrator** qilib tayinlang!",
                parse_mode="Markdown",
            )

    # Bot guruhda admin qilinganda
    elif old_status != ChatMemberStatus.ADMINISTRATOR and new_status == ChatMemberStatus.ADMINISTRATOR:
        await bot.send_message(
            event.chat.id,
            "✅ **Rahmat! Menga admin huquqi berildi.**\n"
            "Guruh nazorati faol — haqorat, reklama va nojo'ya xabarlarga darhol **BAN** beriladi!",
            parse_mode="Markdown",
        )


# ==========================================
# ASOSIY BUYRUQLAR
# ==========================================

@dp.message(CommandStart(), F.chat.type == "private")
async def start_private(message: Message):
    user = message.from_user

    try:
        chat = await bot.get_chat(user.id)
        bio = chat.bio
    except Exception:
        bio = None

    db.add_user(user.id, user.first_name, user.last_name, user.username, bio)

    if db.has_phone(user.id):
        await message.answer(f"Salom, {user.first_name}! Siz allaqachon ro'yxatdan o'tgansiz ✅")
    else:
        await message.answer(
            f"Salom, {user.first_name}! 👮 Men guruh nazoratchisiman.\n\n"
            "Ro'yxatdan o'tish uchun telefon raqamingizni yuboring 👇",
            reply_markup=phone_kb,
        )


@dp.message(CommandStart(), F.chat.type.in_({"group", "supergroup"}))
async def start_group(message: Message):
    await message.answer("👮 Men shu guruhni nazorat qilaman! Haqorat, reklama va nojo'ya xabarlarga BAN beriladi.")


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "👮 Men guruh nazoratchisiman.\n\n"
        "/start - botni ishga tushirish\n"
        "/rules - guruh qoidalari\n"
        "/help - yordam"
    )


@dp.message(Command("rules"))
async def rules_cmd(message: Message):
    await message.answer(
        "📋 **Guruh qoidalari**\n\n"
        "1. Bir-biringizni hurmat qiling\n"
        "2. Reklama va begona havolalar taqiqlanadi (darhol **BAN**)\n"
        "3. Haqoratli va so'kinish so'zlar ishlatmang (darhol **BAN**)\n"
        "4. Nojo'ya xabarlar yubormang (darhol **BAN**)",
        parse_mode="Markdown",
    )


@dp.message(F.new_chat_members)
async def welcome(message: Message):
    for user in message.new_chat_members:
        if user.id == bot.id:
            continue
        await message.answer(
            f"Xush kelibsiz, {user.first_name}! 👋\n"
            "Guruhda haqorat, reklama va nojo'ya xabarlar taqiqlangan (qoidabuzarlar darhol **BAN** qilinadi).\n"
            "Qoidalar: /rules",
            parse_mode="Markdown",
        )


# ==========================================
# GURUH NAZORATI (HAQORAT, REKLAMA, NOJO'YA XABAR -> BAN)
# ==========================================

@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def moderate_group_messages(message: Message):
    if not message.from_user or message.from_user.is_bot:
        return

    # 1. Haqoratli so'zlarni tekshirish
    text_to_check = (message.text or "") + " " + (message.caption or "")
    bad_word = find_bad_word(text_to_check)
    if bad_word:
        await delete_and_ban(message, f"haqoratli so'z: {bad_word}")
        return

    # 2. Reklama, havola va begona forwardlarni tekshirish
    spam_reason = has_spam_or_link(message)
    if spam_reason:
        await delete_and_ban(message, spam_reason)
        return

    # 3. Taqiqlangan / nojo'ya media turlarini tekshirish
    media_reason = check_inappropriate_media(message)
    if media_reason:
        await delete_and_ban(message, f"nojo'ya {media_reason}")
        return


# ==========================================
# SHAXSIY CHAT, KONTAKT VA ADMIN PANEL
# ==========================================

@dp.message(F.contact, F.chat.type == "private")
async def get_contact(message: Message):
    contact = message.contact

    if contact.user_id != message.from_user.id:
        await message.answer("❌ Iltimos, o'zingizning raqamingizni yuboring.")
        return

    db.set_phone(message.from_user.id, contact.phone_number)
    await message.answer(
        "✅ Rahmat, ro'yxatdan o'tdingiz!",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(Command("admin"), F.from_user.id == ADMIN_ID, F.chat.type == "private")
async def admin_panel(message: Message):
    await message.answer("🛠 Admin panel", reply_markup=admin_kb)


@dp.callback_query(F.data == "stat", F.from_user.id == ADMIN_ID)
async def show_stat(call: CallbackQuery):
    await call.message.answer(
        "📊 Statistika\n\n"
        f"Jami foydalanuvchilar: {db.count_users()}\n"
        f"Raqam yuborganlar: {db.count_with_phone()}\n"
        f"Bugun qo'shilganlar: {db.count_today()}"
    )
    await call.answer()


@dp.callback_query(F.data == "users", F.from_user.id == ADMIN_ID)
async def show_users(call: CallbackQuery):
    users = db.last_users()
    if not users:
        await call.message.answer("Hali hech kim yo'q.")
    else:
        text = "👥 Oxirgi foydalanuvchilar\n\n"
        for i, (uid, fn, ln, un, phone, joined) in enumerate(users, 1):
            ism = f"{fn or ''} {ln or ''}".strip()
            un = f"@{un}" if un else "—"
            line = f"{i}. {ism} | {un} | {phone or '—'} | {joined}\n"
            if len(text) + len(line) > 2000:
                await call.message.answer(text)
                text = ""
            text += line
        if text:
            await call.message.answer(text)
    await call.answer()


@dp.callback_query(F.data == "export", F.from_user.id == ADMIN_ID)
async def export_users(call: CallbackQuery):
    wb = Workbook()
    ws = wb.active
    ws.title = "Foydalanuvchilar"

    ws.append(["ID", "Ism", "Familiya", "Username", "Telefon", "Bio", "Sana"])
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for row in db.all_users():
        ws.append(list(row))

    widths = [14, 15, 15, 18, 16, 30, 18]
    for i, w in enumerate(widths):
        ws.column_dimensions[chr(65 + i)].width = w

    buffer = io.BytesIO()
    wb.save(buffer)

    await call.message.answer_document(
        BufferedInputFile(buffer.getvalue(), filename="foydalanuvchilar.xlsx"),
        caption=f"Jami: {db.count_users()} ta",
    )
    await call.answer()


@dp.message(F.chat.type == "private")
async def echo(message: Message):
    if message.text:
        await message.answer(f"Siz yozdingiz: {message.text}")
    else:
        await message.answer("Men faqat matn xabarlarini tushunaman 😊")


# ==========================================
# BUYRUQ MENYULARI VA ISHGA TUSHIRISH
# ==========================================

async def set_commands():
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Botni ishga tushirish"),
            BotCommand(command="help", description="Yordam"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )

    await bot.set_my_commands(
        [
            BotCommand(command="rules", description="Guruh qoidalari"),
            BotCommand(command="help", description="Yordam"),
        ],
        scope=BotCommandScopeAllGroupChats(),
    )

    if ADMIN_ID:
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Botni ishga tushirish"),
                    BotCommand(command="help", description="Yordam"),
                    BotCommand(command="admin", description="Admin panel"),
                ],
                scope=BotCommandScopeChat(chat_id=ADMIN_ID),
            )
        except Exception as e:
            logger.warning(f"Admin menyusini o'rnatib bo'lmadi (ADMIN_ID={ADMIN_ID}): {e}")


async def health(request):
    return web.Response(text="OK")


async def start_http_server():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


async def main():
    db.create_tables()
    await set_commands()
    await start_http_server()
    logger.info("Bot ishga tushdi...")
    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types(),
    )


if __name__ == "__main__":
    asyncio.run(main())