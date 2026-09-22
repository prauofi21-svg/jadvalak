#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Daily Persian crossword generator for the "جدولک" Telegram mini app.

Pipeline (per puzzle, 10 per day):
  1. Groq (qwen) proposes ~26 candidate words + clues for the puzzle's topic,
     avoiding recently used words.
  2. Local filtering: 32-letter alphabet, length 2..8, clue sanity (must not
     contain the answer word), no repeats within the day, not used before.
  3. crossword_builder.build_puzzle() places the words on an 8x8 RTL grid
     ALGORITHMICALLY (zero grid errors by construction) and an independent
     validator re-checks every rule.
  4. Optional Groq editor pass polishes the clues of the placed words.
  5. The finished day file data/<date>.json is written with lightly
     obfuscated answers (XOR + base64) so casual JSON peeking doesn't spoil
     the puzzles.

Reserve pool: extra validated puzzles are stored in data/reserve.json and
used to top up days when generation partially fails.

CLI:
    python generate_puzzles.py --date 2026-09-22      generate for a date
    python generate_puzzles.py --date auto            Tehran "tomorrow"
    python generate_puzzles.py --selftest            offline (builtin bank)

Env: GROK_API_KEY (required unless --selftest).
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from crossword_builder import build_puzzle, normalize_word, validate_puzzle, word_ok

log = logging.getLogger("jadvalak-gen")

TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30))
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
PUZZLES_PER_DAY = 10
RESERVE_TARGET = 10
CANDIDATES_PER_PUZZLE = 26
USED_HISTORY = 1200

TOPICS = [
    "آسمان شب، ستارگان و صورت‌های فلکی",
    "طبیعت، صبح و آب‌وهوا",
    "روزنامه، رسانه، کتاب و نوشتن",
    "باغ، گیاهان و درخت‌ها",
    "خانه، ابزار و ساختمان",
    "دریا، اقیانوس و موجودات دریایی",
    "کویر، سفر و جاده",
    "فناوری، رایانه و اینترنت",
    "کهکشان، فضانوردی و سیارات",
    "جشن، شادی، غذا و میوه",
]

GEN_SYSTEM = (
    "You are an expert Persian (Farsi) crossword compiler for a popular "
    "Iranian puzzle app. You answer with valid JSON only — no commentary, "
    "no markdown fences."
)

GEN_PROMPT = """موضوع واژه‌ها: {topic}

برای جدول کلمات متقاطع فارسی، {n} واژهٔ رایج و زیبای فارسی با شرح کوتاه پیشنهاد بده.

قواعد مهم (اگر نقض شود واژه حذف می‌شود):
- هر واژه یک «تک‌واژهٔ» فارسی رایج بین ۳ تا ۸ حرف باشد (ترکیب/دوواژه‌ای ممنوع).
- فقط از این ۳۲ حرف استفاده کن: ا ب پ ت ث ج چ ح خ د ذ ر ز ژ س ش ص ض ط ظ ع غ ف ق ک گ ل م ن و ه ی
- این حروف ممنوع‌اند: آ ء ئ ؤ — واژه‌هایی مثل «آسمان» یا «مسئله» را پیشنهاد نکن.
- شرح باید کوتاه (۲ تا ۱۰ واژه)، شیرین و دقیق باشد و خود واژه یا بخش قابل تشخیص آن را لو ندهد.
- شرح فارسی روان باشد، نه ترجمهٔ تحت‌اللفظی.
- واژه‌های تکراری و نام‌های خاص (شخص/برند/شهر) پیشنهاد نکن.

فقط از این واژه‌ها دوری کن (قبلاً استفاده شده‌اند):
{avoid}

پاسخ فقط به شکل JSON:
[{{"w":"واژه","clue":"شرح کوتاه"}} , ...]"""

EDITOR_SYSTEM = (
    "You are a meticulous Persian crossword editor. You answer with valid "
    "JSON only — no commentary, no markdown fences."
)

EDITOR_PROMPT = """این واژه‌ها و شرح‌های جدول کلمات متقاطع فارسی است:
{items}

هر شرح را اگر لازم باشد بهتر کن: شیرین‌تر، دقیق‌تر و روان‌تر؛ اما:
- خود واژه یا بخش قابل تشخیص آن در شرح نیاید.
- طول شرح بین ۲ تا ۱۲ واژه بماند.
- اگر شرح خوب است همان را بدون تغییر برگردان.

پاسخ فقط به شکل JSON:
[{{"w":"همان واژه","clue":"شرح نهایی"}} , ...]"""

