"""Web chat UI for the browser agent.

    python webapp.py            # then open http://127.0.0.1:5000

Sync Playwright requires the browser to live on one thread, so a single Worker
thread owns the Agent; Flask request handlers talk to it through a queue and a
shared, locked state. ask_human surfaces as a prompt in the page rather than
blocking on the terminal.
"""
import argparse
import os
import queue
import sys
import threading

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from flask import Flask, jsonify, request

from browser_agent import config as cfg
from browser_agent.orchestrator import Agent


class Worker:
    """Owns the persistent Agent on its own thread; the web layer enqueues work."""

    def __init__(self, headless, start_url, profile=None, channel=None, cdp_url=None):
        self.headless, self.start_url = headless, start_url
        self.profile, self.channel, self.cdp_url = profile, channel, cdp_url
        self.cmd_q = queue.Queue()
        self.lock = threading.Lock()
        self.events = []          # append-only [{id, kind, text}]
        self.next_id = 0
        self.busy = False
        self.ready = False
        self.awaiting = False     # waiting on a human answer (ask_human)
        self.question = None
        self.url = None
        self.cost = 0.0
        self._answer = ""
        self._answer_ev = threading.Event()
        self.agent = None
        threading.Thread(target=self._run, daemon=True).start()

    # --- emit / state ------------------------------------------------------
    def _emit(self, kind, text):
        with self.lock:
            self.events.append({"id": self.next_id, "kind": kind, "text": text})
            self.next_id += 1

    def _on_log(self, s):
        self._emit("progress", s)

    def _on_ask(self, q):          # runs on the worker thread; blocks until answered
        with self.lock:
            self.awaiting, self.question = True, q
        self._emit("ask", q)
        self._answer_ev.clear()
        self._answer_ev.wait()
        with self.lock:
            self.awaiting, self.question = False, None
            return self._answer

    def submit_answer(self, text):
        with self.lock:
            self._answer = text
        self._emit("user", text)
        self._answer_ev.set()

    def snapshot(self, since):
        with self.lock:
            evs = [e for e in self.events if e["id"] >= since]
            state = {"busy": self.busy, "ready": self.ready, "awaiting": self.awaiting,
                     "question": self.question, "url": self.url, "cost": round(self.cost, 4)}
        return evs, state

    # --- enqueue -----------------------------------------------------------
    def send(self, text):
        self.cmd_q.put({"type": "goal", "text": text})

    def open(self, url):
        self.cmd_q.put({"type": "open", "text": url})

    def new(self):
        self.cmd_q.put({"type": "new"})

    # --- the thread --------------------------------------------------------
    def _run(self):
        self.agent = Agent(headless=self.headless, start_url=self.start_url,
                           on_log=self._on_log, on_ask=self._on_ask,
                           profile=self.profile, channel=self.channel,
                           cdp_url=self.cdp_url)
        with self.lock:
            self.url, self.ready = self.agent.current_url(), True
        self._emit("system", f"Browser ready — {self.url}")
        if self.profile:
            self._emit("system", f"Profile '{self.profile}': if a site needs login, "
                       "sign in once in the browser window — it's reused next time.")
        while True:
            cmd = self.cmd_q.get()
            typ = cmd.get("type")
            try:
                if typ == "goal":
                    with self.lock:
                        self.busy = True
                    self._emit("user", cmd["text"])
                    result, status = self.agent.run_goal(cmd["text"])
                    self._emit("result", result or f"(no answer — {status})")
                elif typ == "open":
                    self.agent.open(cmd["text"])
                    self._emit("system", f"opened {self.agent.current_url()}")
                elif typ == "new":
                    self.agent.transcript.clear()
                    self._emit("system", "conversation context cleared")
            except Exception as e:
                self._emit("error", f"{type(e).__name__}: {e}")
            finally:
                with self.lock:
                    self.busy = False
                    try:
                        self.url, self.cost = self.agent.current_url(), self.agent.cost()
                    except Exception:
                        pass


