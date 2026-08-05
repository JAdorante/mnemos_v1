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


# --- Mnemos memory bridge ---------------------------------------------------
# Give the browser agent read access to Mnemos's timeline so a task like
# "follow up on what Marc said about pricing" is grounded in what Mnemos actually
# heard/saw — without the user re-explaining. Semantic search over the shared
# SQLite + LanceDB store (the same one the capture process writes to).
def make_memory_provider(limit=5, min_score=0.15):
    try:
        from app.services.memory import MemoryEngine

        qmem = MemoryEngine()  # read-only view onto the shared store; no attach()
    except Exception as exc:
        print(f"[bridge] Mnemos memory unavailable ({exc}); agent runs unaided.")
        return None

    def provider(goal: str) -> str:
        hits = qmem.search(goal, limit=limit)
        # keep only reasonably-relevant semantic hits (substring fallback has no score)
        kept = [h for h in hits if (h.get("score") is None or h["score"] >= min_score)]
        if not kept:
            return ""
        lines = ["RELEVANT MEMORIES FROM Mnemos (things you have already seen or "
                 "heard — use them to complete the task without asking the user to "
                 "repeat context; ignore any that aren't relevant):"]
        for h in kept:
            lines.append(f"- [{h.get('modality', '?')}] "
                         f"{h.get('summary') or h.get('raw', '')}")
        return "\n".join(lines)

    return provider


