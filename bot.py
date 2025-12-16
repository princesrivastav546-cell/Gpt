import os
import time
import secrets
import sqlite3
from typing import Optional, List, Tuple

import httpx
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    MessageHandler,
    ContextTypes,
    filters,
)

MAILTM_BASE = "https://api.mail.tm"
DB_PATH = "data.db"

# --- UI Buttons (no slash commands needed) ---
BTN_NEW = "📧 Generate new mail"
BTN_DELETE = "🗑️ Delete current mail"
BTN_LIST = "📜 My saved mails"
BTN_REUSE = "♻️ Reuse a mail"
BTN_HELP = "❓ Help / Contact"

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(BTN_NEW), KeyboardButton(BTN_DELETE)],
        [KeyboardButton(BTN_LIST), KeyboardButton(BTN_REUSE)],
        [KeyboardButton(BTN_HELP)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

REUSE_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("⬅️ Back to menu")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

POLL_EVERY_SECONDS = int(os.environ.get("POLL_EVERY_SECONDS", "12"))  # tune if you want


# ---------------- DB ----------------
def init_db() -> None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # one "active mailbox" per chat
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS active_mailbox (
            chat_id INTEGER PRIMARY KEY,
            mailbox_id INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )

    # multiple saved mailboxes per chat (reuse supported)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS mailboxes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            address TEXT NOT NULL,
            password TEXT NOT NULL,
            token TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            label TEXT DEFAULT NULL,
            UNIQUE(chat_id, address)
        )
        """
    )

    # remember what messages we've already pushed to each chat
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_messages (
            chat_id INTEGER NOT NULL,
            message_id TEXT NOT NULL,
            seen_at INTEGER NOT NULL,
            PRIMARY KEY(chat_id, message_id)
        )
        """
    )

    con.commit()
    con.close()


def db_get_active_mailbox(chat_id: int) -> Optional[Tuple[int, str, str, str]]:
    """Returns (mailbox_id, address, password, token) for active mailbox"""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        SELECT m.id, m.address, m.password, m.token
        FROM active_mailbox a
        JOIN mailboxes m ON m.id = a.mailbox_id
        WHERE a.chat_id = ?
        """,
        (chat_id,),
    )
    row = cur.fetchone()
    con.close()
    return row


def db_set_active_mailbox(chat_id: int, mailbox_id: int) -> None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO active_mailbox(chat_id, mailbox_id, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
          mailbox_id=excluded.mailbox_id,
          updated_at=excluded.updated_at
        """,
        (chat_id, mailbox_id, int(time.time())),
    )
    con.commit()
    con.close()


def db_save_mailbox(chat_id: int, address: str, password: str, token: str, label: Optional[str] = None) -> int:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        INSERT OR IGNORE INTO mailboxes(chat_id, address, password, token, created_at, label)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (chat_id, address, password, token, int(time.time()), label),
    )
    con.commit()

    # fetch mailbox id
    cur.execute("SELECT id FROM mailboxes WHERE chat_id=? AND address=?", (chat_id, address))
    mailbox_id = cur.fetchone()[0]
    con.close()
    return mailbox_id


def db_list_mailboxes(chat_id: int) -> List[Tuple[int, str, Optional[str], int]]:
    """Returns [(id, address, label, created_at), ...]"""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT id, address, label, created_at FROM mailboxes WHERE chat_id=? ORDER BY created_at DESC",
        (chat_id,),
    )
    rows = cur.fetchall()
    con.close()
    return rows


def db_delete_mailbox(chat_id: int, mailbox_id: int) -> None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # remove active if it points to this mailbox
    cur.execute("DELETE FROM active_mailbox WHERE chat_id=? AND mailbox_id=?", (chat_id, mailbox_id))
    # delete mailbox
    cur.execute("DELETE FROM mailboxes WHERE chat_id=? AND id=?", (chat_id, mailbox_id))

    con.commit()
    con.close()


def db_mark_seen(chat_id: int, message_id: str) -> None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO seen_messages(chat_id, message_id, seen_at) VALUES (?, ?, ?)",
        (chat_id, message_id, int(time.time())),
    )
    con.commit()
    con.close()


def db_is_seen(chat_id: int, message_id: str) -> bool:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM seen_messages WHERE chat_id=? AND message_id=? LIMIT 1", (chat_id, message_id))
    row = cur.fetchone()
    con.close()
    return row is not None


