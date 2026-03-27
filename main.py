import os
import json
import time
import hmac
import hashlib
import asyncio
import random
import httpx
import asyncpg
from fastapi import FastAPI, Request as StarletteRequest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

BOT_TOKEN    = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
DATABASE_URL = os.getenv("DATABASE_URL", "")
WEBAPP_URL   = os.getenv("WEBAPP_URL", "https://your-domain.com")
TG_API       = f"https://api.telegram.org/bot{BOT_TOKEN}"

db_pool = None


async def get_pool():
    global db_pool
    if db_pool is None:
        db_pool = await asyncpg.create_pool(
            DATABASE_URL, min_size=1, max_size=10, ssl='require'
        )
    return db_pool


async def send_message(chat_id: int, text: str, reply_markup: dict = None, parse_mode: str = "HTML"):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(f"{TG_API}/sendMessage", json=payload)


@app.on_event("startup")
async def startup():
    await get_pool()
    await init_db()
    print("✅ Casino started!")


@app.on_event("shutdown")
async def shutdown():
    global db_pool
    if db_pool:
        await db_pool.close()


async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                tg_id       BIGINT PRIMARY KEY,
                name        TEXT,
                username    TEXT,
                photo       TEXT,
                balance     BIGINT DEFAULT 10000,
                total_bets  BIGINT DEFAULT 0,
                total_wins  BIGINT DEFAULT 0,
                games_played INT DEFAULT 0,
                created_at  TIMESTAMP DEFAULT NOW(),
                last_seen   TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS game_history (
                id          BIGSERIAL PRIMARY KEY,
                tg_id       BIGINT,
                game        TEXT DEFAULT 'dice',
                bet         BIGINT,
                choice      TEXT,
                result      TEXT,
                win         BOOLEAN,
                payout      BIGINT,
                balance_after BIGINT,
                ts          TIMESTAMP DEFAULT NOW()
            )
        """)
    print("✅ DB initialized!")


# ── TELEGRAM INIT DATA VALIDATION ──

def validate_init_data(init_data: str) -> dict | None:
    """Validate Telegram WebApp initData. Returns user dict or None."""
    try:
        parsed = {}
        for item in init_data.split("&"):
            k, v = item.split("=", 1)
            parsed[k] = v

        hash_val = parsed.pop("hash", None)
        if not hash_val:
            return None

        data_check = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed.items())
        )
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(expected, hash_val):
            return None

        user_json = parsed.get("user", "{}")
        from urllib.parse import unquote
        user = json.loads(unquote(user_json))
        return user
    except Exception as e:
        print(f"initData validation error: {e}")
        return None


# ── MODELS ──

class RegisterRequest(BaseModel):
    init_data: str
    photo: str = None

class BetRequest(BaseModel):
    init_data: str
    bet: int
    choice: str  # "high" (4-6) or "low" (1-3) or exact number "1"-"6"

class ProfileRequest(BaseModel):
    init_data: str


# ── AUTH / PROFILE ──

@app.post("/auth/register")
async def register(req: RegisterRequest):
    user = validate_init_data(req.init_data)
    if not user:
        # In dev mode without real Telegram, parse raw
        try:
            from urllib.parse import unquote, parse_qs
            parsed = parse_qs(req.init_data)
            user_raw = parsed.get("user", ["{}"])[0]
            user = json.loads(unquote(user_raw))
        except:
            return {"ok": False, "error": "Invalid initData"}

    tg_id = user.get("id")
    if not tg_id:
        return {"ok": False, "error": "No user id"}

    name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
    username = f"@{user['username']}" if user.get("username") else ""
    photo = req.photo or ""

    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT * FROM users WHERE tg_id=$1", tg_id)
        if existing:
            await conn.execute(
                "UPDATE users SET name=$1, username=$2, last_seen=NOW() WHERE tg_id=$3",
                name, username, tg_id
            )
            row = await conn.fetchrow("SELECT * FROM users WHERE tg_id=$1", tg_id)
        else:
            row = await conn.fetchrow(
                """INSERT INTO users (tg_id, name, username, photo)
                   VALUES ($1,$2,$3,$4) RETURNING *""",
                tg_id, name, username, photo
            )

    u = dict(row)
    u["created_at"] = u["created_at"].isoformat() if u.get("created_at") else None
    u["last_seen"] = u["last_seen"].isoformat() if u.get("last_seen") else None
    return {"ok": True, "user": u, "is_new": not existing}


@app.post("/auth/profile")
async def get_profile(req: ProfileRequest):
    user = validate_init_data(req.init_data)
    if not user:
        try:
            from urllib.parse import unquote, parse_qs
            parsed = parse_qs(req.init_data)
            user_raw = parsed.get("user", ["{}"])[0]
            user = json.loads(unquote(user_raw))
        except:
            return {"ok": False, "error": "Invalid initData"}

    tg_id = user.get("id")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE tg_id=$1", tg_id)
        if not row:
            return {"ok": False, "error": "User not found"}
        history = await conn.fetch(
            "SELECT * FROM game_history WHERE tg_id=$1 ORDER BY ts DESC LIMIT 10",
            tg_id
        )

    u = dict(row)
    u["created_at"] = u["created_at"].isoformat() if u.get("created_at") else None
    u["last_seen"] = u["last_seen"].isoformat() if u.get("last_seen") else None

    hist = []
    for h in history:
        d = dict(h)
        d["ts"] = d["ts"].isoformat() if d.get("ts") else None
        hist.append(d)

    return {"ok": True, "user": u, "history": hist}


# ── DICE GAME ──

DICE_PAYOUTS = {
    "high": 1.9,     # 4-6 — almost 2x
    "low": 1.9,      # 1-3 — almost 2x
    "exact": 5.0,    # exact number — 5x
}

@app.post("/game/dice/roll")
async def dice_roll(req: BetRequest):
    user = validate_init_data(req.init_data)
    if not user:
        try:
            from urllib.parse import unquote, parse_qs
            parsed = parse_qs(req.init_data)
            user_raw = parsed.get("user", ["{}"])[0]
            user = json.loads(unquote(user_raw))
        except:
            return {"ok": False, "error": "Invalid initData"}

    tg_id = user.get("id")
    bet = req.bet
    choice = req.choice.lower().strip()

    if bet <= 0:
        return {"ok": False, "error": "Ставка должна быть больше 0"}
    if bet > 1_000_000:
        return {"ok": False, "error": "Максимальная ставка 1,000,000"}

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE tg_id=$1", tg_id)
        if not row:
            return {"ok": False, "error": "Пользователь не найден"}

        balance = row["balance"]
        if bet > balance:
            return {"ok": False, "error": "Недостаточно монет"}

        # Roll the dice
        result = random.randint(1, 6)

        # Determine win
        if choice == "high":
            win = result >= 4
            multiplier = DICE_PAYOUTS["high"]
        elif choice == "low":
            win = result <= 3
            multiplier = DICE_PAYOUTS["low"]
        elif choice in ["1","2","3","4","5","6"]:
            win = result == int(choice)
            multiplier = DICE_PAYOUTS["exact"]
        else:
            return {"ok": False, "error": "Неверный тип ставки"}

        payout = int(bet * multiplier) if win else 0
        profit = payout - bet
        new_balance = balance - bet + payout

        # Update DB
        await conn.execute(
            """UPDATE users SET
               balance=$1,
               total_bets=total_bets+$2,
               total_wins=total_wins+$3,
               games_played=games_played+1,
               last_seen=NOW()
               WHERE tg_id=$4""",
            new_balance, bet, (payout if win else 0), tg_id
        )
        await conn.execute(
            """INSERT INTO game_history (tg_id, game, bet, choice, result, win, payout, balance_after)
               VALUES ($1,'dice',$2,$3,$4,$5,$6,$7)""",
            tg_id, bet, choice, str(result), win, payout, new_balance
        )

    return {
        "ok": True,
        "result": result,
        "win": win,
        "bet": bet,
        "payout": payout,
        "profit": profit,
        "balance": new_balance,
        "multiplier": multiplier,
    }


@app.get("/game/leaderboard")
async def leaderboard():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT tg_id, name, username, balance, total_wins, games_played
               FROM users ORDER BY balance DESC LIMIT 20"""
        )
    return {"ok": True, "leaders": [dict(r) for r in rows]}


