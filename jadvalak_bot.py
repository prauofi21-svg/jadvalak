#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
جدولک gateway bot — runs 24/7 on GitHub Actions (self-chaining 6h jobs).

Architecture (2026-10-08 v3 — "verification lives in the bot chat"):
  - The mini app itself NEVER gates: it opens straight into the game. There
    is no join screen, no token dance and no second window inside the app —
    which is exactly what caused the recurring «صفحهٔ مزاحم» complaints.
  - Membership is verified HERE, in the bot chat, BEFORE the mini app ever
    opens: on /start (or any message) and on the «عضو شدم» callback we run
    getChatMember against @daily_sciences. Verified members receive the
    «ورود به جدولک» web_app button (and get it pinned for easy return);
    non-members receive the join flow. The check re-runs on EVERY button
    issue, so a user who leaves the channel must rejoin to get back in.
  - The old web_app chat-menu button is actively RESET to the standard
    commands menu: a frozen menu-button URL could outlive channel
    membership and bypass the chat verification.
  - Button URLs still carry a signed ?st= token + &v=<deploy>: the v param
    busts Telegram's WebView HTML cache (guaranteed fresh code). v2.10: the
    st token is a 4-segment uid.iat.exp.sig LIVE membership vouch — the
    mini app opens the game instantly for its holder (the bot verified
    them in chat at iat) while its continuous re-check demotes on any
    FRESH «no». Legacy 3-segment 30-day tokens on pre-Oct-8 buttons are
    structurally rejected client-side, and the telegram-web-app.js bridge
    redirects any WebView-cached legacy page to the current build before
    its own st validation could ever matter.
  - /about: "طراحی، ساخت و اجرا توسط @factcaster"
  - Membership checks use a list of bot tokens (any bot that is an admin of
    the channel works — e.g. the channel's posting bot), so the gate works
    from day one without extra BotFather/channel setup.

Architecture (2026-10-11 v6 — "real-time membership detection"):
  - The owner's live test showed the v2.5 latencies through the user's eyes:
    leaving the channel and tapping the game button ~a minute later still
    opened the game, and a player who joined from the gate screen sat
    waiting. Root cause was pure LATENCY BUDGET: a 6 s tick that only
    checked 2 users, an 8 s commit gap and a ~50 s client re-check added
    up to ~1-2 minutes end-to-end — functioning, but nowhere near the
    «بدون تأخیر» the owner asked for.
  - v2.6 slashes every hop: the roster tick is 2.5 s and at the current
    scale EVERY known user is re-checked on EVERY tick (leave/join is
    detected within seconds); verdict flips force a members.json commit
    within a 4 s gap; the client polls the roster every ~1.8 s on the gate
    screen and re-verifies every ~7 s while playing. End-to-end both
    directions is now ~5-10 s, and the chat path (/start, «عضو شدم»)
    remains instant as always.
  - members.json gets a 30-minute heartbeat commit so its "updated"
    timestamp always proves the sweep is alive (today's debugging showed
    how valuable that is).
  - The gate screen itself no longer shows the title line the owner asked
    to remove («جدولک برای اعضای کانال است» — removed from index.html) and
    the app's roster fetch now gives raw.githubusercontent a tight budget
    before falling back to the same-origin Pages copy, so networks that
    block raw.githubusercontent don't pay the full serial timeout.

Architecture (2026-10-11 v8 — "legacy cache bridge"):
  - The owner's live test still showed the game opening from an OLD welcome
    message's button. Root cause: pre-2026.10.08 builds — the HTML Telegram's
    WebView serves from its cache for pre-Oct-8 button URLs (?st=<token>,
    no v param) — have NO self-heal and NO membership check: they admit
    anyone holding a signed st token issued while they were still a member
    (30-day TTL in the Sep 26-27 era) or a saved jk:st:<uid> localStorage
    token. No server-side fix can reach those frozen pages.
  - The cure: telegram-web-app.js — the ONE subresource every build since
    2026-09-22 loads BEFORE its own code — now carries a «legacy bridge»:
    it purges the jk:st:* localStorage tokens and, whenever the page URL's
    ?v= is missing or outdated, redirects (location.replace, v swapped in
    place, fail-open) to the current versioned URL BEFORE the legacy st
    gate can run. Cached legacy pages therefore land on the v2.7 live
    getChatMember gate within one subresource revalidation (~HTTP cache
    lifetime). Fresh buttons (v == this deploy) take a zero-cost fast path.
  - TOKEN_TTL dropped 365d -> 1d so no st token in existence can outlive a
    channel leave by more than a day even in a WebView that still serves a
    stale copy of the script. index.html additionally purges jk:st:* keys
    on every current-page boot as a second belt-and-braces layer.

Architecture (2026-10-11 v7 — "sacrificial checker bot, ZERO latency"):
  - The owner created a dedicated checker bot (@Jadval_chekerbot, admin of
    @daily_sciences with can_post=FALSE) and handed us its token. It now
    serves BOTH paths: (a) server-side — the CHECKER_TOKEN secret points
    at it (the previous checker, Agentxza, lost its admin rights and every
    roster sweep check was failing with "chat not found", silently
    freezing the roster); (b) browser-side — index.html v2026.10.11.4 calls
    getChatMember DIRECTLY from the mini app, so every verdict (start
    button, blue open button, gate poll, in-game recheck) is a fresh,
    real-time Telegram answer with no roster lag at all.
  - Browser-side semantics (verified live): 200 + member-ish status →
    member; left/kicked → non-member; 400 PARTICIPANT_ID_INVALID →
    DEFINITIVE non-member; anything else → fall back to the roster.
  - The roster (members.json) remains the fallback tier for networks where
    the direct call cannot run, so the fail-open/fail-closed behavior of
    v2.5/v2.6 is fully preserved. Nothing in this file's runtime logic
    changed for v7 — only the checker token (secret) did.

Architecture (2026-10-11 v5 — "correct getChatMember semantics"):
  - ROOT-CAUSE fix: Telegram now answers getChatMember with 400
    PARTICIPANT_ID_INVALID for anyone who is NOT in the channel (verified
    live: even real never-joined users get 400, not a 200/"left"). The old
    code read every non-200 as "checker broken" -> None -> the scary ⛔
    message, and (worse) the roster thread treated it as "check failed" so
    a member who LEFT the channel stayed "member" in the roster FOREVER —
    exactly the owner's bug: «کانال را ترک کرده اما جدولک هنوز باز می‌شود».
    Now: 400 PARTICIPANT_ID_INVALID/USER_ID_INVALID -> definitive False;
    400 chat-not-found -> that token can't see the channel (try next);
    429 -> retry_after honoured; only true infra failure yields None, and
    even then the chat falls back to the last roster verdict instead of
    ever showing an error message.
  - members.json v3 carries BOTH the member hashes and a `left` list
    (salted hashes of known NON-members). v2.9 RETIRES the grace window:
    an unknown hash now meets the gate too — the owner's test account
    (never seen by the roster) sailed into the game seconds after leaving
    precisely because grace admitted every unknown until Oct 14. One
    /start in the chat verifies a real member LIVE and adds them to the
    roster forever.
  - The sweep is real-time at small scale and adaptive at large: with
    ≤ 16 known users every user is checked every ~2.5 s tick; beyond that,
    non-members (anyone waiting on the gate screen) keep absolute priority
    while members rotate oldest-check-first. Verdict flips force an
    immediate members.json commit (≤ 4 s) so a rejoining player staring at
    the gate screen is auto-let-in within seconds.
  - Cold start: the roster seeds itself with the channel's human admins
    (getChatAdministrators), so the owner's accounts are covered from the
    first minute after a roster reset.
  - Bot-authored updates (from.is_bot) are ignored — a forwarded/re-sent
    bot message once produced a bogus "user" id that failed every check.

Architecture (2026-10-11 v4 — "live membership roster"):
  - The mini app gates itself against a PUBLIC roster file (data/members.json)
    that THIS bot maintains: a background thread keeps re-checking every
    known user with getChatMember and commits the salted hashes of current
    members to the repo. The app fetches that file (cache-busted, straight
    from raw.githubusercontent so a Pages rebuild is never on the critical
    path) and compares it with its own user-id hash:
      member          -> straight into the game, zero friction (as before)
      non-member      -> one quiet join screen with a «عضویت» button; the
                         screen AUTO-POLLS the roster, so the moment the user
                         joins the channel (usually < 10-15 s) the game opens
                         by itself — no second window, no bot spam, no «عضو
                         شدم» round-trip. THIS is the fix for the user's
                         complaint that a member who LEFT the channel could
                         keep playing: leavers drop out of the roster within
                         the sweep interval (~25 min) and are gated on their
                         next open.
  - Freshness tiers of the roster thread:
      hot set   (unknown / non-member users)  — re-checked every ~6 s so a
                  fresh join is reflected almost instantly
      full sweep (everyone, oldest-check first) — one pass every ~25 min so
                  a leave is caught quickly even for dormant users
  - Known-user ids are persisted ENCRYPTED in data/roster.enc (key derived
    from BOT_TOKEN, which lives only in GitHub Secrets) — a public repo
    never sees plaintext Telegram ids; members.json itself carries only
    salted hashes of ids (salt is public and only prevents enumeration).
  - Chat verification stays as it was: /start, any message and the «عضو
     شدم» callback run getChatMember before the web_app button is issued;
     a successful verification is written to the roster + members.json is
     committed right away, so the freshly-issued button opens instantly.
  - control.json can carry {"grace_until": <epoch>} — legacy field, no
    longer emitted into members.json (the v2.9 client ignores it entirely
    and unknowns are gated); kept only so old control files parse clean.

Run modes (env):
    BOT_TOKEN          the jadvalak bot token           (required)
    CHECKER_TOKENS     comma-separated admin-bot tokens (first = jadvalak itself)
    GATE_CHAT_ID       channel id, e.g. -1001234567890  (required)
    APP_URL            https URL of the mini app        (required)
    TOKEN_TTL_MIN      access-token lifetime, minutes   (default 1440 = 1 day)
    RUNTIME_MINUTES    how long to poll this instance   (default 345)
    START_OFFSET       getUpdates offset to resume from (default -1)
    GH_TOKEN / GATEWAY_REPO / GATEWAY_WORKFLOW — used to write the chain
    handoff file consumed by the workflow's next-dispatch step.

Files written:
    chain_offset.txt   next getUpdates offset (read by gateway.yml)
    chain_flag.txt     "chain" or "no-chain" (control.json says off -> stop)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

log = logging.getLogger("jadvalak-gateway")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHECKER_TOKENS = [t.strip() for t in os.environ.get(
    "CHECKER_TOKENS", BOT_TOKEN).split(",") if t.strip()]
GATE_CHAT_ID = os.environ.get("GATE_CHAT_ID", "").strip()
APP_URL = os.environ.get("APP_URL", "").strip().rstrip("/")
APP_V = "2026.10.12.4"   # deploy version — keep in sync with index.html APP_V and version.json
TOKEN_TTL_MIN = int(os.environ.get("TOKEN_TTL_MIN", "1440"))  # 1 day: the ?st= token is the bot's LIVE membership vouch. v2.10 made the client honor it again (instant entry after a chat verification), but ONLY in the new 4-segment uid.iat.exp.sig format whose lifetime is capped at ~24 h — the legacy 3-segment 30-day tokens on pre-Oct-8 welcome-message buttons are structurally rejected client-side, so that hole stays shut. The continuous in-app re-check still demotes on any FRESH «no».
RUNTIME_MINUTES = float(os.environ.get("RUNTIME_MINUTES", "345"))
START_OFFSET = int(os.environ.get("START_OFFSET", "-1"))

# ---- player statistics (unique-player counter shown in the mini app) ----
GH_TOKEN = os.environ.get("GH_TOKEN", "")           # GITHUB_TOKEN of the run
STATS_REPO = os.environ.get("GITHUB_REPOSITORY", "prauofi21-svg/jadvalak")
TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30))
STATS_USERS_FILE = Path("data/users.json")
STATS_STATS_FILE = Path("data/stats.json")
CONTROL_URL = os.environ.get(
    "CONTROL_URL",
    "https://raw.githubusercontent.com/prauofi21-svg/jadvalak/main/control.json",
)
CHANNEL_URL = "https://t.me/daily_sciences"
BOT_LINK = "https://t.me/jadvalak_bot"

