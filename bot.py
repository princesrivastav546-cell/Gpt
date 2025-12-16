import os
import time
import secrets
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ParseMode
from telegram.ext import Application, MessageHandler, ContextTypes, filters

# ================= CONFIG =================
MAILTM_BASE = "https://api.mail.tm"
DB_PATH = "data.db"
POLL_EVERY_SECONDS = 12
PORT = int(os.environ.get("PORT", "10000"))

# ================= BUTTONS =================
BTN_NEW = "📧 New mail"
BTN_DELETE = "🗑 Delete mail"
BTN_HELP = "❓ Help"

MENU = ReplyKeyboardMarkup(
    [[BTN_NEW, BTN_DELETE], [BTN_HELP]],
    resize_keyboard=True
)

# ================= DATABASE =================
def init_db():
    db = sqlite3.connect(DB_PATH)
    c = db.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS mailbox (
            chat_id INTEGER PRIMARY KEY,
            address TEXT,
            token TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS seen (
            chat_id INTEGER,
            msg_id TEXT,
            PRIMARY KEY(chat_id, msg_id)
        )
    """)
    db.commit()
    db.close()

def save_mail(chat_id, address, token):
    db = sqlite3.connect(DB_PATH)
    db.execute("REPLACE INTO mailbox VALUES (?, ?, ?)", (chat_id, address, token))
    db.commit()
    db.close()

def get_mail(chat_id):
    db = sqlite3.connect(DB_PATH)
    row = db.execute("SELECT address, token FROM mailbox WHERE chat_id=?", (chat_id,)).fetchone()
    db.close()
    return row

def mark_seen(chat_id, msg_id):
    db = sqlite3.connect(DB_PATH)
    db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (chat_id, msg_id))
    db.commit()
    db.close()

def is_seen(chat_id, msg_id):
    db = sqlite3.connect(DB_PATH)
    row = db.execute("SELECT 1 FROM seen WHERE chat_id=? AND msg_id=?", (chat_id, msg_id)).fetchone()
    db.close()
    return row is not None

# ================= MAIL.TM =================
async def create_mail():
    async with httpx.AsyncClient() as client:
        d = (await client.get(f"{MAILTM_BASE}/domains")).json()["hydra:member"][0]["domain"]
        address = f"{secrets.token_hex(6)}@{d}"
        password = "pass1234"

        await client.post(f"{MAILTM_BASE}/accounts", json={"address": address, "password": password})
        token = (await client.post(f"{MAILTM_BASE}/token", json={"address": address, "password": password})).json()["token"]
        return address, token

async def fetch_messages(token):
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{MAILTM_BASE}/messages",
            headers={"Authorization": f"Bearer {token}"}
        )
        return r.json()["hydra:member"]

async def read_message(token, msg_id):
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{MAILTM_BASE}/messages/{msg_id}",
            headers={"Authorization": f"Bearer {token}"}
        )
        return r.json()

# ================= BOT =================
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    txt = update.message.text

    if txt == "/start":
        address, token = await create_mail()
        save_mail(chat_id, address, token)
        await update.message.reply_text(f"📧 {address}", reply_markup=MENU)
        return

    if txt == BTN_NEW:
        address, token = await create_mail()
        save_mail(chat_id, address, token)
        await update.message.reply_text(f"📧 {address}", reply_markup=MENU)
        return

    if txt == BTN_DELETE:
        save_mail(chat_id, "", "")
        await update.message.reply_text("Deleted", reply_markup=MENU)
        return

    if txt == BTN_HELP:
        await update.message.reply_text("Contact: @platoonleaderr", reply_markup=MENU)
        return

async def poll(context: ContextTypes.DEFAULT_TYPE):
    db = sqlite3.connect(DB_PATH)
    rows = db.execute("SELECT chat_id, token FROM mailbox WHERE token!=''").fetchall()
    db.close()

    for chat_id, token in rows:
        msgs = await fetch_messages(token)
        for m in msgs:
            if not is_seen(chat_id, m["id"]):
                full = await read_message(token, m["id"])
                text = full.get("text") or "(empty)"
                await context.bot.send_message(chat_id, text[:3500])
                mark_seen(chat_id, m["id"])

# ================= PORT SERVER =================
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def start_server():
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

# ================= MAIN =================
def main():
    init_db()
    threading.Thread(target=start_server, daemon=True).start()

    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(MessageHandler(filters.TEXT | filters.COMMAND, handle))
    app.job_queue.run_repeating(poll, interval=POLL_EVERY_SECONDS)
    app.run_polling()

if __name__ == "__main__":
    main()
