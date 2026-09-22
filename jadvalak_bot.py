#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
جدولک gateway bot — runs 24/7 on GitHub Actions (self-chaining 6h jobs).

Responsibilities:
  - /start and the app deep-link (?start=gate): check that the user is a
    REAL member of the @daily_sciences channel (via getChatMember) before
    handing out the mini-app button. The check runs on EVERY button issue,
    so a user who leaves the channel must rejoin to get back in.
  - The mini-app button URL carries a signed, expiring access token
    (?st=uid.exp.sig) that the app validates (uid bound to initData,
    expiry, format). TTL default: 60 minutes.
  - "I joined" inline button: re-checks membership instantly.
  - /about: "طراحی، ساخت و اجرا توسط @factcaster"
  - Membership checks use a list of bot tokens (any bot that is an admin of
    the channel works — e.g. the channel's posting bot), so the gate works
    from day one without extra BotFather/channel setup.

Run modes (env):
    BOT_TOKEN          the jadvalak bot token           (required)
    CHECKER_TOKENS     comma-separated admin-bot tokens (first = jadvalak itself)
    GATE_CHAT_ID       channel id, e.g. -1001234567890  (required)
    APP_URL            https URL of the mini app        (required)
    TOKEN_TTL_MIN      access-token lifetime, minutes   (default 60)
    RUNTIME_MINUTES    how long to poll this instance   (default 345)
    START_OFFSET       getUpdates offset to resume from (default -1)
    GH_TOKEN / GATEWAY_REPO / GATEWAY_WORKFLOW — used to write the chain
    handoff file consumed by the workflow's next-dispatch step.

Files written:
    chain_offset.txt   next getUpdates offset (read by gateway.yml)
    chain_flag.txt     "chain" or "no-chain" (control.json says off -> stop)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import base64
from urllib.parse import quote

import requests

log = logging.getLogger("jadvalak-gateway")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHECKER_TOKENS = [t.strip() for t in os.environ.get(
    "CHECKER_TOKENS", BOT_TOKEN).split(",") if t.strip()]
GATE_CHAT_ID = os.environ.get("GATE_CHAT_ID", "").strip()
APP_URL = os.environ.get("APP_URL", "").strip().rstrip("/")
TOKEN_TTL_MIN = int(os.environ.get("TOKEN_TTL_MIN", "720"))
RUNTIME_MINUTES = float(os.environ.get("RUNTIME_MINUTES", "345"))
START_OFFSET = int(os.environ.get("START_OFFSET", "-1"))
CONTROL_URL = os.environ.get(
    "CONTROL_URL",
    "https://raw.githubusercontent.com/prauofi21-svg/jadvalak/main/control.json",
)
CHANNEL_URL = "https://t.me/daily_sciences"
BOT_LINK = "https://t.me/jadvalak_bot"

TG_API = "https://api.telegram.org"
UA = {"User-Agent": "JadvalakGateway/1.0"}

SHARE_URL = (
    "https://t.me/share/url?url=" + quote("https://t.me/jadvalak_bot", safe="")
    + "&text=" + quote(
        "🧩 جدولک — هر روز ۱۰ جدول کلمات متقاطع فارسی! بیا بازی کنیم 🎮",
        safe="")
)

ABOUT_TEXT = "طراح و سازنده @factcaster"

# the channel join requirement message shown to non-members
GATE_TEXT = (
    "برای بازی با جدولک 🧩 باید عضو کانال دانشنامهٔ علمی @daily_sciences باشی:\n\n"
    "۱) روی دکمهٔ «عضویت» بزن و عضو شو\n"
    "۲) بعد دکمهٔ «عضو شدم» را بزن تا عضویتت همین‌جا بررسی شود"
)
WELCOME_TEXT = (
    "به جدولک خوش اومدی 🧩\n"
    "هر روز ۱۰ جدول کلمات متقاطع فارسیِ تازه! ✨\n\n"
    "جدول‌ها را به ترتیب حل کن تا قفل جدول بعدی باز شود. 🔓\n"
    "برای دعوت دوستان، دکمهٔ «دعوت از دوستان» همین پایین هست. 📤"
)

_member_cache: dict = {}          # user_id -> (allowed, ts)
_checker_idx: int = 0             # which checker token works
_gate_broken_notified: set = set()


# --------------------------------------------------------------------------- #
#  Telegram helpers                                                             #
# --------------------------------------------------------------------------- #

def tg(method: str, token: str, payload: dict, timeout=(15, 60)) -> dict | None:
    try:
        r = requests.post(f"{TG_API}/bot{token}/{method}", json=payload,
                          headers=UA, timeout=timeout)
        if r.status_code == 200 and r.json().get("ok"):
            return r.json().get("result") or {}
        log.warning("tg %s -> %s %s", method, r.status_code, r.text[:180])
    except requests.RequestException as exc:
        log.warning("tg %s failed: %s", method, exc)
    return None


def send(chat_id: int, text: str, keyboard: dict | None = None) -> int | None:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if keyboard:
        payload["reply_markup"] = keyboard
    res = tg("sendMessage", BOT_TOKEN, payload)
    return (res or {}).get("message_id")


def answer_cb(cb_id: str, text: str = "") -> None:
    tg("answerCallbackQuery", BOT_TOKEN, {"callback_query_id": cb_id, "text": text})


def edit_message(chat_id: int, message_id: int, text: str, keyboard: dict | None = None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": True}
    if keyboard:
        payload["reply_markup"] = keyboard
    tg("editMessageText", BOT_TOKEN, payload)


# --------------------------------------------------------------------------- #
#  Channel membership gate                                                      #
# --------------------------------------------------------------------------- #

def is_member(user_id: int) -> bool | None:
    """
    True/False membership, or None if the check itself failed (no checker bot
    has access to the channel). Result is cached for 90 seconds.
    """
    now = time.time()
    cached = _member_cache.get(user_id)
    if cached and now - cached[1] < 90:
        return cached[0]

    global _checker_idx
    result = None
    for i in range(len(CHECKER_TOKENS)):
        idx = (_checker_idx + i) % len(CHECKER_TOKENS)
        token = CHECKER_TOKENS[idx]
        try:
            r = requests.get(
                f"{TG_API}/bot{token}/getChatMember",
                params={"chat_id": GATE_CHAT_ID, "user_id": user_id},
                headers=UA, timeout=(15, 30),
            )
            if r.status_code == 200 and r.json().get("ok"):
                status = (r.json().get("result") or {}).get("status", "")
                _checker_idx = idx
                result = status in ("creator", "administrator", "member", "restricted")
                _member_cache[user_id] = (result, now)
                return result
            # this token cannot see the channel — try the next one
            log.warning("checker token #%d cannot check membership: %s %s",
                        idx, r.status_code, r.text[:120])
        except requests.RequestException as exc:
            log.warning("checker token #%d request failed: %s", idx, exc)
    return result


# --------------------------------------------------------------------------- #
#  Signed access tokens for the mini app                                        #
# --------------------------------------------------------------------------- #

def _gate_secret() -> bytes:
    return hashlib.sha256(("jadvalak-gate:" + BOT_TOKEN).encode()).digest()


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def make_access_token(user_id: int, ttl_min: int = TOKEN_TTL_MIN) -> str:
    exp = int(time.time()) + ttl_min * 60
    body = f"{user_id}.{exp}"
    sig = hmac.new(_gate_secret(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64u(sig)}"


def app_url_for(user_id: int) -> str:
    return f"{APP_URL}?st={make_access_token(user_id)}"


def verify_access_token(token: str, user_id: int | None) -> bool:
    """Server-side check (also mirrored client-side in the app)."""
    try:
        uid_s, exp_s, sig_s = token.split(".")
        uid, exp = int(uid_s), int(exp_s)
        if exp < time.time():
            return False
        if user_id is not None and uid != user_id:
            return False
        expected = hmac.new(_gate_secret(), f"{uid}.{exp}".encode(),
                            hashlib.sha256).digest()
        got = base64.urlsafe_b64decode(sig_s + "=" * (-len(sig_s) % 4))
        return hmac.compare_digest(expected, got)
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
#  Keyboards                                                                    #
# --------------------------------------------------------------------------- #

def kb_join() -> dict:
    return {"inline_keyboard": [[
        {"text": "📢 عضویت در @daily_sciences", "url": CHANNEL_URL},
    ], [
        {"text": "✅ عضو شدم، ادامه بده", "callback_data": "joined"},
    ], [
        {"text": "📤 دعوت از دوستان", "url": SHARE_URL},
    ]]}


def kb_app(user_id: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "🎮 ورود به جدولک", "web_app": {"url": app_url_for(user_id)}},
    ], [
        {"text": "🔄 بررسی دوبارهٔ عضویت", "callback_data": "joined"},
    ], [
        {"text": "📤 دعوت از دوستان", "url": SHARE_URL},
    ]]}