# ---- live membership roster (public members.json for the mini app) ----
# MUST stay identical to MEMBER_SALT in index.html.
MEMBER_SALT = "2adeb51be375c1ab70e971569537f425"
MEMBERS_PATH = "data/members.json"          # public, client-served
ROSTER_PATH = "data/roster.enc"             # encrypted ids (bot token key)
RAW_BASE = ("https://raw.githubusercontent.com/"
            "prauofi21-svg/jadvalak/main/")
HOT_INTERVAL = 2.5        # s between roster ticks (v2.6: was 6 — flips now land in seconds)
HOT_PER_TICK = 25         # max hot (non-member/unknown) users re-checked per tick
FAST_ALL_MAX = 16         # ≤ this many known users: EVERYONE is checked EVERY tick
SWEEP_BUDGET = 10         # max getChatMember calls per tick once N > FAST_ALL_MAX
MEMBERS_COMMIT_GAP = 4.0     # s, min gap between members.json commits (fast flips)
MEMBERS_KEEPALIVE = 1800.0   # s, heartbeat commit so members.json freshness is visible
LEFT_SEEN_DAYS = 60          # a hash stays in `left` while the user was seen recently
ROSTER_COMMIT_GAP = 1200.0   # s, min gap between roster.enc commits (20 min)
ROSTER_NEW_IDS_FLUSH = 8     # commit roster.enc early when this many new ids

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

FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa_num(n: int) -> str:
    """1234 -> ۱۲۳۴ — same digit style as the mini app's fa()."""
    return str(int(n)).translate(FA_DIGITS)


def welcome_text() -> str:
    """Welcome shown in the bot chat — includes the live player count
    (mirrors the counter the mini app shows, from data/stats.json)."""
    txt = (
        "به جدولک خوش اومدی 🧩\n"
        "هر روز ۱۰ جدول کلمات متقاطع فارسیِ تازه! ✨\n\n"
        "جدول‌ها را به ترتیب حل کن تا قفل جدول بعدی باز شود. 🔓"
    )
    if _total_players >= 1:
        txt += f"\n👥 بازیکن‌های جدولک تا امروز: {fa_num(_total_players)}"
    txt += "\n\nبرای دعوت دوستان، دکمهٔ «دعوت از دوستان» همین پایین هست. 📤"
    return txt

_member_cache: dict = {}          # user_id -> (allowed, ts)
_checker_idx: int = 0             # which checker token works
_gate_broken_notified: set = set()
_pinned_chats: set = set()        # chats whose game-button message we pinned
_gh_lock = threading.Lock()       # serialises Contents-API writes
_control_grace: int = 0           # legacy (v2.9): no longer emitted — kept for control.json compat

# ---- stats state ----
_seen_users: set = set()          # salted hashes of counted player ids
_total_players: int = 0
_today_players: int = 0
_stats_day: str = ""
_stats_dirty: bool = False
_stats_last_commit: float = 0.0


def _stats_salt() -> str:
    """Secret salt: without the bot token nobody can map hashes to ids."""
    return hashlib.sha256(("jadvalak-stats:" + BOT_TOKEN).encode()).hexdigest()


def _uhash(uid: int) -> str:
    return hashlib.sha256((_stats_salt() + str(uid)).encode()).hexdigest()


def tehran_day() -> str:
    return datetime.now(TEHRAN_TZ).strftime("%Y-%m-%d")


def stats_load() -> None:
    """Load committed counters from the run's checkout of main."""
    global _seen_users, _total_players, _today_players, _stats_day
    _stats_day = tehran_day()
    try:
        users = json.loads(STATS_USERS_FILE.read_text(encoding="utf-8"))
        _seen_users = set(users.get("users", []))
        _total_players = len(_seen_users)
    except (OSError, ValueError):
        _seen_users, _total_players = set(), 0
    try:
        st = json.loads(STATS_STATS_FILE.read_text(encoding="utf-8"))
        if st.get("day") == _stats_day:
            _today_players = int(st.get("today", 0))
    except (OSError, ValueError):
        pass
    log.info("stats: %d known players", _total_players)


def count_user(uid: int) -> None:
    """Count a unique player (no-op for already-known ids)."""
    global _total_players, _today_players, _stats_dirty
    h = _uhash(uid)
    if h in _seen_users:
        return
    _seen_users.add(h)
    _total_players += 1
    _today_players += 1
    _stats_dirty = True


def _gh_put(path: str, obj, msg: str) -> bool:
    """Thread-safe Contents-API write (stats main loop AND roster thread both
    use it). GET current sha -> PUT; on conflict/failure one retry with a
    fresh sha. Never raises."""
    if not GH_TOKEN:
        return False
    headers = {"Authorization": f"token {GH_TOKEN}",
               "Accept": "application/vnd.github+json",
               "User-Agent": "jadvalak-stats"}
    url = f"https://api.github.com/repos/{STATS_REPO}/contents/{path}"
    data = base64.b64encode(
        json.dumps(obj, ensure_ascii=False).encode()).decode()
    with _gh_lock:
        for attempt in (1, 2):
            try:
                get = requests.get(f"{url}?ref=main", headers=headers, timeout=30)
                body = {"message": msg, "branch": "main", "content": data}
                if get.status_code == 200:
                    body["sha"] = get.json()["sha"]
                put = requests.put(url, headers=headers, json=body, timeout=30)
                if put.status_code in (200, 201):
                    return True
                log.warning("gh put %s attempt %d -> %s %s", path, attempt,
                            put.status_code, put.text[:160])
            except requests.RequestException as exc:
                log.warning("gh put %s failed: %s", path, exc)
        return False


