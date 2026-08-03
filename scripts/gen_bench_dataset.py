"""Generate a SYNTHETIC labeled vision-benchmark dataset.

Renders content pages (todo lists, notes, tables, code, forms, ...) as JPEGs with
known ground truth matching app.services.vlm._SCHEMA, so bench_vision.py can score
any VLM's content_type / items / ocr_text against a gold label.

CAVEAT: this is printed text on clean backgrounds — an UPPER BOUND on real
performance. It measures classification + clean-OCR ability, not handwriting,
glare, or webcam noise. For production routing decisions, replace with ~180
hand-labeled real frames from data/frames/ (see BENCHMARKS spec). Synthetic
numbers are for relative model comparison and harness validation only.

    python scripts/gen_bench_dataset.py
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import cv2
import numpy as np

OUT = Path("data/bench/vision")
FRAMES = OUT / "frames"
random.seed(7)   # deterministic dataset

_FONT = cv2.FONT_HERSHEY_SIMPLEX


def _canvas(w=900, h=650, bg=245):
    return np.full((h, w, 3), bg, np.uint8)


def _put(img, lines, x=40, y0=70, dy=52, scale=1.0, color=(20, 20, 20), thick=2):
    y = y0
    for ln in lines:
        cv2.putText(img, ln, (x, y), _FONT, scale, color, thick, cv2.LINE_AA)
        y += dy
    return img


def _save(idx, img):
    p = FRAMES / f"{idx:04d}.jpg"
    cv2.imwrite(str(p), img, [cv2.IMWRITE_JPEG_QUALITY, 80])


# ---- per-class content generators: return (title, items, body_lines) ----------
_GROCERIES = ["Milk", "Eggs", "Bread", "Coffee", "Apples", "Rice", "Butter", "Tea"]
_TASKS = ["Book the venue", "Email Chris", "Pay rent", "Call the dentist",
          "Finish the deck", "Review the PR", "Renew passport", "Water plants"]
_NOTE_LINES = ["Demo is on Monday at 2pm", "Budget approved for Q3",
               "Sarah leads the redesign", "Ship v2 by end of month",
               "Vendor quote came in low", "Standup moved to 9:30"]
_QS = ["What is the deadline", "Who owns the rollout", "How much is the budget",
       "When does the trial end", "Which vendor did we pick", "Why did tests fail"]
_CODE = ["def add(a, b):", "    return a + b", "", "for i in range(10):",
         "    print(i * 2)", "x = [n for n in nums if n > 0]"]


def gen():
    OUT.mkdir(parents=True, exist_ok=True)
    FRAMES.mkdir(parents=True, exist_ok=True)
    labels = []
    idx = 0

    def add(content_type, title, items, ocr_lines, render, hard=None):
        nonlocal idx
        img = _canvas()
        render(img)
        _save(idx, img)
        labels.append({
            "id": f"{idx:04d}", "content_type": content_type,
            "title": title, "items": items,
            "ocr_text": "\n".join(ocr_lines), "people_count": 0,
            "hard_case": hard or ["none"], "synthetic": True,
        })
        idx += 1

    N = 10  # variants per text class
    for _ in range(N):
        # todo_list
        title = random.choice(["To Do", "Tasks", "Groceries"])
        pool = _GROCERIES if title == "Groceries" else _TASKS
        items = random.sample(pool, random.randint(3, 5))
        lines = [title] + [f"- {it}" for it in items]
        add("todo_list", title, items, lines,
            lambda im, L=lines: _put(im, L))

        # notes
        items = random.sample(_NOTE_LINES, random.randint(3, 4))
        lines = ["Notes"] + [f"* {it}" for it in items]
        add("notes", "Notes", items, lines, lambda im, L=lines: _put(im, L))

        # questions
        items = [q + "?" for q in random.sample(_QS, random.randint(3, 4))]
        lines = ["Questions"] + [f"{i+1}. {q}" for i, q in enumerate(items)]
        add("questions", "Questions", items, lines, lambda im, L=lines: _put(im, L))

        # table
        rows = [["Item", "Qty", "Price"]]
        for it in random.sample(_GROCERIES, 3):
            rows.append([it, str(random.randint(1, 9)), f"${random.randint(1,20)}"])
        items = ["  ".join(r) for r in rows]
        lines = items
        add("table", "", items, lines,
            lambda im, L=lines: _put(im, L, scale=0.9))

        # calculation
        items = []
        for _ in range(random.randint(3, 4)):
            a, b = random.randint(2, 40), random.randint(2, 40)
            items.append(f"{a} + {b} = {a+b}")
        add("calculation", "", items, items, lambda im, L=items: _put(im, L))

        # form
        fields = random.sample(["Name", "Email", "Phone", "Address", "Date"], 4)
        items = [f"{f}: ____________" for f in fields]
        lines = ["Registration"] + items
        add("form", "Registration", items, lines, lambda im, L=lines: _put(im, L))

        # code
        add("code", "", _CODE, _CODE,
            lambda im: _put(im, _CODE, scale=0.8, color=(30, 30, 30)))

    # none: plain scenes (no page of content) — ~1.5x a text class
    for _ in range(15):
        img = np.random.randint(50, 130, (650, 900, 3), np.uint8)
        cx, cy = random.randint(300, 600), random.randint(250, 400)
        cv2.circle(img, (cx, cy), random.randint(60, 120),
                   (random.randint(120, 200),) * 3, -1)
        _save(idx, img)
        labels.append({"id": f"{idx:04d}", "content_type": "none", "title": "",
                       "items": [], "ocr_text": "", "people_count": 0,
                       "hard_case": ["none"], "synthetic": True})
        idx += 1

    (OUT / "labels.jsonl").write_text(
        "\n".join(json.dumps(r) for r in labels) + "\n", encoding="utf-8")
    print(f"wrote {idx} frames + labels to {OUT}")
    from collections import Counter
    print("class balance:", dict(Counter(r["content_type"] for r in labels)))


if __name__ == "__main__":
    gen()