app = Flask(__name__)
worker: Worker = None


@app.get("/")
def index():
    return PAGE


@app.post("/send")
def send():
    text = ((request.get_json(silent=True) or {}).get("text") or "").strip()
    if not text:
        return jsonify(ok=False, error="empty")
    with worker.lock:
        awaiting, busy = worker.awaiting, worker.busy
    if awaiting:                      # same box answers an ask_human prompt
        worker.submit_answer(text)
        return jsonify(ok=True, routed="answer")
    if busy:
        return jsonify(ok=False, error="busy")
    worker.send(text)
    return jsonify(ok=True, routed="goal")


@app.post("/open")
def open_url():
    url = ((request.get_json(silent=True) or {}).get("url") or "").strip()
    if url:
        worker.open(url)
    return jsonify(ok=True)


@app.post("/new")
def new_ctx():
    worker.new()
    return jsonify(ok=True)


@app.get("/poll")
def poll():
    since = int(request.args.get("since", 0))
    evs, state = worker.snapshot(since)
    return jsonify(events=evs, state=state)


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>Exec.AI — browser agent</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{--bg:#0f1115;--panel:#171a21;--mut:#8b93a7;--line:#262b36;--acc:#4f8cff;}
  *{box-sizing:border-box} body{margin:0;font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif;
    background:var(--bg);color:#e6e9ef;height:100vh;display:flex;flex-direction:column}
  header{padding:10px 16px;border-bottom:1px solid var(--line);display:flex;gap:12px;
    align-items:center;background:var(--panel)}
  header b{font-weight:600} .url{color:var(--mut);font-size:13px;max-width:48vw;overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap} .spacer{flex:1}
  .cost{color:var(--mut);font-size:13px} .dot{width:9px;height:9px;border-radius:50%;
    background:#3a4150;display:inline-block;margin-right:6px} .dot.busy{background:#f0b429;
    animation:p 1s infinite} @keyframes p{50%{opacity:.3}}
  button,input{font:inherit} .ctl{background:#222634;color:#cfd5e3;border:1px solid var(--line);
    border-radius:8px;padding:6px 10px;cursor:pointer} .ctl:hover{border-color:#384156}
  #log{flex:1;overflow:auto;padding:18px;display:flex;flex-direction:column;gap:10px}
  .msg{max-width:78%;padding:9px 13px;border-radius:12px;white-space:pre-wrap;word-wrap:break-word}
  .user{align-self:flex-end;background:var(--acc);color:#fff}
  .result{align-self:flex-start;background:#16261c;border:1px solid #2c6b46}
  .system{align-self:center;color:var(--mut);font-size:13px;background:transparent}
  .ask{align-self:flex-start;background:#2a2410;border:1px solid #7a5d12}
  .error{align-self:flex-start;background:#2a1416;border:1px solid #7a2230}
  .progress{align-self:flex-start;color:var(--mut);font:12px/1.45 ui-monospace,Consolas,monospace;
    background:#12151c;border:1px solid var(--line);border-radius:8px;padding:6px 10px;max-width:88%}
  footer{padding:12px 16px;border-top:1px solid var(--line);background:var(--panel);
    display:flex;gap:10px} #box{flex:1;background:#0c0e13;color:#e6e9ef;border:1px solid var(--line);
    border-radius:10px;padding:11px 13px;resize:none;height:46px} #send{background:var(--acc);
    color:#fff;border:none;border-radius:10px;padding:0 20px;cursor:pointer;font-weight:600}
  #send:disabled{opacity:.5;cursor:default}
</style></head><body>
<header>
  <span><span id="dot" class="dot"></span><b>Exec.AI</b></span>
  <span id="url" class="url"></span>
  <span class="spacer"></span>
  <input id="openurl" class="ctl" placeholder="open url…" style="width:180px">
  <button class="ctl" onclick="openUrl()">Open</button>
  <button class="ctl" onclick="newCtx()">New context</button>
  <span id="cost" class="cost"></span>
</header>
<div id="log"></div>
<footer>
  <div id="approvebar" style="display:none;gap:8px;align-items:center">
    <button class="ctl" onclick="approve(true)" style="border-color:#2c6b46;color:#7ee0a6">✓ Approve</button>
    <button class="ctl" onclick="approve(false)" style="border-color:#7a2230;color:#f0a0aa">✕ Deny</button>
  </div>
  <textarea id="box" placeholder="Describe a task… (it prepares & drafts; irreversible steps need your approval)"></textarea>
  <button id="send" onclick="send()">Send</button>
</footer>
<script>
let since=0, busy=false, awaiting=false;
const log=document.getElementById('log'), box=document.getElementById('box'),
      sendBtn=document.getElementById('send');
function add(kind,text){const d=document.createElement('div');d.className='msg '+kind;
  d.textContent=text;log.appendChild(d);log.scrollTop=log.scrollHeight;}
async function poll(){
  try{
    const r=await fetch('/poll?since='+since); const j=await r.json();
    for(const e of j.events){ since=e.id+1; add(e.kind, e.text); }
    const s=j.state; busy=s.busy; awaiting=s.awaiting;
    document.getElementById('url').textContent=s.url||'';
    document.getElementById('cost').textContent=s.ready?('$'+s.cost.toFixed(4)):'starting…';
    document.getElementById('dot').className='dot'+(busy?' busy':'');
    sendBtn.disabled = busy && !awaiting;
    const isApproval = awaiting && /^APPROVAL NEEDED/.test(s.question||'');
    document.getElementById('approvebar').style.display = isApproval ? 'flex' : 'none';
    box.placeholder = awaiting ? ('Agent asked: '+s.question+'  — type your reply')
      : 'Describe a task… (it prepares & drafts; irreversible steps need your approval)';
  }catch(e){}
}
function approve(ok){ box.value = ok ? 'approve' : 'deny'; send(); }
async function send(){
  const t=box.value.trim(); if(!t) return;
  if(busy && !awaiting) return;
  box.value='';
  const r=await fetch('/send',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text:t})});
  const j=await r.json();
  if(!j.ok && j.error==='busy') add('system','(agent is busy — wait for it to finish)');
  poll();
}
async function openUrl(){const u=document.getElementById('openurl').value.trim();if(!u)return;
  document.getElementById('openurl').value='';
  await fetch('/open',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url:u})}); poll();}
async function newCtx(){await fetch('/new',{method:'POST'}); poll();}
box.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}});
setInterval(poll, 700); poll();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser(description="Web chat UI for the browser agent")
    ap.add_argument("--start-url", default=None)
    ap.add_argument("--headless", action="store_true",
                    help="hide the agent's browser window (default: visible)")
    ap.add_argument("--profile", default=None,
                    help="named persistent profile — reuse a logged-in session")
    ap.add_argument("--chrome", action="store_true",
                    help="drive real installed Chrome (rarely blocked at login)")
    ap.add_argument("--channel", default=None, help="browser channel (chrome, msedge)")
    ap.add_argument("--cdp", default=None, metavar="URL",
                    help="attach to your running Chrome (e.g. http://127.0.0.1:9222)")
    ap.add_argument("--attach", action="store_true",
                    help="shortcut for --cdp http://127.0.0.1:9222")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("ANTHROPIC_API_KEY is not set. Add it to .env (or run `ant auth login`).",
              file=sys.stderr)
        sys.exit(1)

    global worker
    channel = args.channel or ("chrome" if args.chrome else None)
    cdp = args.cdp or ("http://127.0.0.1:9222" if args.attach else None)
    worker = Worker(headless=args.headless, start_url=args.start_url,
                    profile=args.profile, channel=channel, cdp_url=cdp)
    print(f"\n  Exec.AI UI -> http://127.0.0.1:{args.port}\n  (Ctrl+C to stop)\n")
    # threaded=True so /poll stays responsive while a goal runs on the worker.
    app.run(host="127.0.0.1", port=args.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
