"""Eval: prove the executor's vision actually pays off (Track B — eval hardening).

The agent can already attach a screenshot when the DOM view is thin (config
EXECUTOR_VISION). That the *mechanism* fires is verified elsewhere; this proves
the *payoff*: a task that is impossible from the accessibility tree alone but
solvable from pixels.

The page renders an access code and a "Continue" button ENTIRELY inside a
<canvas> — there is no DOM text, no aria-label, nothing for `read` to return.
We run the same goal twice on it:

    A. text-only  (EXECUTOR_VISION off)  -> cannot find the code  -> FAIL (expected)
    B. vision     (EXECUTOR_VISION on)   -> reads the pixels      -> PASS (expected)

A passing eval = A fails AND B succeeds: vision is load-bearing, not decorative.

    python scripts/eval_vision_task.py            # headless, both conditions
    python scripts/eval_vision_task.py --show      # watch the browser

Needs ANTHROPIC_API_KEY (or an `ant auth login` profile) and a Chromium
(`playwright install chromium`).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

SECRET = "PLUTO-7Q"   # rendered to canvas pixels only; never placed in the DOM

_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>Access Portal</title></head>
<body style="margin:0;background:#f4f4f4;font-family:sans-serif">
<canvas id="c" width="560" height="240"></canvas>
<script>
// Everything the task needs is drawn as pixels — the DOM has no text at all,
// so an accessibility-tree-only reader is blind to it.
const ctx = document.getElementById('c').getContext('2d');
ctx.fillStyle = '#0b3d91'; ctx.fillRect(0, 0, 560, 240);
ctx.fillStyle = '#ffffff'; ctx.font = '22px Arial';
ctx.fillText('To continue, enter this access code:', 24, 64);
ctx.font = 'bold 52px Consolas'; ctx.fillStyle = '#ffd166';
ctx.fillText('%SECRET%', 24, 150);
// an image/canvas-only button (no DOM node, no accessible name)
ctx.fillStyle = '#e63946'; ctx.fillRect(24, 176, 190, 46);
ctx.fillStyle = '#ffffff'; ctx.font = '22px Arial';
ctx.fillText('Continue', 70, 206);
</script>
</body></html>"""

GOAL = ("Read the access code shown in the blue box on this page and report it. "
        "Reply with the code via done.")


def _write_page() -> str:
    out = Path("data") / "eval_canvas.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(_HTML.replace("%SECRET%", SECRET), encoding="utf-8")
    return out.resolve().as_uri()


def _run_condition(label: str, url: str, *, vision: bool, headless: bool) -> dict:
    # Toggle the executor's vision on the config module the loop reads at runtime.
    from browser_agent import config as cfg
    cfg.EXECUTOR_VISION = vision
    cfg.VISION_ALWAYS = vision   # force a screenshot every step when testing vision
    from browser_agent.orchestrator import Agent

    print(f"\n=== Condition {label} (executor vision: {'ON' if vision else 'OFF'}) ===")
    agent = Agent(headless=headless, start_url=url)
    try:
        # navigate-only keeps it a pure read task — no mutations, no approval.
        result, status = agent.run_goal(GOAL, dry_run="navigate")
        found = SECRET.lower() in (result or "").lower()
        print(f"status : {status}")
        print(f"result : {(result or '').strip()[:300]}")
        print(f"found the code: {found}   (cost ${agent.cost():.4f}, {agent.last_steps} steps)")
        return {"label": label, "vision": vision, "found": found,
                "status": status, "result": result}
    finally:
        agent.close()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Prove executor vision beats text-only")
    ap.add_argument("--show", action="store_true", help="show the browser window")
    args = ap.parse_args(argv[1:])

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("ANTHROPIC_API_KEY is not set — add it to .env to run this eval.",
              file=sys.stderr)
        return 2

    url = _write_page()
    print(f"Eval page: {url}\nSecret (canvas-only): {SECRET}")

    text_only = _run_condition("A/text-only", url, vision=False, headless=not args.show)
    vision = _run_condition("B/vision", url, vision=True, headless=not args.show)

    print("\n" + "=" * 56)
    a_fails = not text_only["found"]
    b_passes = vision["found"]
    ok = a_fails and b_passes
    print(f"  A text-only  : {'FAILED to read code (expected)' if a_fails else 'unexpectedly found it'}")
    print(f"  B vision     : {'READ the code (expected)' if b_passes else 'FAILED to read it'}")
    print("-" * 56)
    if ok:
        print("  RESULT: PASS — vision is load-bearing: the pixels were readable")
        print("          exactly where the accessibility tree was not.")
    else:
        print("  RESULT: INCONCLUSIVE — see conditions above.")
        if not a_fails:
            print("   (text-only found the code — the page leaked text into the DOM?)")
        if not b_passes:
            print("   (vision missed the code — check the screenshot path / model.)")
    print("=" * 56)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