# ---------------- Mail.tm ----------------
async def mailtm_get_random_domain(client: httpx.AsyncClient) -> str:
    r = await client.get(f"{MAILTM_BASE}/domains?page=1")
    r.raise_for_status()
    items = r.json().get("hydra:member", [])
    if not items:
        raise RuntimeError("No mail.tm domains available right now.")
    for d in items:
        if d.get("isActive"):
            return d["domain"]
    return items[0]["domain"]


async def mailtm_create_account_and_token(client: httpx.AsyncClient) -> Tuple[str, str, str]:
    domain = await mailtm_get_random_domain(client)
    local = secrets.token_hex(6)
    address = f"{local}@{domain}"
    password = secrets.token_urlsafe(12)

    r1 = await client.post(f"{MAILTM_BASE}/accounts", json={"address": address, "password": password})
    if r1.status_code >= 400:
        address = f"{secrets.token_hex(7)}@{domain}"
        r1 = await client.post(f"{MAILTM_BASE}/accounts", json={"address": address, "password": password})
    r1.raise_for_status()

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


def format_full_message(msg: dict) -> str:
    frm = (msg.get("from") or {}).get("address", "unknown")
    subj = msg.get("subject") or "(no subject)"
    to_list = msg.get("to") or []
    tos = ", ".join([(x.get("address") or "") for x in to_list if isinstance(x, dict)]) or "(unknown)"
    created = msg.get("createdAt") or ""
    text = (msg.get("text") or "").strip()

    # Some emails only have HTML; Mail.tm sometimes provides "html" list.
    html = msg.get("html")
    if not text and html:
        # We intentionally do NOT render raw HTML in Telegram.
        text = "(This email is HTML-only. Text version not available.)"

    # Telegram hard limit ~4096 chars; keep a safe margin.
    if len(text) > 3500:
        text = text[:3500] + "\n…(truncated)"

    return (
        f"📩 <b>New Email</b>\n"
        f"<b>From:</b> {frm}\n"
        f"<b>To:</b> {tos}\n"
        f"<b>Subject:</b> {subj}\n"
        f"<b>Date:</b> {created}\n\n"
        f"{text or '(empty body)'}"
    )


# ---------------- Bot logic ----------------
async def ensure_menu(update: Update) -> None:
    if update.message:
        await update.message.reply_text("Menu ready ✅", reply_markup=MAIN_MENU)


async def handle_start_like(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Disposable Mail Bot ✅\n\n"
        "Use the buttons below.\n"
        "• Auto-forward is ON: when an email arrives, I send it here automatically.\n\n"
        "Privacy tip: this bot stores your temp mailbox tokens in its database so you can reuse mailboxes."
    )
    await update.message.reply_text(text, reply_markup=MAIN_MENU)


