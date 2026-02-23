import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Я бот помощи по ПК.\n\n"
        "Пришли скрин ошибки."
    )

def keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👨‍💻 Живая поддержка", callback_data="live_support")]
    ])

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Скрин получен.\nНажми кнопку для живой поддержки.",
        reply_markup=keyboard()
    )

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "live_support":
        user = query.from_user
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🆘 Заявка!\nUser: @{user.username}\nID: {user.id}"
        )
        await query.edit_message_text("Заявка отправлена.")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.run_polling()

if __name__ == "__main__":
    main()