# --------------------------------------------------------------------------- #
#  Update handling                                                              #
# --------------------------------------------------------------------------- #

def handle_start(chat_id: int, user_id: int, first: bool):
    member = is_member(user_id)
    if member is None:
        # no checker bot has channel access — fail CLOSED with an explanation
        log.error("membership check unavailable for user %s", user_id)
        send(chat_id, (
            "⛔ اتصال ربات به کانال @daily_sciences هنوز برقرار نشده است.\n"
            "لطفاً چند دقیقه بعد دوباره تلاش کنید."
        ))
        return
    if member:
        send(chat_id, WELCOME_TEXT if first else "بیا داخل! 🎮", kb_app(user_id))
    else:
        send(chat_id, GATE_TEXT, kb_join())


def handle_joined(cb) -> None:
    user_id = cb["from"]["id"]
    chat_id = cb["message"]["chat"]["id"]
    message_id = cb["message"]["message_id"]
    member = is_member(user_id)
    if member is None:
        answer_cb(cb["id"], "بررسی عضویت موفق نبود؛ چند لحظه بعد دوباره بزن.")
        return
    if member:
        answer_cb(cb["id"], "✅ خوش اومدی! دکمهٔ «ورود به جدولک» تازه شد.")
        edit_message(chat_id, message_id, WELCOME_TEXT, kb_app(user_id))
    else:
        answer_cb(cb["id"], "هنوز عضویتت تایید نشد! اول عضو @daily_sciences شو.")
        # edit the same message (no spam) with a fresh nudge + join keyboard
        edit_message(chat_id, message_id,
                     GATE_TEXT + "\n\n⏳ هنوز عضویت دیده نشد — عضو شو و دوباره «عضو شدم» را بزن.",
                     kb_join())


