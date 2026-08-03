"""CLI entrypoint for the P0 read-only browser agent.

Examples:
  python run.py "Summarize the top 5 stories" --start-url https://news.ycombinator.com
  python run.py "What's the current weather in Boston?" --headful
"""
import argparse
import os
import sys

try:
    from dotenv import load_dotenv  # optional
    load_dotenv()
except Exception:
    pass

from browser_agent import config as cfg
from browser_agent.orchestrator import run


def main():
    ap = argparse.ArgumentParser(description="FS-BA-001 P0 read-only browser agent")
    ap.add_argument("goal", help="natural-language task")
    ap.add_argument("--start-url", default=None, help="page to open before planning")
    ap.add_argument("--headful", action="store_true", help="show the browser window")
    ap.add_argument("--max-steps", type=int, default=None, help="override step cap")
    ap.add_argument("--profile", default=None,
                    help="named persistent profile — reuse a logged-in session")
    ap.add_argument("--chrome", action="store_true",
                    help="drive real installed Chrome (rarely blocked at login)")
    ap.add_argument("--channel", default=None, help="browser channel (chrome, msedge)")
    ap.add_argument("--cdp", default=None, metavar="URL",
                    help="attach to your running Chrome (e.g. http://127.0.0.1:9222)")
    ap.add_argument("--attach", action="store_true",
                    help="shortcut for --cdp http://127.0.0.1:9222")
    args = ap.parse_args()

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("ANTHROPIC_API_KEY is not set. Set it (or run `ant auth login`) "
              "and try again.", file=sys.stderr)
        sys.exit(1)

    if args.max_steps:
        cfg.MAX_STEPS = args.max_steps

    channel = args.channel or ("chrome" if args.chrome else None)
    cdp = args.cdp or ("http://127.0.0.1:9222" if args.attach else None)
    run(args.goal, start_url=args.start_url, headless=not args.headful,
        profile=args.profile, channel=channel, cdp_url=cdp)


if __name__ == "__main__":
    main()
