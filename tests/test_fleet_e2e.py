"""End to end: two Sparrows and the firm relay on one box, over real HTTP.

An agent on Sparrow A publishes; an agent on Sparrow B receives it within
2 s with peer provenance; nothing comes back to A; and the relay's log
verifies. Each process has its own data dir, the same way on-box peers use
internal_url.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib import request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_client import FleetClient  # noqa: E402

ADMIN = "e2e-admin-token-0123456789abcdef"
COMPLIANCE = "e2e-compliance-token-0123456789abcdef"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _call(method: str, url: str, body: dict | None = None,
          token: str = "") -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = request.Request(url, method=method, headers=headers,
                          data=None if body is None
                          else json.dumps(body).encode("utf-8"))
    with request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def _wait_up(url: str, proc: subprocess.Popen, timeout: float = 60) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if proc.poll() is not None:
            raise RuntimeError(f"{url} exited: {proc.stdout.read()[-2000:]}")
        try:
            with request.urlopen(url, timeout=1):
                return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"{url} never came up")


@unittest.skipUnless(os.environ.get("QUILL_SKIP_E2E") not in ("1", "true"),
                     "QUILL_SKIP_E2E set")
class FleetEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="fleet_e2e_"))
        cls.procs = []
        base_env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("QUILL_FLEET", "QUILL_PEER",
                                         "QUILL_RELAY", "QUILL_ORG"))}
        cls.relay_port = _free_port()
        cls.relay_url = f"http://127.0.0.1:{cls.relay_port}"
        relay_env = {**base_env,
                     "QUILL_ORG_COORD_DATA": str(cls.tmp / "relay"),
                     "QUILL_RELAY_ADMIN_TOKEN": ADMIN,
                     "QUILL_RELAY_COMPLIANCE_TOKEN": COMPLIANCE}
        cls._spawn("org_coordinator.main:app", cls.relay_port, relay_env)
        cls.sparrows = {}
        for name in ("a", "b"):
            port = _free_port()
            data = cls.tmp / f"sparrow_{name}"
            data.mkdir(parents=True)
            (data / "restricted_list.json").write_text(
                '{"instruments": ["XYZ"]}', encoding="utf-8")
            env = {**base_env, "QUILL_DATA_DIR": str(data), "QUILL_FLEET": "1",
                   "QUILL_PORT": str(port), "QUILL_PEER_INGEST": "0",
                   "QUILL_PEER_TELEMETRY": "0", "QUILL_DESKTOP_JAIL":
                   str(cls.tmp / f"jail_{name}")}
            cls._spawn("tests.fleet_e2e_app:app", port, env)
            cls.sparrows[name] = f"http://127.0.0.1:{port}"
        _wait_up(f"{cls.relay_url}/health", cls.procs[0])
        for i, url in enumerate(cls.sparrows.values(), start=1):
            _wait_up(f"{url}/fleet/schema", cls.procs[i])

    @classmethod
    def _spawn(cls, target: str, port: int, env: dict) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", target, "--host", "127.0.0.1",
             "--port", str(port), "--log-level", "warning"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)
        cls.procs.append(proc)

    @classmethod
    def tearDownClass(cls) -> None:
        for p in cls.procs:
            p.terminate()
        for p in cls.procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()

    def test_a_publishes_b_receives_nothing_comes_back(self) -> None:
        a, b = self.sparrows["a"], self.sparrows["b"]
        # Owners register their agents (the token is shown once).
        quant = _call("POST", f"{a}/fleet/agents",
                      {"name": "quant", "topics": ["macro.rates"],
                       "role": "publisher"})["agent"]["token"]
        watcher_a = _call("POST", f"{a}/fleet/agents",
                          {"name": "watch", "topics": ["macro.rates"],
                           "role": "subscriber"})["agent"]["token"]
        reader_b = _call("POST", f"{b}/fleet/agents",
                         {"name": "reader", "topics": ["macro.rates"],
                          "role": "subscriber"})["agent"]["token"]
        # Each Sparrow enrolls with the relay; an admin sets the barrier.
        for name, url in (("node-a", a), ("node-b", b)):
            out = _call("POST", f"{url}/fleet/relay/register",
                        {"relay_url": self.relay_url, "node_id": name,
                         "fleet_url": url})
            self.assertTrue(out["ok"], out)
            _call("PUT", f"{self.relay_url}/relay/admin/nodes/{name}/group",
                  {"group": "research"}, ADMIN)
        _call("PUT", f"{self.relay_url}/relay/admin/topics/macro.rates",
              {"members": ["node-a", "node-b"], "groups": ["research"]}, ADMIN)
        _call("PUT", f"{self.relay_url}/relay/admin/restricted",
              {"instruments": ["XYZ"]}, ADMIN)
        # Both owners opt the topic in. B sharing too would expose any
        # re-forward of inbound signals.
        for url in (a, b):
            _call("PUT", f"{url}/fleet/routes",
                  {"rules": [{"topic": "macro.rates", "action": "share"}]})

        got_b: list[tuple[float, dict]] = []
        got_a: list[dict] = []

        def listen(url, token, sink, stamp):
            fc = FleetClient(url, token)
            for item in fc.subscribe(["macro.rates"], max_s=6.0):
                sink.append((time.monotonic(), item) if stamp else item)

        threads = [threading.Thread(target=listen,
                                    args=(b, reader_b, got_b, True)),
                   threading.Thread(target=listen,
                                    args=(a, watcher_a, got_a, False))]
        for t in threads:
            t.start()
        time.sleep(1.0)

        t0 = time.monotonic()
        pub = FleetClient(a, quant).publish({
            "topic": "macro.rates", "instrument": "TLT",
            "direction": "bearish", "horizon": "weeks", "confidence": 0.7,
            "thesis": "Term premium is rebuilding after the auction tail.",
            "sources": [{"name": "desk notes", "license": "internal_ok"}]})
        self.assertEqual(pub["route"]["action"], "share")
        origin = pub["signal"]["origin_id"]

        # A restricted name never reaches the network.
        refused = FleetClient(a, quant).publish({
            "topic": "macro.rates", "instrument": "XYZ",
            "direction": "bullish", "horizon": "days", "confidence": 0.5,
            "thesis": "Restricted name.",
            "sources": [{"name": "desk notes", "license": "internal_ok"}]})
        self.assertEqual(refused["route"]["action"], "refused")

        for t in threads:
            t.join(15)

        self.assertEqual(len(got_b), 1, got_b)
        arrived, item = got_b[0]
        self.assertLess(arrived - t0, 2.0)
        self.assertEqual(item["origin_id"], origin)
        self.assertEqual(item["provenance"], "peer")
        self.assertEqual(item["peer_node"], "node-a")
        self.assertEqual(item["hops"], 1)
        self.assertEqual(item["signal"]["thesis"], pub["signal"]["thesis"])

        # Nothing comes back to A: its watcher saw the local signal (and the
        # refused one, which stayed local) and never a peer copy.
        self.assertTrue(got_a)
        self.assertTrue(all(i["provenance"] == "local" for i in got_a), got_a)
        self.assertEqual(sum(1 for i in got_a if i["origin_id"] == origin), 1)

        # The relay forwarded exactly once, to B only, and its log verifies.
        feed = _call("GET", f"{self.relay_url}/relay/compliance/feed",
                     token=COMPLIANCE)["rows"]
        fwd = [r for r in feed if r["kind"] == "forward"]
        self.assertEqual(len(fwd), 1)
        self.assertEqual(fwd[0]["sender"], "node-a")
        self.assertEqual(fwd[0]["recipients"], ["node-b"])
        self.assertFalse([r for r in feed if r["kind"] == "forward"
                          and r["sender"] == "node-b"])
        verdict = _call("GET", f"{self.relay_url}/relay/compliance/verify",
                        token=COMPLIANCE)
        self.assertTrue(verdict["ok"], verdict)


if __name__ == "__main__":
    unittest.main()