# ── DAILY BONUS ──

class DailyRequest(BaseModel):
    init_data: str

@app.post("/game/daily")
async def daily_bonus(req: DailyRequest):
    user = validate_init_data(req.init_data)
    if not user:
        try:
            from urllib.parse import unquote, parse_qs
            parsed = parse_qs(req.init_data)
            user_raw = parsed.get("user", ["{}"])[0]
            user = json.loads(unquote(user_raw))
        except:
            return {"ok": False, "error": "Invalid initData"}

    tg_id = user.get("id")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE tg_id=$1", tg_id)
        if not row:
            return {"ok": False, "error": "Not found"}

        last = row["last_seen"]
        import datetime
        now = datetime.datetime.utcnow()
        diff = (now - last).total_seconds() if last else 99999

        if diff < 86400:
            remaining = int(86400 - diff)
            h = remaining // 3600
            m = (remaining % 3600) // 60
            return {"ok": False, "error": f"Следующий бонус через {h}ч {m}м"}

        bonus = random.randint(500, 5000)
        new_balance = row["balance"] + bonus
        await conn.execute(
            "UPDATE users SET balance=$1, last_seen=NOW() WHERE tg_id=$2",
            new_balance, tg_id
        )

    return {"ok": True, "bonus": bonus, "balance": new_balance}