def maybe_commit_stats(force: bool = False) -> None:
    """Throttled commit of the counters (every 5 min when dirty, or forced
    at run end). NEVER raises: a stats problem must not kill the poller.
    Failures retry on the next throttle window."""
    global _stats_dirty, _stats_last_commit, _stats_day, _today_players
    try:
        if not _stats_dirty or not GH_TOKEN:
            return
        now = time.time()
        if not force and now - _stats_last_commit < 300:
            return
        day = tehran_day()
        if day != _stats_day:            # midnight rollover: reset today's count
            _stats_day = day
            _today_players = 0
        payload = {"total": _total_players, "today": _today_players, "day": _stats_day,
                   "updated": datetime.now(timezone.utc).isoformat()[:19] + "Z"}
        ok1 = _gh_put("data/users.json", {"users": sorted(_seen_users), "updated": _stats_day},
                      "chore(stats): unique players [skip ci]")
        ok2 = _gh_put("data/stats.json", payload, "chore(stats): counters [skip ci]")
        _stats_last_commit = now
        if ok1 and ok2:
            _stats_dirty = False
            log.info("stats committed: total=%d today=%d", _total_players, _today_players)
        else:
            log.warning("stats commit failed — retrying later")
    except Exception as exc:
        # belt & braces: keep the poller alive no matter what stats does
        log.error("stats commit crashed (ignored): %s", exc)
        _stats_last_commit = time.time()   # back off before retrying


# --------------------------------------------------------------------------- #
#  Telegram helpers                                                             #
# --------------------------------------------------------------------------- #

def tg(method: str, token: str, payload: dict, timeout=(15, 60)) -> dict | None:
    try:
        r = requests.post(f"{TG_API}/bot{token}/{method}", json=payload,
                          headers=UA, timeout=timeout)
        if r.status_code == 200:
            try:
                if r.json().get("ok"):
                    return r.json().get("result") or {}
            except ValueError:
                pass
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

MEMBER_STATUSES = ("creator", "administrator", "member", "restricted")
# Telegram's getChatMember (verified live 2026-10-11) answers 400 with these
# descriptions for anyone NOT in the channel — a DEFINITIVE non-member, not
# a checker failure. Anything else (chat not found…) means THIS token cannot
# see the chat and the next checker token must be tried.
NOT_MEMBER_ERRORS = ("PARTICIPANT_ID_INVALID", "USER_ID_INVALID", "user not found")


def _member_verdict_raw(uid: int) -> bool | None:
    """getChatMember with CORRECT semantics:
      200 + member-ish status          -> True
      200 + left/kicked                -> False
      400 PARTICIPANT_ID_INVALID & co  -> False   (definitively not a member)
      400 chat not found / other       -> this token can't see the channel
      429                             -> honour retry_after, retry once
    Returns None ONLY when every token failed on infrastructure — callers
    then fall back to the last roster verdict instead of erroring at the
    user. Sticky _checker_idx keeps the working token first."""
    global _checker_idx
    for i in range(len(CHECKER_TOKENS)):
        idx = (_checker_idx + i) % len(CHECKER_TOKENS)
        token = CHECKER_TOKENS[idx]
        for _attempt in range(2):          # one calm retry for transient blips
            try:
                r = requests.get(
                    f"{TG_API}/bot{token}/getChatMember",
                    params={"chat_id": GATE_CHAT_ID, "user_id": uid},
                    headers=UA, timeout=(3, 6),
                )
                code = r.status_code
                if code == 200:
                    try:
                        body = r.json()
                    except ValueError:
                        break              # non-JSON -> next token
                    if body.get("ok"):
                        status = (body.get("result") or {}).get("status", "")
                        _checker_idx = idx
                        return status in MEMBER_STATUSES
                    break                 # 200 but not ok -> next token
                if code == 400:
                    desc = (r.text or "")[:200]
                    if any(e in desc for e in NOT_MEMBER_ERRORS):
                        _checker_idx = idx
                        return False      # DEFINITIVE non-member
                    log.warning("checker token #%d cannot check membership: %s %s",
                                idx, code, desc[:120])
                    break                # token can't see the chat -> next
                if code == 429:
                    try:
                        wait_s = min(float(
                            (r.json().get("parameters") or {}).get("retry_after", 3)), 5.0)
                    except ValueError:
                        wait_s = 3.0
                    time.sleep(wait_s)
                    continue              # retry the same token
                # 5xx / anything else — brief pause, retry once, then next
                log.warning("checker token #%d check oddity: %s %s",
                            idx, code, (r.text or "")[:120])
                time.sleep(0.4)
                continue
            except requests.RequestException as exc:
                log.warning("checker token #%d request failed: %s", idx, exc)
                time.sleep(0.4)
                continue
    return None


def is_member(user_id: int) -> bool | None:
    """
    True/False membership, or None only on real infrastructure failure.
    Result is cached for 90 seconds.
    """
    now = time.time()
    cached = _member_cache.get(user_id)
    if cached and now - cached[1] < 90:
        return cached[0]
    result = _member_verdict_raw(user_id)
    if result is not None:
        _member_cache[user_id] = (result, now)
    return result


# --------------------------------------------------------------------------- #
#  Live membership roster (public data/members.json maintained here)            #
# --------------------------------------------------------------------------- #
# The roster thread keeps getChatMember results for every user we know and
# publishes the salted hashes of CURRENT members so the mini app can gate
# itself without any backend. Two freshness tiers (see docstring):
#   hot   — unknown/non-member users, re-checked every tick (~6 s)
#   sweep — everyone, oldest check first, a full pass every ~25 min

_roster: dict = {}                 # uid -> {"m": bool|None, "ts": epoch, "seen": epoch}
_roster_lock = threading.Lock()
_roster_hot_cursor = 0
_roster_new_ids = 0
_roster_last_save = 0.0
_members_last_commit = 0.0
_members_last_hashes: frozenset = frozenset()
_members_last_left: frozenset = frozenset()
_members_last_grace: int = -1     # legacy (v2.9): grace retired, field unused
_members_flip = False               # a verdict flipped -> force the next commit


