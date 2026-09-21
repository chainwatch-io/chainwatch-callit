"""
Launcher bot for the BTC Higher/Lower Mini App.

This bot's only job is to give users a button that opens the Mini App
(the FastAPI-served web page in app.py + static/index.html). The game
logic itself lives in the web app, not here.

Setup:
    export BOT_TOKEN="your-telegram-bot-token"
    export MINI_APP_URL="https://your-deployed-app-url.example.com"
    python bot.py
"""

import os
import logging
from telegram import Update, WebAppInfo, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
MINI_APP_URL = os.environ.get("MINI_APP_URL")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not MINI_APP_URL:
        await update.message.reply_text(
            "Mini app URL isn't configured yet. Set MINI_APP_URL once it's deployed."
        )
        return

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Play: Higher or Lower", web_app=WebAppInfo(url=MINI_APP_URL))]]
    )
    await update.message.reply_text(
        "Guess whether BTC will be higher or lower in the next round. "
        "Free to play, one guess per round, climb the leaderboard.",
        reply_markup=keyboard,
    )


async def play_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_cmd(update, context)


def main():
    if not BOT_TOKEN:
        raise SystemExit("Set the BOT_TOKEN environment variable first.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("play", play_cmd))
    log.info("Launcher bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