# ── TELEGRAM BOT WEBHOOK ──

@app.post("/webhook")
async def telegram_webhook(request: StarletteRequest):
    try:
        data = await request.json()
    except Exception:
        return {"ok": True}

    msg = data.get("message")
    if not msg:
        return {"ok": True}

    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()
    user = msg.get("from", {})
    first_name = user.get("first_name", "Игрок")

    if text == "/start":
        welcome = (
            f"🎰 <b>Привет, {first_name}!</b>\n\n"
            f"Добро пожаловать в <b>LUCKY DICE CASINO</b> 🎲\n\n"
            f"Испытай удачу в захватывающих играх!\n"
            f"• 🎲 Dice — бросай кубик\n"
            f"• 🃏 Poker — <i>скоро</i>\n"
            f"• 📈 Crash — <i>скоро</i>\n\n"
            f"💰 Тебе начислено <b>10,000 монет</b> на старт!\n\n"
            f"👇 Нажми кнопку чтобы войти в казино:"
        )
        keyboard = {
            "inline_keyboard": [[{
                "text": "🎰 Открыть казино",
                "web_app": {"url": WEBAPP_URL}
            }]]
        }
        await send_message(chat_id, welcome, reply_markup=keyboard)

    elif text == "/balance":
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT balance, games_played, total_wins FROM users WHERE tg_id=$1", chat_id)
        if row:
            await send_message(chat_id,
                f"💰 Баланс: <b>{row['balance']:,}</b> монет\n"
                f"🎮 Игр сыграно: <b>{row['games_played']}</b>\n"
                f"🏆 Монет выиграно: <b>{row['total_wins']:,}</b>"
            )
        else:
            await send_message(chat_id, "❌ Сначала зарегистрируйся в казино!")

    return {"ok": True}


@app.get("/set-webhook")
async def set_webhook(url: str):
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(f"{TG_API}/setWebhook", json={"url": url})
    return r.json()


@app.get("/health")
async def health():
    return {"status": "ok", "service": "Lucky Dice Casino"}
