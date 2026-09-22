#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Algorithmic 8x8 Persian crossword builder (no LLM needed for placement).

Grid convention (RTL):
  - rows 0..7 top -> bottom; columns 0..7 where column 0 is the RIGHTMOST
    column (Persian reading direction). Horizontal words start on their
    rightmost letter and extend leftward.
  - horizontal word at (r, c, len): cells (r, c), (r, c+1)...(r, c+len-1)
  - vertical word at (r, c, len):   cells (r, c), (r+1, c)...(r+len-1, c)

Design constraints ("clean & spacious" crossword):
  1. at most ONE horizontal word per row and ONE vertical word per column,
     so the clue banner label "[افقی N]" (row N) / "[عمودی N]" (col N)
     matches the axis numbers drawn OUTSIDE the grid 1:1.
  2. no letter conflicts, bounds respected (8x8),
  3. classic adjacency rules (no fake parallel words touching),
  4. every word crosses at least one other word (fully connected grid),
  5. word length 2..8, alphabet restricted to the 32-letter Persian set.

Public API:
    build_puzzle(candidates, seed, target=11) -> dict | None
    validate_puzzle(words, size=8)             -> (ok, errors)
    word_ok(w)                                  -> bool (alphabet/length check)
"""

from __future__ import annotations

import random

SIZE = 8

# The 32-letter Persian alphabet (exactly the keys of the in-app keyboard).
PERSIAN_LETTERS = set("ابپتثجچحخدذرزژسشصضطظعغفقکگلمنوهی")

MIN_LEN, MAX_LEN = 2, SIZE


def normalize_word(w: str) -> str:
    """Normalize a Persian word to the 32-letter alphabet form."""
    w = (w or "").strip()
    w = w.replace("ي", "ی").replace("ك", "ک").replace("ة", "ه").replace("أ", "ا")
    w = w.replace("إ", "ا").replace("ؤ", "و").replace("ئ", "ی").replace("ٱ", "ا")
    w = w.replace("\u200c", "").replace("\u200f", "").replace("\u200e", "")
    return w


def word_ok(w: str) -> bool:
    """True if the word fits the crossword alphabet & length rules."""
    w = normalize_word(w)
    if not (MIN_LEN <= len(w) <= MAX_LEN):
        return False
    if "آ" in w or "ء" in w:  # not on the 32-key keyboard
        return False
    return all(ch in PERSIAN_LETTERS for ch in w)


# --------------------------------------------------------------------------- #
#  Grid engine                                                                  #
# --------------------------------------------------------------------------- #

class Grid:
    """Mutable 8x8 grid that enforces the placement rules."""

    def __init__(self):
        self.cells = [[None] * SIZE for _ in range(SIZE)]  # cells[r][c] = letter
        self.words = []  # {"w", "r", "c", "d", "clue"}

    # -- lookups ------------------------------------------------------------- #

    def letter(self, r: int, c: int):
        return self.cells[r][c] if 0 <= r < SIZE and 0 <= c < SIZE else "#"

    def h_rows(self):
        return {w["r"] for w in self.words if w["d"] == "h"}

    def v_cols(self):
        return {w["c"] for w in self.words if w["d"] == "v"}

    def occupied(self, r: int, c: int) -> bool:
        return 0 <= r < SIZE and 0 <= c < SIZE and self.cells[r][c] is not None

    # -- placement feasibility ------------------------------------------------ #

    def can_place(self, word: str, r: int, c: int, d: str, first: bool) -> bool:
        L = len(word)
        # 1. bounds + length
        if d == "h":
            if c + L - 1 >= SIZE:
                return False
        else:
            if r + L - 1 >= SIZE:
                return False
        # 2. one h-word per row / one v-word per column
        if d == "h" and r in self.h_rows():
            return False
        if d == "v" and c in self.v_cols():
            return False

        crossings = 0
        for i, ch in enumerate(word):
            rr, cc = (r, c + i) if d == "h" else (r + i, c)
            existing = self.cells[rr][cc]
            if existing is not None:
                if existing != ch:          # letter conflict
                    return False
                crossings += 1              # genuine crossing
            else:
                # empty cell: orthogonal neighbours must be empty, otherwise
                # a fake parallel word would appear next to this fresh letter.
                if d == "h":
                    if self.occupied(rr - 1, cc) or self.occupied(rr + 1, cc):
                        return False
                else:
                    if self.occupied(rr, cc - 1) or self.occupied(rr, cc + 1):
                        return False
        # 3. end caps: the cells just before the start / after the end of the
        #    word (in its own direction) must be empty or outside the grid.
        if d == "h":
            if self.occupied(r, c - 1) or self.occupied(r, c + L):
                return False
        else:
            if self.occupied(r - 1, c) or self.occupied(r + L, c):
                return False
        # 4. connectivity: every word after the first must cross something.
        if not first and crossings == 0:
            return False
        return True

    def place(self, word: str, r: int, c: int, d: str, clue: str = "") -> None:
        for i, ch in enumerate(word):
            rr, cc = (r, c + i) if d == "h" else (r + i, c)
            self.cells[rr][cc] = ch
        self.words.append({"w": word, "r": r, "c": c, "d": d, "clue": clue})

    # -- scoring --------------------------------------------------------------- #

    def bbox(self):
        rows = [w["r"] for w in self.words] + [w["r"] + len(w["w"]) - 1 for w in self.words if w["d"] == "v"]
        cols = [w["c"] for w in self.words] + [w["c"] + len(w["w"]) - 1 for w in self.words if w["d"] == "h"]
        if not rows:
            return 0
        return (max(rows) - min(rows) + 1) * (max(cols) - min(cols) + 1)

    def crossings_of(self, word) -> int:
        """How many shared letters this word has with the rest."""
        own = set(self._cells_of(word))
        total = 0
        for other in self.words:
            if other is word:
                continue
            total += len(own & set(self._cells_of(other)))
        return total

    @staticmethod
    def _cells_of(word) -> list:
        out = []
        for i in range(len(word["w"])):
            out.append((word["r"], word["c"] + i) if word["d"] == "h"
                       else (word["r"] + i, word["c"]))
        return out


# --------------------------------------------------------------------------- #
#  Builder                                                                      #
# --------------------------------------------------------------------------- #

def _placement_score(grid: Grid, word: str, r: int, c: int, d: str) -> float:
    """Score a candidate placement: crossings + spread, minus sprawl."""
    L = len(word)
    crossings = sum(
        1 for i in range(L)
        if grid.cells[(r, c + i)[0] if d == "h" else r + i][(c + i) if d == "h" else c] is not None
    )
    # provisional bbox with this word added
    rmin = min([w["r"] for w in grid.words] + [r, r + L - 1 if d == "v" else r])
    rmax = max([w["r"] for w in grid.words] + [r, r + L - 1 if d == "v" else r])
    cmin = min([w["c"] for w in grid.words] + [c, c + L - 1 if d == "h" else c])
    cmax = max([w["c"] for w in grid.words] + [c, c + L - 1 if d == "h" else c])
    sprawl = (rmax - rmin + 1) * (cmax - cmin + 1)
    # balance horizontal vs vertical counts a little
    n_h = 1 + sum(1 for w in grid.words if w["d"] == "h")
    n_v = sum(1 for w in grid.words if w["d"] == "v")
    if d == "v":
        n_v += 1
    balance_penalty = abs(n_h - n_v)
    return crossings * 4.0 - sprawl * 0.015 - balance_penalty * 1.2


def _best_placements(grid: Grid, word: str):
    """All valid (score, r, c, d) placements for `word` on the current grid."""
    out = []
    L = len(word)
    first = not grid.words
    for d in ("h", "v"):
        rng_rows = range(SIZE) if d == "h" else range(SIZE - L + 1)
        rng_cols = range(SIZE - L + 1) if d == "h" else range(SIZE)
        for r in rng_rows:
            for c in rng_cols:
                if grid.can_place(word, r, c, d, first):
                    out.append((_placement_score(grid, word, r, c, d), r, c, d))
    out.sort(key=lambda t: -t[0])
    return out


def build_once(candidates, rng: random.Random, target: int) -> list | None:
    """One randomized construction attempt. Returns the word list or None."""
    grid = Grid()
    pool = [(normalize_word(w), clue) for w, clue in candidates]
    pool = [(w, clue) for w, clue in pool if word_ok(w)]
    # dedupe by word, keep the first clue
    seen = set()
    unique = []
    for w, clue in pool:
        if w not in seen:
            seen.add(w)
            unique.append((w, clue))
    if len(unique) < 8:
        return None

    # seed word: a long one, horizontal, near the vertical middle
    long_words = [wc for wc in unique if len(wc[0]) >= 5]
    if not long_words:
        return None
    seed_word, seed_clue = rng.choice(long_words)
    r0 = rng.randint(2, 5)
    c0 = rng.randint(0, SIZE - len(seed_word))
    grid.place(seed_word, r0, c0, "h", seed_clue)

    rest = [wc for wc in unique if wc[0] != seed_word]
    # try longer words first (easier to cross), with some shuffle
    rest.sort(key=lambda wc: -len(wc[0]))
    head = rest[: max(6, len(rest) // 2)]
    tail = rest[len(head):]
    rng.shuffle(head)
    rng.shuffle(tail)
    rest = head + tail

    for word, clue in rest:
        if len(grid.words) >= target:
            break
        placements = _best_placements(grid, word)
        if not placements:
            continue
        # weighted pick among the top 3 placements for variety
        top = placements[:3]
        weights = [max(p[0], 0.1) + 1.0 for p in top]
        (score, r, c, d) = rng.choices(top, weights=weights, k=1)[0]
        grid.place(word, r, c, d, clue)

    words = grid.words
    n_h = sum(1 for w in words if w["d"] == "h")
    n_v = sum(1 for w in words if w["d"] == "v")
    if len(words) < 8 or n_h < 3 or n_v < 3:
        return None
    return words


def build_puzzle(candidates, seed: int, target: int = 11, attempts: int = 10):
    """
    Build one puzzle from (word, clue) candidates.
    Tries several randomized layouts and keeps the best one:
    most words, most crossings, compact bounding box.
    Returns {"words": [...]} or None.
    """
    best = None
    best_key = None
    for i in range(attempts):
        rng = random.Random(seed * 1000 + i)
        words = build_once(candidates, rng, target)
        if not words:
            continue
        ok, _ = validate_puzzle(words)
        if not ok:
            continue
        n_h = sum(1 for w in words if w["d"] == "h")
        n_v = len(words) - n_h
        g = Grid()
        for w in words:
            g.place(w["w"], w["r"], w["c"], w["d"])
        key = (len(words), min(n_h, n_v), -g.bbox())
        if best_key is None or key > best_key:
            best, best_key = words, key
    if best is None:
        return None
    return {"words": [{"w": w["w"], "r": w["r"], "c": w["c"],
                       "d": w["d"], "clue": w["clue"]} for w in best]}


# --------------------------------------------------------------------------- #
#  Independent validator (defense in depth — used on LLM output too)            #
# --------------------------------------------------------------------------- #

def validate_puzzle(words, size: int = SIZE):
    """Re-check every rule from scratch. Returns (ok, [errors])."""
    errors = []
    cells = {}
    seen_words = set()
    h_rows, v_cols = {}, {}

    for w in words:
        word = normalize_word(w.get("w", ""))
        d, r, c = w.get("d"), w.get("r"), w.get("c")
        if not word_ok(word):
            errors.append(f"bad word: {w.get('w')!r}")
            continue
        if word in seen_words:
            errors.append(f"duplicate word: {word}")
        seen_words.add(word)
        if d not in ("h", "v"):
            errors.append(f"bad direction: {d}")
            continue
        if not isinstance(r, int) or not isinstance(c, int) or not (0 <= r < size and 0 <= c < size):
            errors.append(f"bad position for {word}: ({r},{c})")
            continue
        L = len(word)
        if d == "h":
            if c + L - 1 >= size:
                errors.append(f"out of bounds (h): {word}")
                continue
            if r in h_rows:
                errors.append(f"two h-words in row {r}")
            h_rows[r] = word
        else:
            if r + L - 1 >= size:
                errors.append(f"out of bounds (v): {word}")
                continue
            if c in v_cols:
                errors.append(f"two v-words in column {c}")
            v_cols[c] = word

    # letter conflicts
    for w in words:
        word = normalize_word(w.get("w", ""))
        d, r, c = w.get("d"), w.get("r"), w.get("c")
        if d not in ("h", "v") or not isinstance(r, int) or not isinstance(c, int):
            continue
        if not (0 <= r < size and 0 <= c < size):
            continue
        L = len(word)
        if (d == "h" and c + L - 1 >= size) or (d == "v" and r + L - 1 >= size):
            continue
        for i, ch in enumerate(word):
            rr, cc = (r, c + i) if d == "h" else (r + i, c)
            if (rr, cc) in cells and cells[(rr, cc)] != ch:
                errors.append(f"conflict at ({rr},{cc}): {cells[(rr,cc)]} vs {ch}")
            cells[(rr, cc)] = ch

    # adjacency + end caps
    for w in words:
        word = normalize_word(w.get("w", ""))
        d, r, c = w.get("d"), w.get("r"), w.get("c")
        if d not in ("h", "v") or not isinstance(r, int) or not isinstance(c, int):
            continue
        L = len(word)
        if (d == "h" and c + L - 1 >= size) or (d == "v" and r + L - 1 >= size):
            continue
        if d == "h" and ((c > 0 and (r, c - 1) in cells) or (c + L < size and (r, c + L) in cells)):
            errors.append(f"h-word caps touch letters: {word}")
        if d == "v" and ((r > 0 and (r - 1, c) in cells) or (r + L < size and (r + L, c) in cells)):
            errors.append(f"v-word caps touch letters: {word}")
        for i in range(L):
            rr, cc = (r, c + i) if d == "h" else (r + i, c)
            if d == "h":
                if (rr > 0 and (rr - 1, cc) in cells and (rr, cc) not in cells) or \
                   (rr + 1 < size and (rr + 1, cc) in cells and (rr, cc) not in cells):
                    errors.append(f"fake parallel word next to {word}")
            else:
                if (cc > 0 and (rr, cc - 1) in cells and (rr, cc) not in cells) or \
                   (cc + 1 < size and (rr, cc + 1) in cells and (rr, cc) not in cells):
                    errors.append(f"fake parallel word next to {word}")

    # connectivity (graph of words connected via shared cells)
    idx = {}
    for i, w in enumerate(words):
        word = normalize_word(w.get("w", ""))
        d, r, c = w.get("d"), w.get("r"), w.get("c")
        if d not in ("h", "v"):
            continue
        idx[i] = set()
        for j in range(len(word)):
            rr, cc = (r, c + j) if d == "h" else (r + j, c)
            idx[i].add((rr, cc))
    if idx:
        start = next(iter(idx))
        seen = {start}
        stack = [start]
        while stack:
            cur = stack.pop()
            for other, cellset in idx.items():
                if other not in seen and idx[cur] & cellset:
                    seen.add(other)
                    stack.append(other)
        if len(seen) != len(idx):
            errors.append(f"grid not fully connected ({len(seen)}/{len(idx)})")

    n_h, n_v = len(h_rows), len(v_cols)
    if len(words) < 8:
        errors.append(f"too few words: {len(words)}")
    if n_h < 3 or n_v < 3:
        errors.append(f"unbalanced directions: h={n_h} v={n_v}")
    return (not errors), errors


# --------------------------------------------------------------------------- #
#  Self-test                                                                    #
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    demo = [
        ("ستاره", "جرم درخشان در آسمان شب"), ("سیاره", "جرم آسمانی گردان به دور ستاره"),
        ("کهکشان", "سامانه ای عظیم از ستارگان"), ("ماه", "همراه زمین در شب"),
        ("خورشید", "ستاره مرکزی منظومه"), ("آسمان", "گنبد آبی بالای سر"),
        ("فضانورد", "مسافر فضا"), ("تلسکوپ", "وسیله رصد آسمان"),
        ("مدار", "مسیر گردش اجسام فضایی"), ("شهاب", "سنگ آسمانی درخشان"),
        ("کهربا", "صمغ fossil"), ("زمین", "سیاره سوم"), ("ابر", "پنبه آسمانی"),
        ("نور", "پدیده بصری"), ("نسیم", "باد ملایم"), ("باران", "قطره های آسمان"),
        ("گل", "زیبایی باغ"), ("درخت", "گیاه بلند قامت"), ("کوه", "برآمدگی بزرگ زمین"),
        ("دریا", "پهنه آبی"), ("موج", "حرکت آب"), ("صدف", "خانه صیاد دریا"),
        ("ماهی", "ساکن آب"), ("قایق", "وسیله رفت و آمد دریایی"), ("بادبان", "بالای قایق"),
        ("ستاره باران", "x"), ("نجوم", "دانش ستارگان"), ("فضا", "پهنه بی کرانه"),
        ("جرم", "مقدار ماده"), "بدون-شرح", ("ایستگاه", "مقر فضایی"),
    ]
    pairs = [item for item in demo if isinstance(item, (tuple, list)) and len(item) == 2]
    demo = [(normalize_word(w), c) for w, c in pairs
            if isinstance(w, str) and isinstance(c, str)]
    demo = [(w, c) for w, c in demo if word_ok(w)]
    ok_count = 0
    for seed in range(30):
        p = build_puzzle(demo, seed=seed, target=11)
        if not p:
            print(f"seed {seed}: no puzzle")
            continue
        valid, errs = validate_puzzle(p["words"])
        if valid:
            ok_count += 1
            if seed < 3:
                grid_txt = [["·"] * SIZE for _ in range(SIZE)]
                for w in p["words"]:
                    for i, ch in enumerate(w["w"]):
                        rr, cc = (w["r"], w["c"] + i) if w["d"] == "h" else (w["r"] + i, w["c"])
                        grid_txt[rr][cc] = ch
                print(f"seed {seed}: {len(p['words'])} words "
                      f"(h={sum(1 for w in p['words'] if w['d']=='h')}, "
                      f"v={sum(1 for w in p['words'] if w['d']=='v')})")
                for row in grid_txt:
                    print("  " + " ".join(row))
        else:
            print(f"seed {seed}: INVALID -> {errs}")
    print(f"\n{ok_count}/30 seeds produced a valid puzzle")