# --------------------------------------------------------------------------- #
#  Groq client (compact port of the proven llm_translator pattern)              #
# --------------------------------------------------------------------------- #

API_KEY = (os.environ.get("GROK_API_KEY") or "").strip()
BASE = "https://api.groq.com/openai/v1"
PREFERRED_MODELS = ["qwen/qwen3.8-27b", "openai/gpt-oss-120b", "llama-3.3-70b-versatile"]
_MODEL = None


def _headers():
    return {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def detect_model() -> str | None:
    global _MODEL
    if _MODEL:
        return _MODEL
    if not API_KEY:
        return None
    try:
        r = requests.get(f"{BASE}/models", headers=_headers(), timeout=(15, 60))
        if r.status_code != 200:
            log.warning("Groq /models HTTP %d — trying preferred list anyway", r.status_code)
        else:
            ids = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
            for m in PREFERRED_MODELS:
                if m in ids:
                    _MODEL = m
                    log.info("LLM model: %s", m)
                    return m
    except requests.RequestException as exc:
        log.warning("Groq /models failed: %s", exc)
    _MODEL = PREFERRED_MODELS[0]
    return _MODEL


def ask_llm(system: str, user: str, max_tokens: int = 2048, temperature: float = 0.7):
    """One chat completion with long 429 backoff (qwen: ~1000 tok/min)."""
    model = detect_model()
    if not model:
        return None
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    last_err = None
    for attempt in range(1, 5):
        try:
            r = requests.post(f"{BASE}/chat/completions", headers=_headers(),
                              json=payload, timeout=(15, 180))
            if r.status_code == 200:
                content = (r.json().get("choices") or [{}])[0].get("message", {}).get("content")
                if content and content.strip():
                    return content.strip()
                last_err = "empty completion"
            elif r.status_code == 429:
                wait = 30
                try:
                    wait = max(int(r.headers.get("retry-after") or 0), 20)
                except (TypeError, ValueError):
                    pass
                last_err = "HTTP 429"
                log.warning("rate-limited — sleeping %ds (attempt %d/4)", wait, attempt)
                time.sleep(wait)
                continue
            else:
                last_err = f"HTTP {r.status_code}: {r.text[:180]}"
                if r.status_code in (400, 401, 403):
                    break
        except requests.RequestException as exc:
            last_err = str(exc)
        if attempt < 4:
            time.sleep(3 * attempt)
    log.warning("LLM call failed: %s", last_err)
    return None


def parse_json_arr(text: str) -> list:
    """Extract the first JSON array from an LLM reply (tolerates fences)."""
    if not text:
        return []
    cleaned = re.sub(r"```(?:json)?", "", text).strip().strip("`").strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end <= start:
        # try object with array inside
        s2, e2 = cleaned.find("{"), cleaned.rfind("}")
        if s2 != -1 and e2 > s2:
            try:
                obj = json.loads(cleaned[s2:e2 + 1])
                for key in ("words", "items", "data", "candidates"):
                    if isinstance(obj.get(key), list):
                        return obj[key]
            except ValueError:
                pass
        return []
    candidate = cleaned[start:end + 1]
    try:
        data = json.loads(candidate)
        return data if isinstance(data, list) else []
    except ValueError:
        fixed = re.sub(r",\s*([\]}])", r"\1", candidate)
        try:
            data = json.loads(fixed)
            return data if isinstance(data, list) else []
        except ValueError:
            return []


# --------------------------------------------------------------------------- #
#  Clue sanity                                                                  #
# --------------------------------------------------------------------------- #

PERSIAN_RE = re.compile(r"[\u0600-\u06FF]")


def clue_ok(clue: str, word: str) -> bool:
    clue = (clue or "").strip()
    if not (2 <= len(clue) <= 90):
        return False
    if not PERSIAN_RE.search(clue):
        return False
    if normalize_word(word) in normalize_word(clue):
        return False
    # a 4+ letter chunk of the word must not appear in the clue
    w = normalize_word(word)
    for i in range(len(w) - 3):
        if w[i:i + 4] in clue:
            return False
    return True


# --------------------------------------------------------------------------- #
#  Obfuscation (stops casual JSON peeking at the answers)                       #
# --------------------------------------------------------------------------- #

def xor_b64(text: str, key: str) -> str:
    raw = text.encode("utf-8")
    kb = key.encode("utf-8")
    out = bytes(b ^ kb[i % len(kb)] for i, b in enumerate(raw))
    return base64.urlsafe_b64encode(out).decode("ascii").rstrip("=")


# --------------------------------------------------------------------------- #
#  Builtin fallback word bank (--selftest and emergency reserve)                #
# --------------------------------------------------------------------------- #

BUILTIN = {
    0: [("ستاره", "جرم درخشان در آسمان شب"), ("سیاره", "گردشگر مدار خورشید"),
        ("کهکشان", "شهر عظیم ستارگان"), ("خورشید", "چراغ مرکزی منظومه"),
        ("مدار", "مسیر گردش اجسام فضایی"), ("شهاب", "سنگ آسمانی درخشان و گذرا"),
        ("تلسکوپ", "چشم بزرگ رصد آسمان"), ("فضا", "پهنه بی‌کرانه"),
        ("نجوم", "دانش ستارگان"), ("ماه", "همراه شبانه زمین"),
        ("ابر", "پنبه‌های آسمان"), ("نور", "پدیدهٔ دیدنی"),
        ("صورت", "نقش فلکی آسمان"), ("جرم", "مقدار مادهٔ جسم")],
    1: [("نسیم", "باد ملایم و خنک"), ("باران", "قطره‌های هدیهٔ ابر"),
        ("دشت", "زمین پهن و هموار"), ("رود", "رگ آبی طبیعت"),
        ("سبزه", "روییدنی بهاری"), ("کوه", "غول خفتهٔ زمین"),
        ("چمن", "فرش سبز طبیعت"), ("غبار", "ذره‌های ریز معلق"),
        ("طوفان", "خشم آسمان"), ("شبنم", "اشک صبحگاهی برگ"),
        ("گل", "زیباری باغ"), ("بوستان", "باغ بزرگ عمومی")],
    2: [("روزنامه", "آینهٔ رویدادهای روز"), ("قلم", "ابزار نویسنده"),
        ("کتاب", "همراه خاموش دانش"), ("خبر", "گزارش تازهٔ رویداد"),
        ("نویسنده", "آفرینندهٔ متن"), ("مقاله", "نوشتهٔ تحلیلی"),
        ("مجله", "نشریهٔ تصویری دوره‌ای"), ("چاپ", "تکثیر مکتوب"),
        ("سطر", "خطی از متن"), ("ویرایش", "پیراستن متن"),
        ("خاطره", "یادمان روزگار"), ("ترجمه", "برد متن به زبانی دیگر")],
    3: [("درخت", "ستون سبز جنگل"), ("ریشه", "لنگر گیاه در خاک"),
        ("برگ", "ششی سبز روی شاخه"), ("میوه", "پاداش درخت"),
        ("بذر", "نگین آغاز گیاه"), ("گلدان", "خانهٔ کوچک گل"),
        ("شاخه", "دست درخت"), ("بوته", "گیاه کوتاه پرشاخ"),
        ("بارور", "حاصلخیز"), ("باغبان", "پرورش‌دهندهٔ گل و گیاه"),
        ("نهال", "درخت نوباوه"), ("خار", "محافظ تیز گیاه")],
    4: [("ابزار", "كمك‌كار دست‌اندرکاران"), ("دیوار", "مرز ایستای خانه"),
        ("پنجره", "چشم روشن اتاق"), ("شیروانی", "سقف شیب‌دار بام"),
        ("نجار", "استاد چوب"), ("آجر", "لبهٔ ساخت‌وساز"),
        ("ستون", "تکیه‌گاه بنا"), ("سردر", "درگاه نمایش"),
        ("نردبان", "راه صعود دستی"), ("کلید", "گشایندهٔ قفل"),
        ("محراب", "طاق نمازخانه"), ("خشت", "خاک پختهٔ بنایی")],
    5: [("دریا", "پهنهٔ آبی زمین"), ("موج", "رقص آب"),
        ("ماهی", "ساکن آب‌های آزاد"), ("صدف", "گنج صیاد دریا"),
        ("قایق", "وسیلهٔ رفت‌وآمد آبی"), ("بادبان", "بال قایق"),
        ("لنگر", "توقف‌دهندهٔ کشتی"), ("غواص", "مسافر اعماق"),
        ("ساحل", "مرز خشکی و آب"), ("ملوان", "دورادور جهان"),
        ("صید", "شکار آبی"), ("تیفون", "توفان دریایی")],
    6: [("کویر", "دریای بی‌آب"), ("شتر", "کشتی صحرا"),
        ("کاروان", "رهگذران صحرا"), ("قافله", "کاروان شتران"),
        ("شن", "ذره‌های ریز صحرا"), ("واحه", "سبزه‌جزیره کویر"),
        ("جاده", "راه هموار رفت‌وآمد"), ("منزل", "آرامگاه مسافر"),
        ("چادر", "خانهٔ سیار عشایر"), ("سراب", "آب دروغین کویر"),
        ("ریگ", "کرانهٔ کویری"), ("هزارت", "گمراه‌کننده")],
    7: [("رایانه", "مغز الکترونیکی"), ("نرم‌افزار", "روح سخت‌افزار")],
    8: [("فضانورد", "مسافر افلاک"), ("ماه‌نورد", "قدم‌زنندهٔ ماه"),
        ("پیشران", "موتور پرتاب فضاپیما"), ("ایستگاه", "مقر مداری"),
        ("مدارگرد", "گردشگر مدار سیاره"), ("فرود", "نشستن بر سطح"),
        ("سنجاقک", "ملخ هواپیمای کوچک")],
    9: [("جشن", "خانهٔ شادی"), ("شادی", "حال خوش دل"),
        ("کیک", "پادیسال تولد"), ("شمع", "چراغ جشن"),
        ("سور", "دل‌گشایی شاد"), ("رقص", "زبان بدن شادمان"),
        ("هزینه", "خرج کرد"), ("مهمان", "پذیرفتهٔ میزبان"),
        ("سفره", "پهن‌کردنی خوراک"), ("عیدی", "هدیهٔ نوروزی")],
}

# words for topics 7/8 need more entries — extended bank
BUILTIN[7] = [
    ("رایانه", "مغز الکترونیکی خانه و اداره"), ("حافظه", "انبار دادهٔ رایانه"),
    ("صفحه", "نمایشگر تصویر"), ("موش", "جانور کوچک کنار دست"),
    ("حافظه", "انبار داده"), ("برنامه", "دستور پشت‌صحنهٔ رایانه"),
    ("شبکه", "پیوند رایانه‌ها"), ("پیشرفته", "تراز بالاتر فناوری"),
    ("داده", "مادهٔ خام اطلاعات"), ("پیام", "سفرکنندهٔ اینترنت"),
    ("سخت", "ضد نرم"), ("ماهواره", "گوش دورافکن زمین"),
    ("چیپ", "مغز کوچک سیلیکونی"), ("کد", "زبان رمزی رایانه"),
    ("اتصال", "پیوند برخط"), ("خودکار", "نیازمند بی‌فرمان"),
]
BUILTIN[8] = [
    ("فضانورد", "مسافر افلاک"), ("مدارگرد", "چرخنده به دور سیاره"),
    ("پیشران", "موتور پرتاب فضاپیما"), ("ایستگاه", "مقر مداری宇航"),
    ("فرود", "نشستن بر سطح"), ("سنجاقک", "هواپیمای کوچک طبیعت"),
    ("کهکشان", "شهر عظیم ستارگان"), ("سیاره", "گردشگر مدار"),
    ("شهاب", "راز آسمان شب"), ("تلسکوپ", "چشم بزرگ رصدگر"),
    ("صخره", "سنگ بزرگ صعب‌گذر"), ("فضاپیما", "وسیلهٔ سفر کیهانی"),
    ("مدار", "مسیر گردش"), ("خلبان", "رانندهٔ آسمان"),
    ("جاذبه", "چشمگیر به سمت جرم"), ("انفجار", "شکوفایی ناگهانی"),
]
# clean any non 32-letter words from the builtin bank
for k in list(BUILTIN):
    BUILTIN[k] = [(normalize_word(w), c) for w, c in BUILTIN[k]]
    BUILTIN[k] = [(w, c) for w, c in BUILTIN[k] if word_ok(w) and clue_ok(c, w)]


# --------------------------------------------------------------------------- #
#  Day file build                                                               #
# --------------------------------------------------------------------------- #

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def tehran_today() -> str:
    return datetime.now(TEHRAN_TZ).strftime("%Y-%m-%d")


def generate_candidates(topic: str, avoid: list, n: int, rng) -> list:
    """Ask Groq for candidate words+clues; fall back to the builtin bank."""
    avoid_str = "، ".join(avoid[:120]) if avoid else "(هیچ)"
    prompt = GEN_PROMPT.format(topic=topic, n=n, avoid=avoid_str)
    for attempt in range(2):
        reply = ask_llm(GEN_SYSTEM, prompt,
                        max_tokens=2200, temperature=0.8 if attempt else 0.7)
        items = parse_json_arr(reply or "")
        cands = []
        for it in items:
            if not isinstance(it, dict):
                continue
            w = normalize_word(str(it.get("w") or it.get("word") or ""))
            clue = str(it.get("clue") or it.get("definition") or "").strip()
            if word_ok(w) and clue_ok(clue, w):
                cands.append((w, clue))
        if len(cands) >= 12:
            log.info("  groq candidates: %d usable", len(cands))
            return cands
        log.warning("  groq gave %d usable candidates (attempt %d) — retrying",
                    len(cands), attempt + 1)
    log.warning("  falling back to builtin bank for this topic")
    return []


def editor_pass(puzzle_words: list) -> list:
    """Polish the clues of the placed words (best-effort)."""
    items = "\n".join(f'- {"واژه: " + w["w"] + " — " + "شرح: " + w["clue"]}' for w in puzzle_words)
    reply = ask_llm(EDITOR_SYSTEM, EDITOR_PROMPT.format(items=items),
                    max_tokens=1600, temperature=0.3)
    data = parse_json_arr(reply or "")
    by_word = {}
    for it in data:
        if isinstance(it, dict):
            w = normalize_word(str(it.get("w") or ""))
            clue = str(it.get("clue") or "").strip()
            if w and clue_ok(clue, w):
                by_word[w] = clue
    improved = 0
    for w in puzzle_words:
        if w["w"] in by_word and by_word[w["w"]] != w["clue"]:
            w["clue"] = by_word[w["w"]]
            improved += 1
    if improved:
        log.info("  editor improved %d clues", improved)
    return puzzle_words


def build_one_puzzle(idx: int, topic: str, avoid: set, used_today: set,
                     rng, use_llm: bool) -> dict | None:
    log.info("پازل %d — موضوع: %s", idx + 1, topic)
    cands = []
    if use_llm:
        cands = generate_candidates(topic, sorted(avoid), CANDIDATES_PER_PUZZLE, rng)
    if not cands:
        bank = list(BUILTIN.get(idx % len(BUILTIN), [])) + \
               [wc for k in BUILTIN for wc in BUILTIN[k]]
        rng.shuffle(bank)
        cands = [wc for wc in bank
                 if wc[0] not in avoid and wc[0] not in used_today][:26]
    # filter: not used before, not used today
    cands = [wc for wc in cands if wc[0] not in avoid and wc[0] not in used_today]
    if len(cands) < 10:
        # second sweep over the whole builtin bank
        bank = [wc for k in BUILTIN for wc in BUILTIN[k]
                if wc[0] not in avoid and wc[0] not in used_today]
        rng.shuffle(bank)
        cands = list(dict.fromkeys(cands + bank))[:30]

    puzzle = None
    for seed in range(14):
        puzzle = build_puzzle(cands, seed=idx * 100 + seed, target=11)
        if puzzle:
            break
    if not puzzle:
        log.warning("  could not build puzzle %d", idx + 1)
        return None
    words = puzzle["words"]
    ok, errs = validate_puzzle(words)
    if not ok:
        log.warning("  puzzle %d invalid: %s", idx + 1, errs)
        return None
    if use_llm:
        words = editor_pass(words)
    for w in words:
        used_today.add(w["w"])
    log.info("  built: %d words (h=%d, v=%d)", len(words),
             sum(1 for w in words if w["d"] == "h"),
             sum(1 for w in words if w["d"] == "v"))
    return {"n": idx + 1, "words": words}


def make_day_payload(date: str, puzzles: list) -> dict:
    key = date
    out = []
    for p in puzzles:
        out.append({
            "n": p["n"],
            "wc": len(p["words"]),
            "words": [
                {"e": xor_b64(w["w"], key), "r": w["r"], "c": w["c"],
                 "d": w["d"], "clue": w["clue"]}
                for w in p["words"]
            ],
        })
    return {"date": date, "v": 1, "puzzles": out}


def generate_day(date: str, use_llm: bool = True) -> tuple:
    """Returns (day_payload, all_words_used_today) or (None, [])."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    used = load_json(DATA_DIR / "used_words.json", {"words": []})
    used_words = set(used.get("words", [])[-USED_HISTORY:])
    reserve = load_json(DATA_DIR / "reserve.json", {"puzzles": []})

    rng = random.Random(date)
    puzzles, used_today = [], set()
    for i, topic in enumerate(TOPICS):
        p = build_one_puzzle(i, topic, used_words, used_today, rng, use_llm)
        if p:
            puzzles.append(p)

    # top up from the reserve pool if the LLM under-delivered
    if len(puzzles) < PUZZLES_PER_DAY and reserve.get("puzzles"):
        for p in reserve["puzzles"]:
            if len(puzzles) >= PUZZLES_PER_DAY:
                break
            if p.get("date") == date:
                continue
            words = p.get("words") or []
            if words and all(w.get("clue") for w in words) and \
                    not (used_today & {w["w"] for w in words}):
                puzzles.append({"n": len(puzzles) + 1, "words": words})
                used_today.update(w["w"] for w in words)
                log.info("topped up from reserve")

    if len(puzzles) < PUZZLES_PER_DAY:
        log.error("only %d/%d puzzles could be built", len(puzzles), PUZZLES_PER_DAY)
        if len(puzzles) < 6:
            return None, []

    # renumber and build payload
    puzzles = sorted(puzzles, key=lambda p: p.get("n", 0))[:PUZZLES_PER_DAY]
    for i, p in enumerate(puzzles):
        p["n"] = i + 1
    payload = make_day_payload(date, puzzles)
    return payload, sorted(used_today)


def top_up_reserve(date: str, use_llm: bool = True) -> list:
    """Try to grow the emergency reserve pool to RESERVE_TARGET puzzles."""
    reserve = load_json(DATA_DIR / "reserve.json", {"puzzles": []})
    have = [p for p in reserve.get("puzzles", []) if p.get("words")]
    if len(have) >= RESERVE_TARGET:
        return have[:RESERVE_TARGET]
    used = load_json(DATA_DIR / "used_words.json", {"words": []})
    avoid = set(used.get("words", [])[-USED_HISTORY:])
    rng = random.Random(f"reserve-{date}")
    for i in range(RESERVE_TARGET - len(have)):
        topic = TOPICS[(len(have) + i) % len(TOPICS)]
        p = build_one_puzzle(i, topic, avoid, set(), rng, use_llm)
        if p:
            have.append({"words": p["words"], "date": date})
            avoid.update(w["w"] for w in p["words"])
    return have[:RESERVE_TARGET + 5]


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate the daily crossword file")
    ap.add_argument("--date", default="auto",
                    help="YYYY-MM-DD (Tehran) or 'auto' = tomorrow")
    ap.add_argument("--selftest", action="store_true",
                    help="offline run using the builtin word bank")
    ap.add_argument("--no-reserve", action="store_true",
                    help="skip the reserve top-up step")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger().handlers[0].setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S"))

    if args.date == "auto":
        date = (datetime.now(TEHRAN_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        date = args.date

    use_llm = not args.selftest
    if use_llm and not API_KEY:
        log.error("GROK_API_KEY is not set")
        return 2
    if use_llm and not detect_model():
        log.error("no LLM available")
        return 2

    log.info("generating %d puzzles for %s (llm=%s)", PUZZLES_PER_DAY, date, use_llm)
    payload, used_today = generate_day(date, use_llm)
    if not payload:
        return 1

    day_file = DATA_DIR / f"{date}.json"
    day_file.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    log.info("wrote %s (%d puzzles, %d bytes)", day_file, len(payload["puzzles"]),
             day_file.stat().st_size)

    # update used-words history
    used = load_json(DATA_DIR / "used_words.json", {"words": []})
    words = [w for w in (used.get("words", []) + used_today) if isinstance(w, str)]
    words = list(dict.fromkeys(words))[-USED_HISTORY:]
    (DATA_DIR / "used_words.json").write_text(
        json.dumps({"words": words, "updated": date}, ensure_ascii=False, indent=0),
        encoding="utf-8")

    if not args.no_reserve:
        try:
            pool = top_up_reserve(date, use_llm)
            (DATA_DIR / "reserve.json").write_text(
                json.dumps({"puzzles": pool}, ensure_ascii=False),
                encoding="utf-8")
            log.info("reserve pool: %d puzzles", len(pool))
        except Exception as exc:  # reserve failure must not fail the day
            log.warning("reserve top-up failed: %s", exc)

    # final sanity: the day file must contain exactly the payload we validated
    check = json.loads(day_file.read_text(encoding="utf-8"))
    assert len(check["puzzles"]) == len(payload["puzzles"]) >= 6
    log.info("DONE: %s ready with %d puzzles", date, len(check["puzzles"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