async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    msg_text = (update.message.text or "").strip()

    if msg_text in ("/start", "start", "hi", "hello"):
        await handle_start_like(update, context)
        return

    if msg_text == "⬅️ Back to menu":
        context.user_data.pop("reuse_mode", None)
        await update.message.reply_text("Back to menu ✅", reply_markup=MAIN_MENU)
        return

    if msg_text == BTN_HELP:
        await update.message.reply_text(
            "Help / Contact:\n\n"
            "If you have issues or want custom features, contact: @platoonleaderr",
            reply_markup=MAIN_MENU,
        )
        return

    if msg_text == BTN_NEW:
        await update.message.reply_text("Creating a new inbox…", reply_markup=MAIN_MENU)
        async with httpx.AsyncClient(timeout=25) as client:
            address, password, token = await mailtm_create_account_and_token(client)

        mailbox_id = db_save_mailbox(chat_id, address, password, token)
        db_set_active_mailbox(chat_id, mailbox_id)

        await update.message.reply_text(
            f"✅ <b>New inbox generated</b>\n\n"
            f"📧 <code>{address}</code>\n\n"
            f"Now just wait—when emails arrive, I’ll forward the full message automatically.",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
        )
        return

    if msg_text == BTN_LIST:
        rows = db_list_mailboxes(chat_id)
        if not rows:
            await update.message.reply_text("You have no saved mails yet. Tap “Generate new mail”.", reply_markup=MAIN_MENU)
            return

        active = db_get_active_mailbox(chat_id)
        active_id = active[0] if active else None

        lines = ["📜 <b>Your saved mails</b>\n"]
        for mid, addr, label, created_at in rows[:20]:
            mark = "✅" if mid == active_id else "▫️"
            label_txt = f" ({label})" if label else ""
            lines.append(f"{mark} <code>{addr}</code>{label_txt}\n<b>ID:</b> <code>{mid}</code>")

        lines.append("\nTo reuse: tap “♻️ Reuse a mail”, then send the ID you want.")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=MAIN_MENU)
        return

    if msg_text == BTN_REUSE:
        rows = db_list_mailboxes(chat_id)
        if not rows:
            await update.message.reply_text("No saved mails to reuse yet. Tap “Generate new mail”.", reply_markup=MAIN_MENU)
            return
        context.user_data["reuse_mode"] = True
        await update.message.reply_text(
            "♻️ Send me the <b>ID</b> of the mailbox you want to reuse.\n\n"
            "Example: 12",
            parse_mode=ParseMode.HTML,
            reply_markup=REUSE_MENU,
        )
        return

    if msg_text == BTN_DELETE:
        active = db_get_active_mailbox(chat_id)
        if not active:
            await update.message.reply_text("No active inbox to delete. Tap “Generate new mail”.", reply_markup=MAIN_MENU)
            return

        mailbox_id, address, _, _ = active
        db_delete_mailbox(chat_id, mailbox_id)
        await update.message.reply_text(
            f"🗑️ Deleted current inbox:\n<code>{address}</code>\n\n"
            f"Tip: If you wanted to keep it for reuse, don’t delete—just generate another and keep this saved.",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
        )
        return

    # If user is selecting a mailbox to reuse
    if context.user_data.get("reuse_mode"):
        if msg_text.isdigit():
            mailbox_id = int(msg_text)
            rows = db_list_mailboxes(chat_id)
            allowed_ids = {r[0] for r in rows}
            if mailbox_id not in allowed_ids:
                await update.message.reply_text("That ID is not in your saved list. Try again.", reply_markup=REUSE_MENU)
                return

            db_set_active_mailbox(chat_id, mailbox_id)
            context.user_data.pop("reuse_mode", None)

            # show active address
            active = db_get_active_mailbox(chat_id)
            address = active[1] if active else "(unknown)"
            await update.message.reply_text(
                f"✅ Reusing inbox:\n<code>{address}</code>\n\n"
                f"Auto-forward is active.",
                parse_mode=ParseMode.HTML,
                reply_markup=MAIN_MENU,
            )
            return

        await update.message.reply_text("Please send a numeric ID (example: 12) or tap Back.", reply_markup=REUSE_MENU)
        return

    # Default fallback
    await update.message.reply_text("Use the menu buttons 👇", reply_markup=MAIN_MENU)


# ---------------- Auto Forward Worker (JobQueue) ----------------
async def poll_all_chats(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Periodically scans mail.tm inboxes for all chats with an active mailbox.
    When it finds new messages, it pushes FULL content to the chat automatically.
    """
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT chat_id, mailbox_id FROM active_mailbox")
    actives = cur.fetchall()
    con.close()

    if not actives:
        return

    async with httpx.AsyncClient(timeout=25) as client:
        for chat_id, mailbox_id in actives:
            # fetch mailbox token
            con2 = sqlite3.connect(DB_PATH)
            cur2 = con2.cursor()
            cur2.execute("SELECT token FROM mailboxes WHERE id=? AND chat_id=?", (mailbox_id, chat_id))
            row = cur2.fetchone()
            con2.close()
            if not row:
                continue
            token = row[0]

            try:
                msgs = await mailtm_list_messages(client, token)
            except Exception:
                # keep silent to avoid spam; you can log if needed
                continue

            # Mail.tm list returns latest; push any unseen in reverse order for readability
            new_ids = []
            for m in msgs:
                mid = m.get("id")
                if mid and not db_is_seen(chat_id, mid):
                    new_ids.append(mid)

            for mid in reversed(new_ids):
                try:
                    full = await mailtm_read_message(client, token, mid)
                    text = format_full_message(full)
                    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
                    db_mark_seen(chat_id, mid)
                except Exception:
                    # If sending fails, don't mark seen so we can retry later
                    continue


def main() -> None:
    init_db()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN env var")

    app = Application.builder().token(token).build()

    # One handler for all text to drive the menu UI
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))
    # allow /start to still work (optional)
    app.add_handler(MessageHandler(filters.COMMAND, handle_buttons))

    # Run auto-poll job
    app.job_queue.run_repeating(poll_all_chats, interval=POLL_EVERY_SECONDS, first=5)

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