def handle_update(upd: dict) -> None:
    # membership gate is also enforced when Telegram delivers a my_chat_member
    # event (user blocks the bot) — nothing to do there, just log.
    if "message" in upd:
        msg = upd["message"]
        chat_id = msg["chat"]["id"]
        user = msg.get("from") or {}
        user_id = user.get("id")
        if msg["chat"].get("type") != "private" or not user_id:
            return
        text = (msg.get("text") or "").strip()
        if text.startswith("/start"):
            handle_start(chat_id, user_id, first=True)
        elif text.startswith("/about") or text.startswith("/درباره"):
            send(chat_id, ABOUT_TEXT)
        elif text.startswith("/help") or text.startswith("/راهنما"):
            send(chat_id, (
                "🧩 جدولک — راهنما\n\n"
                "• هر روز ۱۰ جدول تازه فارسی\n"
                "• با لمس هر خانه، جهت افقی/عمقی عوض می‌شود\n"
                "• با کیبورد داخل خود بازی تایپ کن\n"
                "• هر جدول را حل کن تا بعدی باز شود\n\n" + ABOUT_TEXT
            ))
        else:
            handle_start(chat_id, user_id, first=False)
    elif "callback_query" in upd:
        cb = upd["callback_query"]
        if cb.get("data") == "joined":
            handle_joined(cb)


