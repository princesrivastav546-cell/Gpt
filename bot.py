import os
import secrets
import sqlite3
from typing import Optional

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

MAILTM_BASE = "https://api.mail.tm"  # official API host 2

DB_PATH = "data.db"


def init_db() -> None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS mailboxes (
            chat_id INTEGER PRIMARY KEY,
            address TEXT NOT NULL,
            password TEXT NOT NULL,
            token TEXT NOT NULL
        )
        """
    )
    con.commit()
    con.close()


def db_get(chat_id: int) -> Optional[tuple[str, str, str]]:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT address, password, token FROM mailboxes WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    con.close()
    return row if row else None


def db_upsert(chat_id: int, address: str, password: str, token: str) -> None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO mailboxes(chat_id, address, password, token)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
          address=excluded.address,
          password=excluded.password,
          token=excluded.token
        """,
        (chat_id, address, password, token),
    )
    con.commit()
    con.close()


def db_delete(chat_id: int) -> None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM mailboxes WHERE chat_id=?", (chat_id,))
    con.commit()
    con.close()


async def mailtm_get_random_domain(client: httpx.AsyncClient) -> str:
    # Mail.tm exposes domains; choose one active domain 3
    r = await client.get(f"{MAILTM_BASE}/domains?page=1")
    r.raise_for_status()
    items = r.json().get("hydra:member", [])
    if not items:
        raise RuntimeError("No mail.tm domains available right now.")
    # pick first active domain
    for d in items:
        if d.get("isActive"):
            return d["domain"]
    return items[0]["domain"]


async def mailtm_create_account_and_token(client: httpx.AsyncClient) -> tuple[str, str, str]:
    domain = await mailtm_get_random_domain(client)
    local = secrets.token_hex(6)
    address = f"{local}@{domain}"
    password = secrets.token_urlsafe(12)

    # Create account: POST /accounts 4
    r1 = await client.post(f"{MAILTM_BASE}/accounts", json={"address": address, "password": password})
    # if address collision, retry once
    if r1.status_code >= 400:
        r1 = await client.post(
            f"{MAILTM_BASE}/accounts",
            json={"address": f"{secrets.token_hex(7)}@{domain}", "password": password},
        )
    r1.raise_for_status()
    address = r1.json()["address"]

    # Get JWT token: POST /token 5
    r2 = await client.post(f"{MAILTM_BASE}/token", json={"address": address, "password": password})
    r2.raise_for_status()
    token = r2.json()["token"]
    return address, password, token


async def mailtm_list_messages(client: httpx.AsyncClient, token: str):
    r = await client.get(
        f"{MAILTM_BASE}/messages?page=1",
        headers={"Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    return r.json().get("hydra:member", [])


async def mailtm_read_message(client: httpx.AsyncClient, token: str, msg_id: str):
    r = await client.get(
        f"{MAILTM_BASE}/messages/{msg_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    return r.json()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Disposable Mail Bot\n\n"
        "Commands:\n"
        "/new - create a new temp inbox\n"
        "/inbox - list emails\n"
        "/read <id> - read an email\n"
        "/forget - remove your stored inbox"
    )


async def new_mail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    async with httpx.AsyncClient(timeout=20) as client:
        address, password, token = await mailtm_create_account_and_token(client)

    db_upsert(chat_id, address, password, token)
    await update.message.reply_text(
        f"✅ New inbox created:\n\n📧 {address}\n\n"
        f"(Tip: use /inbox in a minute if you’re waiting for a verification email.)"
    )


async def inbox(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    row = db_get(chat_id)
    if not row:
        await update.message.reply_text("No inbox yet. Use /new first.")
        return

    _, _, token = row
    async with httpx.AsyncClient(timeout=20) as client:
        msgs = await mailtm_list_messages(client, token)

    if not msgs:
        await update.message.reply_text("📭 Inbox empty (no messages yet).")
        return

    lines = ["📬 Latest messages:\n"]
    for m in msgs[:10]:
        mid = m.get("id")
        frm = (m.get("from") or {}).get("address", "unknown")
        subj = m.get("subject", "(no subject)")
        lines.append(f"- ID: {mid}\n  From: {frm}\n  Subject: {subj}\n")
    await update.message.reply_text("\n".join(lines))


async def read(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    row = db_get(chat_id)
    if not row:
        await update.message.reply_text("No inbox yet. Use /new first.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /read <message_id>")
        return

    msg_id = context.args[0].strip()
    _, _, token = row
    async with httpx.AsyncClient(timeout=20) as client:
        msg = await mailtm_read_message(client, token, msg_id)

    frm = (msg.get("from") or {}).get("address", "unknown")
    subj = msg.get("subject", "(no subject)")
    text = msg.get("text") or ""
    html = msg.get("html") or []

    body_preview = text.strip()
    if not body_preview and isinstance(html, list) and html:
        body_preview = "(HTML email received; this bot is showing text only.)"

    # Telegram message size safety
    body_preview = body_preview[:3500] if body_preview else "(empty body)"
    await update.message.reply_text(
        f"🧾 Message\n\nFrom: {frm}\nSubject: {subj}\n\n{body_preview}"
    )


async def forget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    db_delete(chat_id)
    await update.message.reply_text("🗑️ Forgotten. Use /new anytime to create a new inbox.")


def main() -> None:
    init_db()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN env var")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", new_mail))
    app.add_handler(CommandHandler("inbox", inbox))
    app.add_handler(CommandHandler("read", read))
    app.add_handler(CommandHandler("forget", forget))

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()