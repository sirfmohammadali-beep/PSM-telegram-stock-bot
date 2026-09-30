import os
import re
import asyncio
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

try:
    import psycopg
except ImportError:
    psycopg = None

DB_URL = os.getenv("DATABASE_URL", "").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

TEAMS = {"RENAN", "ZEFREN", "PRAKASH", "RYAN", "SHAKIL", "ALI", "GIAN", "YASIR", "PRASAD", "PANVALI"}

app = FastAPI(title="PSM Telegram Stock Bot")
telegram_app = None

# ---------- Database ----------
def db_conn():
    if not DB_URL:
        import sqlite3
        return sqlite3.connect("stock.db")
    if psycopg is None:
        raise RuntimeError("psycopg is required when DATABASE_URL is set")
    return psycopg.connect(DB_URL)

def is_pg():
    return bool(DB_URL)

def init_db():
    con = db_conn()
    cur = con.cursor()
    if is_pg():
        cur.execute("""
        CREATE TABLE IF NOT EXISTS items (
            code TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            opening INTEGER NOT NULL DEFAULT 0
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id BIGSERIAL PRIMARY KEY,
            code TEXT NOT NULL,
            team TEXT NOT NULL,
            action TEXT NOT NULL,
            qty INTEGER NOT NULL,
            user_id TEXT,
            username TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            team TEXT NOT NULL
        )""")
    else:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS items (
            code TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            opening INTEGER NOT NULL DEFAULT 0
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            team TEXT NOT NULL,
            action TEXT NOT NULL,
            qty INTEGER NOT NULL,
            user_id TEXT,
            username TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            team TEXT NOT NULL
        )""")
    con.commit()
    con.close()

def seed_from_csv():
    """Reads stock.csv if present. Columns accepted:
       Item Number / Item Code / code, Description / Item Name, Store Opening / Opening / Qty
    """
    import csv
    path = Path("stock.csv")
    if not path.exists():
        return 0
    init_db()
    con = db_conn()
    cur = con.cursor()
    count = 0
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = csv.DictReader(f)
        for r in rows:
            def pick(*names):
                for n in names:
                    if n in r and r[n] not in (None, ""):
                        return r[n]
                return ""
            code = str(pick("Item Number", "Item Code", "code")).strip()
            desc = str(pick("Description", "Item Name", "description")).strip()
            opening_raw = pick("Store Opening", "Opening", "Qty", "Quantity")
            if not code or not desc:
                continue
            try:
                opening = int(float(opening_raw or 0))
            except ValueError:
                opening = 0
            if is_pg():
                cur.execute("""
                    INSERT INTO items(code, description, opening)
                    VALUES(%s,%s,%s)
                    ON CONFLICT(code) DO UPDATE SET description=EXCLUDED.description, opening=EXCLUDED.opening
                """, (code, desc, opening))
            else:
                cur.execute("""
                    INSERT INTO items(code, description, opening) VALUES(?,?,?)
                    ON CONFLICT(code) DO UPDATE SET description=excluded.description, opening=excluded.opening
                """, (code, desc, opening))
            count += 1
    con.commit()
    con.close()
    return count

def q1(sql, params=()):
    con = db_conn()
    cur = con.cursor()
    cur.execute(sql, params)
    row = cur.fetchone()
    con.close()
    return row

def exec_sql(sql, params=()):
    con = db_conn()
    cur = con.cursor()
    cur.execute(sql, params)
    con.commit()
    con.close()

def get_team(user_id):
    row = q1("SELECT team FROM users WHERE user_id=%s" % ("%s" if is_pg() else "?"), (str(user_id),))
    return row[0] if row else None

def set_team(user_id, team):
    placeholder = "%s" if is_pg() else "?"
    if is_pg():
        exec_sql(f"""INSERT INTO users(user_id,team) VALUES({placeholder},{placeholder})
                     ON CONFLICT(user_id) DO UPDATE SET team=EXCLUDED.team""",
                 (str(user_id), team))
    else:
        exec_sql(f"""INSERT INTO users(user_id,team) VALUES({placeholder},{placeholder})
                     ON CONFLICT(user_id) DO UPDATE SET team=excluded.team""",
                 (str(user_id), team))

# ---------- Stock ----------
def find_item(term):
    term = term.strip()
    # exact code first
    row = q1("SELECT code,description,opening FROM items WHERE code=%s" % ("%s" if is_pg() else "?"), (term,))
    if row:
        return row
    # exact description
    row = q1("SELECT code,description,opening FROM items WHERE LOWER(description)=LOWER(%s)" % ("%s" if is_pg() else "?"), (term,))
    if row:
        return row
    # partial description
    like = f"%{term}%"
    rows = []
    con = db_conn()
    cur = con.cursor()
    ph = "%s" if is_pg() else "?"
    cur.execute(f"SELECT code,description,opening FROM items WHERE LOWER(description) LIKE LOWER({ph}) LIMIT 10", (like,))
    rows = cur.fetchall()
    con.close()
    return rows

def balance(code):
    ph = "%s" if is_pg() else "?"
    row = q1(f"""SELECT COALESCE(SUM(CASE WHEN action='USED' THEN qty ELSE 0 END),0),
                        COALESCE(SUM(CASE WHEN action IN ('RETURN','RETURNED') THEN qty ELSE 0 END),0),
                        COALESCE(SUM(CASE WHEN action='ISSUE' THEN qty ELSE 0 END),0)
                 FROM transactions WHERE code={ph}""", (code,))
    item = q1(f"SELECT opening FROM items WHERE code={ph}", (code,))
    opening = int(item[0]) if item else 0
    used, returned, issued = map(int, row)
    live = opening - used + returned
    return opening, used, returned, issued, live

def add_tx(code, team, action, qty, user):
    ph = "%s" if is_pg() else "?"
    sql = f"""INSERT INTO transactions(code,team,action,qty,user_id,username)
              VALUES({ph},{ph},{ph},{ph},{ph},{ph})"""
    exec_sql(sql, (code, team, action, qty, str(user.id), user.username or ""))

# ---------- Telegram ----------
HELP = """PSM Stock Bot