# --------------------------------------------------------------------------- #
#  Control file (kill switch served from the repo via raw.githubusercontent)    #
# --------------------------------------------------------------------------- #

def gateway_enabled() -> bool:
    try:
        r = requests.get(CONTROL_URL, headers=UA, timeout=(10, 20))
        if r.status_code == 200:
            return "off" not in (r.text or "").strip().lower()
        return True  # unreachable control file -> assume on
    except requests.RequestException:
        return True


# --------------------------------------------------------------------------- #
#  Main loop                                                                    #
# --------------------------------------------------------------------------- #

def get_updates(offset: int, timeout: int) -> tuple:
    """Returns (updates, next_offset) or raises/returns special codes."""
    try:
        r = requests.get(
            f"{TG_API}/bot{BOT_TOKEN}/getUpdates",
            params={"timeout": timeout, "offset": offset, "allowed_updates":
                    json.dumps(["message", "callback_query"])},
            headers=UA, timeout=(timeout + 20, timeout + 20),
        )
        if r.status_code == 409:
            return "CONFLICT", offset
        if r.status_code == 200 and r.json().get("ok"):
            ups = r.json().get("result") or []
            next_off = offset
            for u in ups:
                next_off = max(next_off, u["update_id"] + 1)
            return ups, next_off
        log.warning("getUpdates -> %s %s", r.status_code, r.text[:160])
    except requests.RequestException as exc:
        log.warning("getUpdates failed: %s", exc)
    return [], offset


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-7s  %(message)s",
                        datefmt="%H:%M:%S")
    if not BOT_TOKEN or not GATE_CHAT_ID or not APP_URL:
        log.error("BOT_TOKEN / GATE_CHAT_ID / APP_URL missing")
        return 2

    me = tg("getMe", BOT_TOKEN, {})
    if me:
        log.info("gateway alive as @%s (id=%s)", me.get("username"), me.get("id"))
    else:
        log.error("getMe failed — token invalid?")
        return 2

    offset = START_OFFSET
    deadline = time.time() + RUNTIME_MINUTES * 60
    conflicts = 0
    log.info("polling for %.0f minutes (start offset=%d)", RUNTIME_MINUTES, offset)

    while time.time() < deadline:
        if int(time.time()) % 60 == 0 or True:
            pass
        if not gateway_enabled():
            log.info("control.json says OFF — stopping without chaining")
            Path_flag = open("chain_flag.txt", "w")
            Path_flag.write("no-chain")
            Path_flag.close()
            with open("chain_offset.txt", "w") as fh:
                fh.write(str(offset))
            return 0

        ups, offset = get_updates(offset, 25)
        if ups == "CONFLICT":
            conflicts += 1
            log.warning("409 conflict (%d/12) — another poller is active", conflicts)
            if conflicts >= 12:
                log.info("yielding to the other poller")
                with open("chain_flag.txt", "w") as fh:
                    fh.write("no-chain")
                with open("chain_offset.txt", "w") as fh:
                    fh.write(str(offset))
                return 0
            time.sleep(15)
            continue
        conflicts = 0
        for u in ups:
            try:
                handle_update(u)
            except Exception as exc:
                log.error("update handler crashed: %s", exc)

    # runtime exhausted — hand the offset to the next chained run
    with open("chain_flag.txt", "w") as fh:
        fh.write("chain")
    with open("chain_offset.txt", "w") as fh:
        fh.write(str(offset))
    log.info("runtime over — offset %d handed to the next run", offset)
    return 0


if __name__ == "__main__":
    import sys
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
