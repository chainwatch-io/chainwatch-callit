"""
BTC Higher/Lower Prediction Backend
------------------------------------
Serves the Telegram Mini App frontend and handles:
  - Fetching the live BTC price (CoinGecko public API)
  - Running rounds on a timer (default: 60 minutes)
  - Accepting one free prediction per user per round (Higher / Lower)
  - Resolving rounds and updating points + leaderboard
  - Verifying Telegram WebApp initData so requests are authenticated

This is a FREE-TO-PLAY points game. It does not process any payments and
awards no cash prizes automatically -- if you want to fund a real prize
for top leaderboard finishers, that is a manual, separate action you take
(e.g. sending a gift card or crypto to the winner yourself), funded from
your own money/sponsorships, not from other players' entries. Keeping it
structured this way (free entry, prize funded by the operator) is what
keeps this in "sweepstakes/skill contest" territory rather than looking
like an unlicensed lottery.

Setup:
    pip install -r requirements.txt
    export BOT_TOKEN="your-telegram-bot-token"
    python app.py

The app must be served over HTTPS with a public URL for Telegram to load
it as a Mini App (see README.md for hosting options).
"""

import os
import time
import hmac
import hashlib
import sqlite3
import logging
from urllib.parse import parse_qsl
from contextlib import asynccontextmanager

import traceback
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_PATH = os.environ.get("PREDICT_DB_PATH", "predict.db")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")  # e.g. "@chainwatch_io" -- optional, enables auto-posted round results
# The t.me Direct Link Mini App URL, e.g. "https://t.me/ChainWatchGameBot/callit".
# NOT the same as the Railway backend URL -- this is a special deep link
# Telegram itself resolves to open the Mini App, and it's the only kind of
# link that works as a button on a CHANNEL post (see setup notes in README).
MINI_APP_DEEPLINK = os.environ.get("MINI_APP_DEEPLINK", "")
ROUND_MINUTES = float(os.environ.get("ROUND_MINUTES", "60"))
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
BINANCE_URL = "https://api.binance.com/api/v3/ticker/price"

