"""A little chat interface for the browser agent.

Type a task in plain language and the agent does it in a live browser window.
The browser stays open across turns, so follow-ups continue where you left off:

    you > go to news.ycombinator.com and list the top 5 story titles
    you > now open the first one and summarize the discussion
    you > /open wikipedia.org
    you > what's today's featured article about?

Commands:
    /open <url>   navigate the browser to a URL
    /url          show the current page URL
    /route <req>  show how QUILL would classify a request (no execution)
    /new          clear the conversation context (browser stays put)
    /help         show this help
    /quit         close the browser and exit
"""
import argparse
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from browser_agent import config as cfg
from browser_agent.orchestrator import Agent

BANNER = """\
┌──────────────────────────────────────────────────────────────┐
│  QUILL — browser agent (chat)                                │
│  Type a task; it prepares/drafts and asks before committing. │
│  Sign in yourself in the browser window — NEVER type a       │
│  password here. Commands: /open /url /route /new /help /quit │
└──────────────────────────────────────────────────────────────┘"""

HELP = (
    "  /open <url>   navigate to a URL\n"
    "  /url          show the current page URL\n"
    "  /route <req>  show QUILL's intent/action routing for a request (no run)\n"
    "  /new          clear conversation context (browser stays put)\n"
    "  /help         show this help\n"
    "  /quit         close and exit\n"
    "  anything else is treated as a task to perform"
)


def main():
    ap = argparse.ArgumentParser(description="Chat with the browser agent")
    ap.add_argument("--start-url", default=None, help="page to open at startup")
    ap.add_argument("--headless", action="store_true",
                    help="hide the browser window (default: visible)")
    ap.add_argument("--profile", default=None,
                    help="named persistent profile — log in once, reuse the session")
    ap.add_argument("--chrome", action="store_true",
                    help="drive real installed Chrome (rarely blocked at login)")
    ap.add_argument("--channel", default=None,
                    help="browser channel (e.g. chrome, msedge); overrides --chrome")
    ap.add_argument("--cdp", default=None, metavar="URL",
                    help="attach to your own running Chrome at this CDP url "
                         "(e.g. http://127.0.0.1:9222) — reuses your logged-in session")
    ap.add_argument("--attach", action="store_true",
                    help="shortcut for --cdp http://127.0.0.1:9222")
    args = ap.parse_args()

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("ANTHROPIC_API_KEY is not set. Add it to .env (or run `ant auth login`).",
              file=sys.stderr)
        sys.exit(1)

    print(BANNER)
    print("Starting browser...")
    channel = args.channel or ("chrome" if args.chrome else None)
    cdp = args.cdp or ("http://127.0.0.1:9222" if args.attach else None)
    agent = Agent(headless=args.headless, start_url=args.start_url,
                  profile=args.profile, channel=channel, cdp_url=cdp)
    if args.profile:
        print(f"[profile '{args.profile}'] If a site needs login, sign in once in "
              "the browser window; the session is reused next time you use this profile.")
    if args.start_url:
        print(f"Opened {agent.current_url()}")

    try:
        while True:
            try:
                line = input("\nyou > ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue

            low = line.lower()
            if low in ("/quit", "/exit", "/q"):
                break
            if low in ("/help", "/h", "?"):
                print(HELP)
                continue
            if low == "/url":
                print(agent.current_url())
                continue
            if low == "/new":
                agent.transcript.clear()
                print("context cleared (browser stays where it is)")
                continue
            if low.startswith("/route"):
                req = line[6:].strip()
                if not req:
                    print("usage: /route <request>")
                    continue
                r = agent.route(req)
                print(f"  intent:          {r.get('intent')}")
                print(f"  requires_browser: {r.get('requires_browser')}")
                print(f"  needs_approval:   {r.get('requires_user_approval')}")
                print(f"  tool/site:        {r.get('tool')} / {r.get('site') or '—'}")
                print(f"  rationale:        {r.get('rationale')}")
                continue
            if low.startswith("/open"):
                url = line[5:].strip()
                if not url:
                    print("usage: /open <url>")
                    continue
                try:
                    agent.open(url)
                    print(f"opened {agent.current_url()}")
                except Exception as e:
                    print(f"open failed: {e}")
                continue

            # otherwise: run it as a task
            result, status = agent.run_goal(line)
            print("\n--- result ---")
            print(result if result else f"({status})")
            print(f"[status: {status} | session cost so far: ${agent.cost():.4f}]")
    finally:
        print("\nClosing browser...")
        agent.close()
        print(f"Episodic log: {cfg.SESSIONS_ROOT / 'episodic.db'}  (session {agent.session_id})")


if __name__ == "__main__":
    main()