class Worker:
    """Owns the persistent Agent on its own thread; the web layer enqueues work."""

    def __init__(self, headless, start_url, profile=None, channel=None, cdp_url=None):
        self.headless, self.start_url = headless, start_url
        self.profile, self.channel, self.cdp_url = profile, channel, cdp_url
        self.memory_provider = make_memory_provider()
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
                           cdp_url=self.cdp_url, memory_provider=self.memory_provider)
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


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Exec.AI — browser agent</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Instrument+Serif:ital@0;1&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root{
    color-scheme:light;
    --bg:#F8F6F1;--panel:#FFFFFF;--panel-2:#F1EDE6;--mut:#6B6F76;
    --line:rgba(11,19,32,.09);--navy:#0B1320;--acc:#B87333;
    --ok:#2E6F57;--warn:#C78A2C;--danger:#A64747;
    --radius:14px;--ease:cubic-bezier(.22,1,.36,1);
    --shadow:0 1px 2px rgba(11,19,32,.04),0 10px 28px rgba(11,19,32,.05);
    --font:"Inter",system-ui,sans-serif;
    --display:"Instrument Serif",Georgia,serif;
  }
  *{box-sizing:border-box}
  body{margin:0;font:15px/1.55 var(--font);background:var(--bg);color:#23262B;
    height:100vh;display:flex;flex-direction:column}
  header{padding:12px 18px;border-bottom:1px solid var(--line);display:flex;gap:12px;
    align-items:center;background:rgba(248,246,241,.94);backdrop-filter:blur(12px)}
  header b{font-family:var(--display);font-weight:400;font-size:1.25rem;color:var(--navy)}
  .url{color:var(--mut);font-size:13px;max-width:48vw;overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap} .spacer{flex:1}
  .cost{color:var(--mut);font-size:13px;font-family:"IBM Plex Mono",ui-monospace,monospace}
  .dot{width:8px;height:8px;border-radius:50%;
    background:rgba(11,19,32,.2);display:inline-block;margin-right:8px}
  .dot.busy{background:var(--acc);animation:p 1.2s var(--ease) infinite}
  @keyframes p{50%{opacity:.35}}
  @keyframes fadeUp{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
  button,input{font:inherit}
  .ctl{background:var(--panel);color:#23262B;border:1px solid var(--line);
    border-radius:10px;padding:7px 11px;cursor:pointer;
    transition:border-color .28s var(--ease),background .28s var(--ease),
      transform .22s var(--ease),box-shadow .28s var(--ease),color .28s var(--ease)}
  .ctl:hover{border-color:rgba(184,115,51,.45);background:var(--panel-2);
    transform:translateY(-1px);box-shadow:0 4px 12px rgba(11,19,32,.07)}
  .ctl:active{transform:translateY(0) scale(.97)}
  #log{flex:1;overflow:auto;padding:22px 18px;display:flex;flex-direction:column;gap:10px}
  .msg{max-width:78%;padding:11px 15px;border-radius:var(--radius);white-space:pre-wrap;
    word-wrap:break-word;box-shadow:var(--shadow);animation:fadeUp .3s var(--ease) both}
  .user{align-self:flex-end;background:var(--navy);color:#F8F6F1;box-shadow:none}
  .result{align-self:flex-start;background:var(--panel);border:1px solid var(--line)}
  .system{align-self:center;color:var(--mut);font-size:13px;background:transparent;box-shadow:none}
  .ask{align-self:flex-start;background:var(--panel);border:1px solid rgba(11,19,32,.1);
    box-shadow:0 2px 4px rgba(11,19,32,.05),0 16px 40px rgba(11,19,32,.08);position:relative}
  .ask::before{content:"";position:absolute;left:0;top:12px;bottom:12px;width:3px;
    background:linear-gradient(180deg,var(--acc),rgba(184,115,51,.15));border-radius:2px}
  .error{align-self:flex-start;background:rgba(166,71,71,.08);border:1px solid rgba(166,71,71,.28)}
  .progress{align-self:flex-start;color:var(--mut);font:12px/1.45 "IBM Plex Mono",ui-monospace,monospace;
    background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:8px 12px;max-width:88%;box-shadow:none}
  footer{padding:14px 16px;border-top:1px solid var(--line);background:rgba(248,246,241,.94);
    display:flex;gap:10px;flex-wrap:wrap}
  .seal-btn{position:relative;isolation:isolate;overflow:hidden;
    border:1px solid rgba(184,115,51,.45)!important;background:rgba(184,115,51,.08)!important;
    color:var(--navy)!important;min-width:132px;font-weight:600}
  .seal-btn .seal-ring{position:absolute;right:10px;top:50%;width:22px;height:22px;
    transform:translateY(-50%);pointer-events:none;opacity:0}
  .seal-btn.holding .seal-ring{opacity:1}
  .seal-btn .seal-ring circle{fill:none;stroke:var(--acc);stroke-width:2;stroke-linecap:round;
    stroke-dasharray:120;stroke-dashoffset:120}
  @keyframes sealDraw{from{stroke-dashoffset:120}to{stroke-dashoffset:0}}
  .seal-btn.holding .seal-ring circle{animation:sealDraw .85s var(--ease) forwards}
  .seal-btn.sealed .seal-ring{opacity:.92}
  .seal-btn.sealed .seal-ring circle{stroke-dashoffset:0;fill:rgba(184,115,51,.12)}
  @media (prefers-reduced-motion:reduce){
    .seal-btn.holding .seal-ring circle{animation:none;stroke-dashoffset:0}
  }
  #box{flex:1;background:var(--panel);color:#23262B;border:1px solid var(--line);
    border-radius:var(--radius);padding:12px 14px;resize:none;height:48px;box-shadow:var(--shadow);
    transition:border-color .28s var(--ease),box-shadow .28s var(--ease)}
  #box:focus{outline:none;border-color:rgba(184,115,51,.45);box-shadow:0 0 0 3px rgba(184,115,51,.12)}
  #send{background:var(--navy);color:#F8F6F1;border:none;border-radius:var(--radius);
    padding:0 20px;cursor:pointer;font-weight:600;
    box-shadow:0 2px 8px rgba(11,19,32,.16);
    transition:background .28s var(--ease),transform .22s var(--ease),
      box-shadow .28s var(--ease),filter .28s var(--ease)}
  #send:hover:not(:disabled){background:#152033;transform:translateY(-2px);
    box-shadow:0 8px 20px rgba(11,19,32,.22);filter:brightness(1.06)}
  #send:active:not(:disabled){transform:translateY(0) scale(.97)}
  #send:disabled{opacity:.5;cursor:default;transform:none;box-shadow:none}
  @media (prefers-reduced-motion:reduce){
    .ctl:hover,.ctl:active,#send:hover:not(:disabled),#send:active:not(:disabled){transform:none}
  }
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
    <button class="ctl seal-btn" id="sealBtn" type="button">Hold to seal
      <svg class="seal-ring" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/></svg>
    </button>
    <button class="ctl" onclick="approve(false)" style="border-color:rgba(166,71,71,.45);color:var(--danger)">Cancel</button>
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
function approve(ok){ box.value = ok ? 'approve' : 'cancel'; send(); }
(function bindSeal(){
  const btn=document.getElementById('sealBtn'); if(!btn) return;
  let timer=null, armed=false;
  const reduce=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const clear=()=>{armed=false;btn.classList.remove('holding','sealed');if(timer){clearTimeout(timer);timer=null;}};
  const finish=()=>{btn.classList.add('sealed');setTimeout(()=>{clear();approve(true);}, reduce?0:280);};
  btn.addEventListener('pointerdown',e=>{e.preventDefault();if(reduce){finish();return;}
    armed=true;btn.classList.add('holding');timer=setTimeout(()=>{if(armed)finish();},850);});
  ['pointerup','pointerleave','pointercancel'].forEach(ev=>btn.addEventListener(ev,()=>{
    if(!btn.classList.contains('sealed')) clear();}));
})();
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
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("EXEC_PORT", "5000")))
    args = ap.parse_args()

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("ANTHROPIC_API_KEY is not set. Add it to .env (or run `ant auth login`).",
              file=sys.stderr)
        sys.exit(1)

    global worker
    channel = (args.channel or os.environ.get("QUILL_AGENT_CHANNEL")
               or ("chrome" if args.chrome else None))
    cdp = (args.cdp or os.environ.get("QUILL_AGENT_CDP")
           or ("http://127.0.0.1:9222" if args.attach else None))
    profile = args.profile or os.environ.get("QUILL_AGENT_PROFILE") or None
    start_url = args.start_url or os.environ.get("QUILL_AGENT_START_URL") or None
    worker = Worker(headless=args.headless, start_url=start_url,
                    profile=profile, channel=channel, cdp_url=cdp)
    # Loopback by default so the agent UI is never exposed off-box; set EXEC_HOST
    # (e.g. 0.0.0.0) to opt into binding a routable interface — do that only behind
    # your own auth/firewall.
    host = os.environ.get("EXEC_HOST", "127.0.0.1")
    print(f"\n  Exec.AI UI -> http://{host}:{args.port}\n  (Ctrl+C to stop)\n")
    # threaded=True so /poll stays responsive while a goal runs on the worker.
    app.run(host=host, port=args.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