First set your team:
 /team ALI

Then use:
 USED C METER 1
 RETURN C METER 1
 ISSUE C METER 2
 STOCK C METER
 STOCK ALL

LIVE BALANCE = Store Opening - Total Used + Total Returned
ISSUE is tracked separately and does not reduce Live Balance.
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)

async def team_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Use: /team ALI")
        return
    team = context.args[0].upper()
    if team not in TEAMS:
        await update.message.reply_text("Unknown team. Use one of: " + ", ".join(sorted(TEAMS)))
        return
    set_team(update.effective_user.id, team)
    await update.message.reply_text(f"Team set: {team}")

async def stock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    term = " ".join(context.args).strip()
    if not term:
        await update.message.reply_text("Use: /stock <item code or description>\nExample: /stock 221200901")
        return
    found = find_item(term)
    if not found:
        await update.message.reply_text("Item nahi mila.")
        return
    if isinstance(found, list):
        if len(found) > 1:
            msg = ["Multiple items found. Item code use karo:"]
            msg += [f"{c} | {d}" for c,d,_ in found[:10]]
            await update.message.reply_text("\n".join(msg))
            return
        code, desc, _ = found[0]
    else:
        code, desc, _ = found
    opening, used, returned, issued, live = balance(code)
    await update.message.reply_text(
        f"{code} | {desc}\n"
        f"Opening: {opening}\nUsed: {used}\nReturned: {returned}\n"
        f"Issued: {issued}\nLIVE BALANCE: {live}"
    )