def member_hash(uid: int) -> str:
    """Salted, truncated id hash — IDENTICAL to the mini app's computation
    (sha256(MEMBER_SALT + ":" + id), first 16 hex chars)."""
    return hashlib.sha256(
        f"{MEMBER_SALT}:{uid}".encode()).hexdigest()[:16]


def _roster_key() -> bytes:
    """roster.enc encryption key — derived from the bot token (GitHub Secret),
    so the public repo never stores plaintext ids."""
    return hashlib.sha256(("jadvalak-roster:" + BOT_TOKEN).encode()).digest()


def _xor_stream(data: bytes) -> bytes:
    """HMAC-SHA256 in counter mode as a keystream (encrypt == decrypt)."""
    key = _roster_key()
    out = bytearray(len(data))
    for blk in range(0, len(data), 32):
        ks = hmac.new(key, str(blk // 32).encode(), hashlib.sha256).digest()
        chunk = data[blk:blk + 32]
        for i, b in enumerate(chunk):
            out[blk + i] = b ^ ks[i]
    return bytes(out)


def _roster_encrypt(obj) -> str:
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
    return base64.b64encode(_xor_stream(raw)).decode()


def _roster_decrypt(text: str):
    try:
        raw = _xor_stream(base64.b64decode(text.strip()))
        obj = json.loads(raw.decode())
        if isinstance(obj, dict):
            return obj
    except (ValueError, TypeError):
        pass
    return None


def roster_load() -> None:
    """Seed the roster from the run checkout, preferring the freshest
    roster.enc from raw.githubusercontent when reachable (the chained run's
    checkout can be several hours old)."""
    global _roster_new_ids, _roster_last_save
    best = None
    try:
        local = _roster_decrypt(Path(ROSTER_PATH).read_text(encoding="utf-8"))
        if local:
            best = local
    except OSError:
        pass
    try:
        r = requests.get(RAW_BASE + ROSTER_PATH + f"?t={int(time.time())}",
                         headers=UA, timeout=(10, 20))
        if r.status_code == 200:
            remote = _roster_decrypt(r.text)
            if remote and (not best or remote.get("saved", 0) > best.get("saved", 0)):
                best = remote
    except requests.RequestException:
        pass
    if not best:
        log.info("roster: empty (cold start) — members.json starts fresh")
        return
    ids = best.get("ids") or {}
    if not isinstance(ids, dict):
        return
    with _roster_lock:
        for uid_s, st in ids.items():
            try:
                _roster[int(uid_s)] = {
                    "m": st.get("m"),          # None -> needs a check
                    "ts": float(st.get("ts") or 0),
                    "seen": float(st.get("seen") or 0),
                }
            except (ValueError, TypeError):
                continue
    _roster_new_ids = 0
    _roster_last_save = time.time()
    log.info("roster: loaded %d known users (%d currently member)",
             len(_roster), sum(1 for v in _roster.values() if v.get("m")))


def roster_save(force: bool = False) -> bool:
    """Persist the encrypted id roster (throttled; early-flush when many new
    ids arrived; forced at run end). Best-effort — a failure retries on a
    later tick."""
    global _roster_last_save, _roster_new_ids
    now = time.time()
    if (not force and now - _roster_last_save < ROSTER_COMMIT_GAP
            and _roster_new_ids < ROSTER_NEW_IDS_FLUSH):
        return False
    if not GH_TOKEN:
        return False
    with _roster_lock:
        payload = {"saved": int(now), "ids": {
            str(uid): {"m": st.get("m"), "ts": int(st.get("ts") or 0),
                       "seen": int(st.get("seen") or 0)}
            for uid, st in _roster.items()}}
    ok = _gh_put(ROSTER_PATH, {"enc": _roster_encrypt(payload)},
                 "chore(roster): encrypted user ids [skip ci]")
    if ok:
        _roster_last_save = now
        _roster_new_ids = 0
        log.info("roster: saved %d ids (encrypted)", len(payload["ids"]))
    return ok


def roster_touch(uid: int, member: bool | None = None) -> None:
    """Record that we just saw this user (and optionally a fresh membership
    verdict). New ids flag an early roster_save."""
    global _roster_new_ids
    with _roster_lock:
        st = _roster.get(uid)
        if st is None:
            _roster_new_ids += 1
            st = {"m": None, "ts": 0.0, "seen": 0.0}
            _roster[uid] = st
        st["seen"] = time.time()
        if member is not None:
            st["m"] = bool(member)
            st["ts"] = st["seen"]


def check_member_raw(uid: int) -> bool | None:
    """Direct verdict (no 90 s cache) — used by the roster thread. A False
    here is what flips a leaver out of the member set."""
    result = _member_verdict_raw(uid)
    if result is not None:
        _member_cache[uid] = (result, time.time())
    return result


def _roster_apply(uid: int, result: bool | None) -> None:
    """Store a check result; None (check failed) keeps the previous value.
    A genuine flip (True<->False) flags members.json for an immediate
    commit so the gate screen watching the roster updates within seconds."""
    global _members_flip
    if result is None:
        return
    with _roster_lock:
        st = _roster.setdefault(uid, {"m": None, "ts": 0.0, "seen": 0.0})
        if st.get("m") is not bool(result):
            _members_flip = True
        st["m"] = bool(result)
        st["ts"] = time.time()
    _member_cache[uid] = (bool(result), time.time())


def _roster_next_batch() -> list:
    """v2.6: which ids to check THIS tick.
    At the current scale (N <= FAST_ALL_MAX) EVERY known user is re-checked
    EVERY tick (~2.5 s) — leaving or joining the channel is detected
    essentially in real time; a member who opens the game seconds after
    leaving is already gated, and a player waiting on the gate screen is
    admitted within one tick of joining.
    Once N grows past FAST_ALL_MAX the per-tick API budget is capped:
      hot (unknown/non-member, freshest-seen first, rotating window) keep
      absolute priority — anyone ON the gate screen is verified within one
      tick of joining — while members rotate oldest-check-first (active
      users first), so leave-detection stays fast for real players."""
    global _roster_hot_cursor
    now = time.time()
    with _roster_lock:
        items = list(_roster.items())
    if len(items) <= FAST_ALL_MAX:
        items.sort(key=lambda kv: -(kv[1].get("seen") or 0))   # freshest first
        return [uid for uid, _ in items]
    hot = [(uid, st) for uid, st in items if not st.get("m")]
    hot.sort(key=lambda kv: -(kv[1].get("seen") or 0))
    if len(hot) > HOT_PER_TICK:
        start = _roster_hot_cursor % len(hot)
        _roster_hot_cursor = (_roster_hot_cursor + HOT_PER_TICK) % len(hot)
        window = hot[start:start + HOT_PER_TICK] + hot[:max(0, start + HOT_PER_TICK - len(hot))]
    else:
        window = hot
    # sweep pool: users WITH a verdict (unknowns are already covered by the
    # hot window — giving them sweep slots would starve stale re-checks).
    # Members ALWAYS keep at least 2 slots per tick: a permanently-full hot
    # window (e.g. many long-time leavers) must never starve leave-detection
    # for the actual players.
    known = [kv for kv in items if kv[1].get("m") is not None]
    sweep = sorted(known, key=lambda kv: (
        (kv[1].get("seen") or 0) < now - 86400,     # active users first
        kv[1].get("ts") or 0,                        # then oldest check
    ))[:max(2, SWEEP_BUDGET - len(window))]
    out, seen = [], set()
    for uid, _ in window + sweep:
        if uid not in seen:
            seen.add(uid)
            out.append(uid)
    return out


def _seed_admins() -> None:
    """Cold-start seed: learn the channel's HUMAN admins (the owner's own
    accounts) right away, so after a roster reset the gate can hard-block a
    known leaver from the first minute instead of waiting for a chat
    interaction. Best effort — never raises."""
    global _checker_idx
    for i in range(len(CHECKER_TOKENS)):
        idx = (_checker_idx + i) % len(CHECKER_TOKENS)
        token = CHECKER_TOKENS[idx]
        try:
            r = requests.get(
                f"{TG_API}/bot{token}/getChatAdministrators",
                params={"chat_id": GATE_CHAT_ID}, headers=UA, timeout=(10, 20),
            )
            if r.status_code == 200 and r.json().get("ok"):
                _checker_idx = idx
                seeded = 0
                for a in (r.json().get("result") or []):
                    u = a.get("user") or {}
                    uid = u.get("id")
                    if uid and not u.get("is_bot"):
                        roster_touch(int(uid))
                        seeded += 1
                log.info("roster: seeded %d channel admins (cold start)", seeded)
                return
        except (requests.RequestException, ValueError):
            continue
    log.warning("roster: admin seeding failed (no checker could list admins)")


def members_payload() -> dict:
    """v3: members AND `left` (salted hashes of recently-seen known
    non-members). The client hard-gates `left` hashes — and, since v2.9,
    unknown hashes too: the grace_until cold-start window is retired, so
    the field is no longer emitted (a stale client that still understands
    it would only ever see it absent, which reads as no grace)."""
    cutoff = time.time() - LEFT_SEEN_DAYS * 86400
    with _roster_lock:
        members = sorted(member_hash(uid) for uid, st in _roster.items()
                         if st.get("m"))
        left = sorted(member_hash(uid) for uid, st in _roster.items()
                      if st.get("m") is False
                      and (st.get("seen") or 0) > cutoff)
    return {"v": 3, "n": len(members), "members": members, "left": left,
            "updated": datetime.now(timezone.utc).isoformat()[:19] + "Z"}


def members_commit(force: bool = False) -> bool:
    """Publish data/members.json — when the member set or the left set
    changed (or forced right after a chat verification / a verdict flip).
    The client always fetches it cache-busted from raw.githubusercontent
    AND the Pages mirror in parallel (newest wins), so a Pages rebuild is
    never on the critical path."""
    global _members_last_commit, _members_last_hashes, _members_last_left
    global _members_flip
    payload = members_payload()
    new_hashes = frozenset(payload["members"])
    new_left = frozenset(payload.get("left") or [])
    now = time.time()
    if not force:
        if (new_hashes == _members_last_hashes
                and new_left == _members_last_left):
            return False
        if now - _members_last_commit < MEMBERS_COMMIT_GAP:
            return False           # changed but throttled — retried next tick
    if not GH_TOKEN:
        return False
    ok = _gh_put(MEMBERS_PATH, payload, "chore(roster): members.json [skip ci]")
    _members_last_commit = now
    if ok:
        _members_last_hashes = new_hashes
        _members_last_left = new_left
        _members_flip = False
        log.info("roster: members.json committed — %d members, %d left",
                 payload["n"], len(new_left))
    return ok


def _maybe_welcome_back(uid: int) -> None:
    """v2.10: the sweep just saw a known user (re)join the channel. If the
    bot chat has been quiet for a few minutes (no handler just served them
    there), push ONE message with a fresh verified game button — a user who
    joined and is staring at the mini app's gate screen, or who already
    closed it, gets a visible one-tap way in the moment membership is
    confirmed server-side (the roster flip the gate poll waits for lands
    at the same time). Best-effort: failures are logged once and never
    retried for that uid on this run."""
    try:
        with _roster_lock:
            seen = (_roster.get(uid) or {}).get("seen") or 0
        if time.time() - seen < 180:
            return            # a handler just talked to them in-chat already
        send(uid, "🎉 عضویتت در @daily_sciences تایید شد! هر وقت خواستی از همین دکمه وارد جدولک شو:",
             kb_app(uid))
        log.info("roster: welcome-back pushed to %s", uid)
    except Exception as exc:
        log.warning("welcome-back push failed for %s: %s", uid, exc)


def roster_loop() -> None:                       # daemon thread — never dies
    log.info("roster: thread alive (tick=%ss, N=%d — every user every tick)",
             HOT_INTERVAL, len(_roster))
    first = True
    keepalive = time.time()
    welcomed: set[int] = set()      # v2.10: one welcome-back push per uid per run
    while True:
        t0 = time.time()
        try:
            if GATE_CHAT_ID and CHECKER_TOKENS:
                if first:
                    _seed_admins()      # cold start: know the owner's accounts
                    # make sure data/members.json EXISTS on main even with
                    # an empty roster (v2.9: no grace field any more)
                    members_commit(force=True)
                    first = False
                for uid in _roster_next_batch():
                    with _roster_lock:
                        prev = (_roster.get(uid) or {}).get("m")
                    res = check_member_raw(uid)
                    _roster_apply(uid, res)
                    # v2.10: a leave->join flip the SWEEP discovered (nobody
                    # pressed anything in chat) deserves a visible push
                    if res is True and prev is False and uid not in welcomed:
                        welcomed.add(uid)
                        _maybe_welcome_back(uid)
                # a verdict flip (member left / leaver rejoined) must reach
                # the gate screen ASAP — force past the throttle
                if _members_flip:
                    members_commit(force=True)
                elif time.time() - keepalive > MEMBERS_KEEPALIVE:
                    members_commit(force=True)   # heartbeat: proves the sweep is alive
                    keepalive = time.time()
                else:
                    members_commit()
            roster_save()
        except Exception as exc:                 # absolutely never crash the bot
            log.warning("roster tick crashed (ignored): %s", exc)
        # v2.6: cadence-corrected sleep — the tick's own runtime no longer
        # stretches the cycle, so "every user every 2.5 s" means exactly that
        time.sleep(max(0.4, HOT_INTERVAL - (time.time() - t0)))


# --------------------------------------------------------------------------- #
#  Signed access tokens for the mini app                                        #
# --------------------------------------------------------------------------- #

def _gate_secret() -> bytes:
    return hashlib.sha256(("jadvalak-gate:" + BOT_TOKEN).encode()).digest()


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def make_access_token(user_id: int, ttl_min: int = TOKEN_TTL_MIN) -> str:
    """v2.10 format: uid.iat.exp.sig — FOUR segments. The iat (issued-at)
    lets the client verify the token's TOTAL lifetime (<= ~25 h) without
    the secret, which is what cleanly retires every legacy 3-segment
    30-day token still sitting on old welcome-message buttons: they fail
    the 4-part structural check and fall back to the full live verdict.
    A valid new token means "the bot verified this user's membership
    LIVE at iat", so the mini app opens the game instantly and its
    continuous re-check keeps watching for a later leave."""
    iat = int(time.time())
    exp = iat + ttl_min * 60
    body = f"{user_id}.{iat}.{exp}"
    sig = hmac.new(_gate_secret(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64u(sig)}"


def app_url_for(user_id: int) -> str:
    """App URL with a fresh signed token AND the deploy version param.
    The &v= param matters: Telegram's WebView caches mini-app HTML far
    longer than HTTP headers allow. A URL carrying a NEW version string is
    a guaranteed cache miss, so every button the bot issues after a deploy
    opens the LATEST code. Keep APP_V in sync with index.html/version.json."""
    sep = "&" if "?" in APP_URL else "?"
    return f"{APP_URL}{sep}st={make_access_token(user_id)}&v={APP_V}"


def set_menu_button(chat_id: int) -> None:
    """RESET this chat's menu button to the standard COMMANDS menu.
    Older builds installed a web_app «جدولک» menu button whose URL is frozen
    — it can outlive channel membership and bypass the chat verification.
    The game now opens ONLY through the verified buttons in the chat flow,
    exactly as the owner asked: membership + verification happen here in
    the bot chat, and only then does the mini app open. Best-effort."""
    try:
        tg("setChatMenuButton", BOT_TOKEN, {
            "chat_id": chat_id,
            "menu_button": {"type": "commands"},
        })
    except Exception as exc:
        log.warning("setChatMenuButton reset failed: %s", exc)


def set_commands() -> None:
    """Fill the standard commands menu (shown by the commands menu button)
    with Persian descriptions — a useful, non-bypassing menu."""
    tg("setMyCommands", BOT_TOKEN, {"commands": [
        {"command": "start", "description": "شروع بازی جدولک 🧩"},
        {"command": "about", "description": "درباره جدولک"},
        {"command": "help", "description": "راهنمای بازی"},
    ]})


def pin_message(chat_id: int, message_id: int | None) -> None:
    """Pin the game-button message (best-effort, once per chat per run) so
    returning players always have a one-tap entry pinned at the top of the
    bot chat. Private chats allow bots to pin without extra rights."""
    if not message_id or chat_id in _pinned_chats:
        return
    try:
        ok = tg("pinChatMessage", BOT_TOKEN, {
            "chat_id": chat_id,
            "message_id": message_id,
            "disable_notification": True,
        })
        if ok is not None:
            _pinned_chats.add(chat_id)
    except Exception as exc:
        log.warning("pinChatMessage failed: %s", exc)


def verify_access_token(token: str, user_id: int | None) -> bool:
    """Server-side check of the v2.10 4-segment format (uid.iat.exp.sig).
    Legacy 3-segment tokens are rejected outright; a valid token must be
    unexpired, issued at most ~5 min in the future (clock skew), and have
    a total lifetime of at most ~25 h (the client enforces the same caps
    structurally — the signature is only verifiable here)."""
    try:
        parts = token.split(".")
        if len(parts) != 4:
            return False
        uid_s, iat_s, exp_s, sig_s = parts
        uid, iat, exp = int(uid_s), int(iat_s), int(exp_s)
        now = time.time()
        if exp < now:
            return False
        if iat > now + 300:
            return False
        if exp - iat > 86400 + 3600:      # <= ~25 h lifetime
            return False
        if user_id is not None and uid != user_id:
            return False
        expected = hmac.new(_gate_secret(), f"{uid}.{iat}.{exp}".encode(),
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
    count_user(user_id)
    member = is_member(user_id)
    if member is None:
        time.sleep(1.2)               # one calm retry — transient blips only
        member = is_member(user_id)
    if member is None:
        # checker genuinely unreachable: fall back to the last KNOWN roster
        # verdict — a member keeps playing, everyone else gets the normal
        # join flow. The user NEVER sees an infrastructure error message.
        with _roster_lock:
            member = (_roster.get(user_id) or {}).get("m")
        if member is None:
            member = False
        roster_touch(user_id)          # seen — previous verdict untouched
        log.warning("membership check unavailable for user %s (roster fallback: %s)",
                    user_id, member)
    else:
        roster_touch(user_id, member)  # roster learns the id + fresh verdict
    set_menu_button(chat_id)          # reset any legacy web_app menu button
    if member:
        # verified HERE in the chat — only now does the mini app open.
        # Publish the roster right away so the button we are about to send
        # opens instantly (no gate flash) in the mini app.
        members_commit(force=True)
        mid = send(chat_id, welcome_text() if first else "بیا داخل! 🎮", kb_app(user_id))
        pin_message(chat_id, mid)
    else:
        send(chat_id, GATE_TEXT, kb_join())


def handle_joined(cb) -> None:
    user_id = cb["from"]["id"]
    chat_id = cb["message"]["chat"]["id"]
    message_id = cb["message"]["message_id"]
    count_user(user_id)
    member = is_member(user_id)
    if member is None:
        time.sleep(1.2)               # one calm retry — transient blips only
        member = is_member(user_id)
    if member is None:
        with _roster_lock:
            member = (_roster.get(user_id) or {}).get("m")
        if member is None:
            member = False
        roster_touch(user_id)
        log.warning("membership check unavailable for user %s (roster fallback: %s)",
                    user_id, member)
    else:
        roster_touch(user_id, member)  # roster learns the id + fresh verdict
    set_menu_button(chat_id)          # reset any legacy web_app menu button
    if member:
        # verified HERE in the chat — only now does the mini app open.
        # Push the roster out immediately: the user may already be staring
        # at the mini app's gate screen, whose auto-poll will flip them in.
        members_commit(force=True)
        answer_cb(cb["id"], "✅ خوش اومدی! دکمهٔ «ورود به جدولک» تازه شد.")
        edit_message(chat_id, message_id, welcome_text(), kb_app(user_id))
        pin_message(chat_id, message_id)
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
        if user.get("is_bot"):
            # a re-sent/forwarded bot-authored message once arrived with
            # from.id == OUR OWN bot id and failed every membership check
            # (Telegram answers 400 PARTICIPANT_ID_INVALID for bot ids).
            log.info("ignored bot-authored message from %s", user_id)
            return
        roster_touch(user_id)           # any interaction heals the cold-start roster
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
        if cb.get("from", {}).get("is_bot"):
            log.info("ignored bot-authored callback from %s",
                     cb.get("from", {}).get("id"))
            return
        roster_touch(cb.get("from", {}).get("id"))   # seen (verdict comes in handler)
        if cb.get("data") == "joined":
            handle_joined(cb)


# --------------------------------------------------------------------------- #
#  Control file (kill switch served from the repo via raw.githubusercontent)    #
# --------------------------------------------------------------------------- #

def gateway_enabled() -> bool:
    """Kill switch served from control.json in the repo:
      {"gateway": "on"}
    (grace_until is a legacy field the v2.9 client ignores — unknowns are
    gated now; parsing it is harmless.)"""
    global _control_grace
    try:
        # cache-buster: raw.githubusercontent serves ~5 min of CDN cache on a
        # fixed URL, which would delay kill-switch / grace edits
        sep = "&" if "?" in CONTROL_URL else "?"
        r = requests.get(f"{CONTROL_URL}{sep}t={int(time.time())}",
                         headers=UA, timeout=(10, 20))
        if r.status_code == 200:
            try:
                ctrl = json.loads(r.text or "{}")
            except ValueError:
                ctrl = {}
            if isinstance(ctrl, dict):
                try:
                    _control_grace = int(ctrl.get("grace_until") or 0)
                except (ValueError, TypeError):
                    _control_grace = 0
                return "off" not in str(ctrl.get("gateway", "on")).lower()
            return True  # non-dict body -> assume on
        return True  # unreachable control file -> assume on
    except requests.RequestException:
        return True


# --------------------------------------------------------------------------- #
#  Main loop                                                                    #
# --------------------------------------------------------------------------- #

def get_updates(offset: int, timeout: int) -> tuple:
    """Returns (updates, next_offset) or raises/returns special codes.

    offset == -1 (bootstrap) OMITS the offset parameter entirely, so
    Telegram returns ALL unconfirmed updates oldest-first — previously
    passing offset=-1 returned only the LAST pending update and the
    offset jump silently confirmed/dropped every older unanswered message.
    """
    try:
        params = {"timeout": timeout, "allowed_updates":
                  json.dumps(["message", "callback_query"])}
        if offset >= 0:
            params["offset"] = offset
        r = requests.get(
            f"{TG_API}/bot{BOT_TOKEN}/getUpdates",
            params=params,
            headers=UA, timeout=(timeout + 20, timeout + 20),
        )
        if r.status_code == 409:
            return "CONFLICT", offset
        if r.status_code == 200:
            try:
                body = r.json()
            except ValueError:
                log.warning("getUpdates returned non-JSON body")
                return [], offset
            if body.get("ok"):
                ups = body.get("result") or []
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

    set_commands()   # standard commands menu (replaces legacy web_app menu)

    offset = START_OFFSET
    deadline = time.time() + RUNTIME_MINUTES * 60
    conflicts = 0
    stats_load()
    roster_load()
    threading.Thread(target=roster_loop, daemon=True, name="roster").start()
    log.info("polling for %.0f minutes (start offset=%d)", RUNTIME_MINUTES, offset)

    while time.time() < deadline:
        if not gateway_enabled():
            log.info("control.json says OFF — stopping without chaining")
            maybe_commit_stats(force=True)
            roster_save(force=True)
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
                maybe_commit_stats(force=True)
                roster_save(force=True)
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
        maybe_commit_stats()

    # runtime exhausted — flush stats + roster, then hand the offset to the next chained run
    maybe_commit_stats(force=True)
    roster_save(force=True)
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