scheduler = AsyncIOScheduler()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    abs_path = os.path.abspath(DB_PATH)
    dir_path = os.path.dirname(abs_path) or "."
    log.info(
        "DB_PATH resolved to: %s (absolute: %s, directory exists: %s, directory writable: %s)",
        DB_PATH, abs_path, os.path.isdir(dir_path), os.access(dir_path, os.W_OK),
    )
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                points INTEGER NOT NULL DEFAULT 0,
                streak INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS rounds (
                round_id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_price REAL,
                end_price REAL,
                start_time REAL NOT NULL,
                end_time REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'  -- open | resolved
            );

            CREATE TABLE IF NOT EXISTS predictions (
                round_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                choice TEXT NOT NULL,  -- higher | lower
                correct INTEGER,       -- NULL until resolved
                PRIMARY KEY (round_id, user_id)
            );
            """
        )


# ---------------------------------------------------------------------------
# Telegram WebApp auth verification
# ---------------------------------------------------------------------------
# Telegram signs the initData string sent from the Mini App with the bot
# token so the backend can trust the user identity without a separate
# login step. See: https://core.telegram.org/bots/webapps#validating-data

def verify_init_data(init_data: str) -> dict:
    if not BOT_TOKEN:
        raise HTTPException(500, "Server misconfigured: BOT_TOKEN not set")
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        raise HTTPException(401, "Malformed init data")

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "Missing hash")

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise HTTPException(401, "Invalid init data signature")

    auth_date = int(pairs.get("auth_date", 0))
    if time.time() - auth_date > 86400:
        raise HTTPException(401, "Init data expired")

    import json
    user = json.loads(pairs.get("user", "{}"))
    return user


# ---------------------------------------------------------------------------
# Price + round logic
# ---------------------------------------------------------------------------

_price_cache = {"price": None, "fetched_at": 0.0}
PRICE_CACHE_TTL = 30  # seconds -- CoinGecko's free tier rate-limits aggressively,
                       # and a game with hour-long rounds doesn't need price data
                       # any fresher than this.


def _fetch_from_coingecko() -> float:
    resp = requests.get(
        COINGECKO_URL, params={"ids": "bitcoin", "vs_currencies": "usd"}, timeout=8
    )
    resp.raise_for_status()
    return resp.json()["bitcoin"]["usd"]


def _fetch_from_binance() -> float:
    resp = requests.get(BINANCE_URL, params={"symbol": "BTCUSDT"}, timeout=8)
    resp.raise_for_status()
    return float(resp.json()["price"])


def fetch_btc_price() -> float:
    now = time.time()
    if _price_cache["price"] is not None and (now - _price_cache["fetched_at"]) < PRICE_CACHE_TTL:
        return _price_cache["price"]

    # Try CoinGecko first, then fall back to Binance's public ticker if
    # CoinGecko is rate-limiting us (common on shared cloud-host IPs, which
    # is exactly what's happening on Railway). Binance's public market-data
    # endpoint has generous, key-free rate limits and is a reliable second
    # source for the same underlying price.
    for source_name, source_fn in [("CoinGecko", _fetch_from_coingecko), ("Binance", _fetch_from_binance)]:
        try:
            price = source_fn()
            _price_cache["price"] = price
            _price_cache["fetched_at"] = now
            log.info("Price fetched from %s: $%s", source_name, price)
            return price
        except requests.RequestException as e:
            log.warning("%s price fetch failed: %s", source_name, e)

    # Both sources failed. Fall back to the last known price rather than
    # crashing the request -- a stale price is far better UX than an error.
    if _price_cache["price"] is not None:
        log.warning("Serving stale cached price after both sources failed")
        return _price_cache["price"]

    raise HTTPException(503, "Price data temporarily unavailable, try again shortly")


def get_open_round(conn):
    return conn.execute("SELECT * FROM rounds WHERE status='open' ORDER BY round_id DESC LIMIT 1").fetchone()


def send_channel_message(text: str):
    """Post a message to the configured Telegram channel using the raw Bot
    API (no need to run a separate bot process for this one-way notification).
    Attaches a one-tap "Play Now" button that opens the Mini App, if
    MINI_APP_DEEPLINK is configured. This MUST be a plain url button using
    the t.me Direct Link Mini App format (https://t.me/Bot/shortname) --
    Telegram's web_app button type is restricted to private chats and does
    not work on channel posts."""
    if not CHANNEL_ID or not BOT_TOKEN:
        return
    payload = {"chat_id": CHANNEL_ID, "text": text, "parse_mode": "Markdown"}
    if MINI_APP_DEEPLINK:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": "🎯 Play Now", "url": MINI_APP_DEEPLINK}]]
        }
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=10,
        )
    except requests.RequestException as e:
        log.warning("Failed to post channel message: %s", e)


def open_new_round():
    try:
        price = fetch_btc_price()
    except Exception as e:
        log.warning("Could not open new round, price unavailable: %s", e)
        return
    now = time.time()
    with db() as conn:
        conn.execute(
            "INSERT INTO rounds (start_price, start_time, end_time, status) VALUES (?, ?, ?, 'open')",
            (price, now, now + ROUND_MINUTES * 60),
        )
        conn.commit()
    log.info("Opened new round at price $%s", price)


def resolve_open_round():
    with db() as conn:
        rnd = get_open_round(conn)
        if rnd is None:
            open_new_round()
            return
        if time.time() < rnd["end_time"]:
            return  # not time yet

        try:
            end_price = fetch_btc_price()
        except Exception as e:
            log.warning("Could not resolve round %s yet, price unavailable: %s", rnd["round_id"], e)
            return  # try again on the next scheduled tick
        direction = "higher" if end_price > rnd["start_price"] else "lower"

        preds = conn.execute(
            "SELECT * FROM predictions WHERE round_id=?", (rnd["round_id"],)
        ).fetchall()
        total_players = len(preds)
        correct_count = 0
        for p in preds:
            correct = 1 if p["choice"] == direction else 0
            if correct:
                correct_count += 1
            conn.execute(
                "UPDATE predictions SET correct=? WHERE round_id=? AND user_id=?",
                (correct, rnd["round_id"], p["user_id"]),
            )
            if correct:
                conn.execute(
                    "UPDATE users SET points = points + 10, streak = streak + 1 WHERE user_id=?",
                    (p["user_id"],),
                )
            else:
                conn.execute("UPDATE users SET streak = 0 WHERE user_id=?", (p["user_id"],))

        conn.execute(
            "UPDATE rounds SET end_price=?, status='resolved' WHERE round_id=?",
            (end_price, rnd["round_id"]),
        )
        conn.commit()

        leader = conn.execute(
            "SELECT username, points FROM users ORDER BY points DESC LIMIT 1"
        ).fetchone()

    log.info("Resolved round %s: %s", rnd["round_id"], direction)

    price_delta = end_price - rnd["start_price"]
    arrow = "📈" if direction == "higher" else "📉"
    lines = [
        f"{arrow} *Round #{rnd['round_id']} result: {direction.upper()}*",
        f"${rnd['start_price']:,.2f} → ${end_price:,.2f} ({price_delta:+,.2f})",
        "",
    ]
    if total_players > 0:
        lines.append(f"👥 {total_players} players called it — {correct_count} got it right")
    else:
        lines.append("👥 No one played this round — be the first next round!")
    if leader:
        lines.append(f"🏆 Leaderboard leader: {leader['username']} ({leader['points']} pts)")
    lines.append("")
    lines.append("Play the next round now ⬆️")

    send_channel_message("\n".join(lines))
    open_new_round()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with db() as conn:
        if get_open_round(conn) is None:
            try:
                open_new_round()
            except Exception as e:
                log.warning("Could not open initial round at startup: %s", e)
                # A round will be opened automatically once resolve_open_round's
                # periodic job runs and finds no open round -- app still starts.
    scheduler.add_job(resolve_open_round, "interval", seconds=5)
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)


# TEMPORARY DEBUG HANDLER -- remove once the crash is diagnosed and fixed.
# Surfaces the real Python error + traceback directly in the HTTP response
# instead of a bare "Internal Server Error", since Railway's log UI can be
# hard to navigate to find application-level tracebacks.
@app.exception_handler(Exception)
async def debug_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"error": str(exc), "traceback": traceback.format_exc()},
    )


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.get("/api/round")
def api_round(init_data: str):
    user = verify_init_data(init_data)
    user_id = user.get("id")

    with db() as conn:
        rnd = get_open_round(conn)
        urow = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        points = urow["points"] if urow else 0
        streak = urow["streak"] if urow else 0

        my_pred = None
        if rnd:
            p = conn.execute(
                "SELECT * FROM predictions WHERE round_id=? AND user_id=?",
                (rnd["round_id"], user_id),
            ).fetchone()
            if p:
                my_pred = p["choice"]

        current_price = fetch_btc_price()
        leaderboard = conn.execute(
            "SELECT username, points FROM users ORDER BY points DESC LIMIT 10"
        ).fetchall()

        entries_count = 0
        if rnd:
            entries_count = conn.execute(
                "SELECT COUNT(*) AS c FROM predictions WHERE round_id=?", (rnd["round_id"],)
            ).fetchone()["c"]

    return {
        "round_id": rnd["round_id"] if rnd else None,
        "start_price": rnd["start_price"] if rnd else None,
        "current_price": current_price,
        "end_time": rnd["end_time"] if rnd else None,
        "my_prediction": my_pred,
        "points": points,
        "streak": streak,
        "entries_count": entries_count,
        "leaderboard": [dict(r) for r in leaderboard],
    }


@app.post("/api/predict")
async def api_predict(request: Request):
    body = await request.json()
    init_data = body.get("init_data", "")
    choice = body.get("choice")
    if choice not in ("higher", "lower"):
        raise HTTPException(400, "choice must be 'higher' or 'lower'")

    user = verify_init_data(init_data)
    user_id = user.get("id")
    username = user.get("username") or user.get("first_name") or "Player"

    with db() as conn:
        rnd = get_open_round(conn)
        if rnd is None:
            raise HTTPException(409, "No open round right now")

        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username, points, streak) VALUES (?, ?, 0, 0)",
            (user_id, username),
        )
        conn.execute(
            "UPDATE users SET username=? WHERE user_id=?", (username, user_id)
        )

        existing = conn.execute(
            "SELECT 1 FROM predictions WHERE round_id=? AND user_id=?",
            (rnd["round_id"], user_id),
        ).fetchone()
        if existing:
            raise HTTPException(409, "Already predicted this round")

        conn.execute(
            "INSERT INTO predictions (round_id, user_id, username, choice) VALUES (?, ?, ?, ?)",
            (rnd["round_id"], user_id, username, choice),
        )
        conn.commit()

    return {"ok": True}


app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