async def stock_all(update: Update):
    con = db_conn()
    cur = con.cursor()
    cur.execute("SELECT code,description FROM items ORDER BY description")
    rows = cur.fetchall()
    con.close()
    if not rows:
        await update.message.reply_text("No stock data loaded yet. Upload stock.csv to the repository and redeploy.")
        return
    # Keep Telegram message size manageable.
    out = ["STOCK ALL (first 100 items)"]
    for code, desc in rows[:100]:
        *_, live = balance(code)
        out.append(f"{code} | {desc[:45]} | LIVE {live}")
    if len(rows) > 100:
        out.append(f"...and {len(rows)-100} more items. Use STOCK <item> for a specific item.")
    await update.message.reply_text("\n".join(out))

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    upper = text.upper()
    team = get_team(update.effective_user.id)

    if upper == "STOCK ALL":
        await stock_all(update)
        return

    m = re.match(r"^(USED|RETURN|ISSUE)\s+(.+?)\s+(\d+)$", text, re.I)
    if m:
        action, item_term, qty_s = m.groups()
        qty = int(qty_s)
        if qty <= 0:
            await update.message.reply_text("Quantity must be greater than 0.")
            return
        if not team:
            await update.message.reply_text("Pehle apni team set karo: /team ALI")
            return
        found = find_item(item_term)
        if not found:
            await update.message.reply_text("Item nahi mila. Item code ya exact/partial description bhejo.")
            return
        if isinstance(found, list):
            if len(found) > 1:
                msg = ["Multiple items found. Item code use karo:"]
                msg += [f"{c} | {d}" for c,d,_ in found[:10]]
                await update.message.reply_text("\n".join(msg))
                return
            code, desc, _ = found[0]
        else:
            code, desc, _ = found
        add_tx(code, team, action.upper(), qty, update.effective_user)
        opening, used, returned, issued, live = balance(code)
        await update.message.reply_text(
            f"Updated: {action.upper()} {desc} x{qty}\n"
            f"Team: {team}\n"
            f"Opening: {opening}\n"
            f"Used: {used}\n"
            f"Returned: {returned}\n"
            f"Issued: {issued}\n"
            f"LIVE BALANCE: {live}"
        )
        return

    m = re.match(r"^STOCK\s+(.+)$", text, re.I)
    if m:
        term = m.group(1).strip()
        found = find_item(term)
        if not found:
            await update.message.reply_text("Item nahi mila.")
            return
        if isinstance(found, list):
            if len(found) > 1:
                msg = ["Multiple items found. Item code use karo:"]
                msg += [f"{c} | {d}" for c,d,_ in found[:10]]
                await update.message.reply_text("\n".join(msg))
                return
            code, desc, _ = found[0]
        else:
            code, desc, _ = found
        opening, used, returned, issued, live = balance(code)
        await update.message.reply_text(
            f"{code} | {desc}\n"
            f"Opening: {opening}\nUsed: {used}\nReturned: {returned}\n"
            f"Issued: {issued}\nLIVE BALANCE: {live}"
        )
        return

    if upper in ("/HELP", "HELP"):
        await update.message.reply_text(HELP)

async def action_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
    if len(context.args) < 2:
        await update.message.reply_text(f"Use: /{action.lower()} <item> <qty>\nExample: /{action.lower()} 221200901 1")
        return
    try:
        qty = int(context.args[-1])
    except ValueError:
        await update.message.reply_text("Quantity number hona chahiye.")
        return
    if qty <= 0:
        await update.message.reply_text("Quantity must be greater than 0.")
        return
    team = get_team(update.effective_user.id)
    if not team:
        await update.message.reply_text("Pehle apni team set karo: /team ALI")
        return
    item_term = " ".join(context.args[:-1]).strip()
    found = find_item(item_term)
    if not found:
        await update.message.reply_text("Item nahi mila. Item code ya description check karo.")
        return
    if isinstance(found, list):
        if len(found) > 1:
            msg = ["Multiple items found. Item code use karo:"]
            msg += [f"{c} | {d}" for c,d,_ in found[:10]]
            await update.message.reply_text("\n".join(msg))
            return
        code, desc, _ = found[0]
    else:
        code, desc, _ = found
    add_tx(code, team, action, qty, update.effective_user)
    opening, used, returned, issued, live = balance(code)
    await update.message.reply_text(
        f"Updated: {action} {desc} x{qty}\nTeam: {team}\n"
        f"Opening: {opening}\nUsed: {used}\nReturned: {returned}\n"
        f"Issued: {issued}\nLIVE BALANCE: {live}"
    )

async def run_bot():
    global telegram_app
    if not BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN is not set; web service will stay online.")
        return
    telegram_app = Application.builder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("team", team_cmd))
    telegram_app.add_handler(CommandHandler("stock", stock_cmd))
    telegram_app.add_handler(CommandHandler("used", lambda u, c: action_cmd(u, c, "USED")))
    telegram_app.add_handler(CommandHandler("return", lambda u, c: action_cmd(u, c, "RETURN")))
    telegram_app.add_handler(CommandHandler("issue", lambda u, c: action_cmd(u, c, "ISSUE")))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()
    print("Telegram bot polling started.")

@app.on_event("startup")
async def startup():
    init_db()
    try:
        print("Seeded items:", seed_from_csv())
    except Exception as e:
        print("Seed error:", e)
    asyncio.create_task(run_bot())

@app.on_event("shutdown")
async def shutdown():
    global telegram_app
    if telegram_app:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()

@app.get("/", response_class=PlainTextResponse)
async def home():
    return "PSM Stock Bot is running"

@app.get("/health", response_class=PlainTextResponse)
async def health():
    return "OK"
