"""CAL Stage 2 — frame segmentation.

A frame is what the user is working ON, held across the gaps and glances that
make a raw event stream unreadable. `now_context` is a decay field over the
graph (what is warm) and `wm_slots` is a surfacing budget; neither says "this
is a stretch of work about Boostrun that began at 10:02", which is the thing a
timeline is made of.

The segmenter is a PURE FUNCTION over an ordered stream. It never reads the
clock and never touches the database: time only ever arrives as an event's
timestamp. That is what lets the same code replay eleven hours of history in a
second and run incrementally on live capture, and it is why the rules below can
be tested at all — measured against one real day, this design's rules fire 0,
1 and 2 times respectively, so the golden streams in the tests are not a
convenience, they are the only place most of this logic is exercised.

Three rules carry the weight, and none of them is the one the design leads with:

  interrupt vs switch  A glance at mail is not a context switch. An excursion
                       that RETURNS is nested under its parent and does not cut
                       the episode; only sustained dwell elsewhere switches.
  hysteresis           Two thresholds, never one. A single threshold flaps, and
                       a flapping segmenter shreds an hour into fragments.
  freeze on idle       Lunch is not a context switch. With nothing competing,
                       evidence is FROZEN rather than decayed, so walking away
                       and coming back to the same work resumes it.

The forced 90-minute split is in here for completeness and, on real data, never
fires: the longest continuous run measured was 19 minutes. The hard problem on
a real stream is aggregation, not splitting — `activities` already shatters into
one block per click, which is the failure this replaces.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

# --- tuning ------------------------------------------------------------------
# Mirrors working_memory's THETA_IN/_theta_out deliberately: a second set of
# hysteresis constants that drifted from the first would be its own bug.
THETA_IN = 0.62          # a competitor must reach this share to take over
THETA_OUT = 0.38         # ...AND the incumbent must fall below this
SWITCH_DWELL_S = 90.0    # ...AND the competitor must hold attention this long
INTERRUPT_MAX_S = 180.0  # an excursion shorter than this nests, never cuts
IDLE_SUSPEND_S = 1200.0  # 20 min of silence suspends the frame
FREEZE_S = 2700.0        # ...but evidence does not decay for 45 min
SUSPEND_EXPIRY_S = 7200.0
MAX_FRAME_S = 5400.0     # 90 min — defensive; a longer frame is a seg failure
STACK_DEPTH = 4
IDLE_FLOOR = 0.05        # below this, nothing is competing for attention
EV_CAP = 3.0             # per-anchor evidence ceiling — see _observe

# Evidence half-life by the tier that carried it. A strong key stays warm far
# longer than a window-title guess, because it was far more certain to begin.
TAU_BY_TIER = {"strong": 1200.0, "medium": 480.0, "supporting": 300.0}
_DEFAULT_TAU = 480.0


@dataclass(frozen=True)
class Anchor:
    """One piece of evidence that an event is about a node."""
    node_type: str            # entity | person | key
    node_id: int | str
    name: str = ""
    strength: float = 0.5
    tier: str = "medium"
    nameable: bool = True     # may this anchor NAME a frame, or only vote?

    @property
    def key(self) -> tuple[str, object]:
        return (self.node_type, self.node_id)


@dataclass(frozen=True)
class StreamEvent:
    """Everything the segmenter needs, and nothing it does not.

    Deliberately not an `app.events.Event`: the segmenter must be feedable from
    a replay query, a synthetic golden and the live bus without any of them
    having to construct the other's shape.
    """
    event_id: int
    t: float
    anchors: tuple[Anchor, ...] = ()
    app: str = ""
    title: str = ""


@dataclass
class Frame:
    id: int
    key: tuple[str, object] | None      # None = an honest unbound stretch
    name: str
    started_at: float
    last_evidence_at: float
    state: str = "active"               # active | suspended | closed
    parent_id: int | None = None
    ended_at: float | None = None
    n_events: int = 0
    n_anchored: int = 0
    apps: dict = field(default_factory=dict)
    switch_reason: str = ""

    @property
    def coherence(self) -> float:
        """Share of this frame's events that actually carried its anchor.

        A frame held together by inheritance alone is a weaker claim than one
        re-evidenced throughout, and the timeline should be able to say so.
        """
        return (self.n_anchored / self.n_events) if self.n_events else 0.0


@dataclass(frozen=True)
class FrameEvent:
    kind: str                 # open | interrupt | switch | suspend | resume | close
    frame: Frame
    t: float
    reason: str = ""


@dataclass(frozen=True)
class Placement:
    """Where one stream event landed. `inherited` is the load-bearing case."""
    event_id: int
    frame_id: int | None
    inherited: bool
    frame_events: tuple[FrameEvent, ...] = ()


class Segmenter:
    """Ordered stream in, frame lifecycle out. Feed events in time order."""

    def __init__(self, *, theta_in: float = THETA_IN,
                 theta_out: float = THETA_OUT,
                 switch_dwell_s: float = SWITCH_DWELL_S,
                 interrupt_max_s: float = INTERRUPT_MAX_S,
                 idle_suspend_s: float = IDLE_SUSPEND_S,
                 freeze_s: float = FREEZE_S,
                 max_frame_s: float = MAX_FRAME_S):
        self.theta_in = theta_in
        self.theta_out = theta_out
        self.switch_dwell_s = switch_dwell_s
        self.interrupt_max_s = interrupt_max_s
        self.idle_suspend_s = idle_suspend_s
        self.freeze_s = freeze_s
        self.max_frame_s = max_frame_s

        self._evidence: dict[tuple[str, object], float] = {}
        self._names: dict[tuple[str, object], str] = {}
        self._tiers: dict[tuple[str, object], str] = {}
        self._nameable: dict[tuple[str, object], bool] = {}
        self._t: float | None = None
        self._next_id = 1
        self.stack: list[Frame] = []          # innermost last
        self.suspended: list[Frame] = []
        self.closed: list[Frame] = []
        self._pending: dict | None = None     # a competitor accruing dwell

    # --- evidence field ----------------------------------------------------
    def _decay(self, dt: float) -> None:
        """Age the evidence field — unless nothing is competing for attention.

        Decaying on wall-clock produces the deeply annoying behavior of a system
        that forgets what you were doing while you ate. Competition is what
        should cost a frame its hold, not the passage of time.
        """
        if dt <= 0 or not self._evidence:
            return
        total = sum(self._evidence.values())
        if total < IDLE_FLOOR and dt < self.freeze_s:
            return                                  # frozen, not decayed
        for k in list(self._evidence):
            tau = TAU_BY_TIER.get(self._tiers.get(k, ""), _DEFAULT_TAU)
            v = self._evidence[k] * math.exp(-dt / tau)
            if v < 1e-4:
                del self._evidence[k]
            else:
                self._evidence[k] = v

    def _observe(self, ev: StreamEvent) -> None:
        for a in ev.anchors:
            # SATURATING, not additive. Seeing the same anchor four hundred
            # times in one sitting is one observation of "I am working on this",
            # not four hundred — the same independence argument the belief layer
            # makes about evidence buckets. Without the cap an incumbent becomes
            # arithmetically undisplaceable and the share thresholds below can
            # never fire, so the segmenter would produce exactly one frame per
            # day and call it a success.
            self._evidence[a.key] = min(
                EV_CAP, self._evidence.get(a.key, 0.0) + a.strength)
            self._names[a.key] = a.name or self._names.get(a.key, "")
            self._nameable[a.key] = (self._nameable.get(a.key, True)
                                     and a.nameable)
            # Keep the STRONGEST tier ever seen for a key: it sets the half-life,
            # and a key proven by a git remote should not start decaying like a
            # window-title guess because the latest sighting was a guess.
            prev = self._tiers.get(a.key)
            order = {"supporting": 0, "medium": 1, "strong": 2}
            if prev is None or order.get(a.tier, 1) > order.get(prev, 1):
                self._tiers[a.key] = a.tier

    def _share(self, key) -> float:
        total = sum(self._evidence.values())
        return (self._evidence.get(key, 0.0) / total) if total > 0 else 0.0

    def _dominant(self):
        """The anchor a frame would be named after — never an unnameable one.

        App identity still competes for attention (it is in the evidence field,
        so switching browsers can still lose a frame), but it cannot claim one:
        a timeline that says "Firefox" for ninety minutes is describing the
        capture system, not the day.
        """
        live = {k: v for k, v in self._evidence.items()
                if self._nameable.get(k, True)}
        if not live:
            return None
        return max(live.items(), key=lambda kv: kv[1])[0]

    # --- frame plumbing ----------------------------------------------------
    @property
    def active(self) -> Frame | None:
        return self.stack[-1] if self.stack else None

    @property
    def root(self) -> Frame | None:
        return self.stack[0] if self.stack else None

    def _new_frame(self, key, t: float, parent_id=None, reason="") -> Frame:
        f = Frame(id=self._next_id, key=key, name=self._names.get(key, "") or "",
                  started_at=t, last_evidence_at=t, parent_id=parent_id,
                  switch_reason=reason)
        self._next_id += 1
        return f

    def _close(self, f: Frame, t: float, reason: str) -> FrameEvent:
        f.state = "closed"
        f.ended_at = t
        f.switch_reason = reason
        if f in self.stack:
            self.stack.remove(f)
        if f in self.suspended:
            self.suspended.remove(f)
        self.closed.append(f)
        # Orphan check. A child nests INSIDE its parent, so a parent that ends
        # while a child is still open leaves a frame whose events belong to a
        # stretch that is already over — and an episode built from it reports
        # a hundred events inside an eighteen-minute span. Descendants that are
        # still running are promoted to roots rather than closed: the work did
        # not stop, it just stopped being an interruption of anything.
        for other in list(self.stack) + list(self.suspended):
            if other.parent_id == f.id:
                other.parent_id = None
                if not other.switch_reason:
                    other.switch_reason = "orphaned"
        return FrameEvent("close", f, t, reason)

    def _close_stack(self, t: float, reason: str) -> list[FrameEvent]:
        return [self._close(f, t, reason) for f in list(reversed(self.stack))]

    # --- the stream --------------------------------------------------------
    def feed(self, ev: StreamEvent) -> Placement:
        out: list[FrameEvent] = []
        dt = 0.0 if self._t is None else max(0.0, ev.t - self._t)

        # 1. gap handling BEFORE decay: a long silence is a lifecycle question,
        #    not an evidence question.
        if self.stack and dt >= self.idle_suspend_s:
            root = self.root
            if dt >= self.freeze_s:
                out += self._close_stack(self._t, "idle_gap")
                self._evidence.clear()
            else:
                for f in list(reversed(self.stack)):
                    f.state = "suspended"
                    self.suspended.append(f)
                    self.stack.remove(f)
                out.append(FrameEvent("suspend", root, self._t, "idle_gap"))
        for f in list(self.suspended):
            if ev.t - f.last_evidence_at >= SUSPEND_EXPIRY_S:
                out.append(self._close(f, f.last_evidence_at, "suspend_expiry"))

        self._decay(dt)
        self._observe(ev)
        self._t = ev.t
        dom = self._dominant()

        # 2. a suspended frame whose anchor just came back is the SAME frame.
        #    Walking away for forty minutes and returning to the same work must
        #    continue the episode, not start a new one.
        if dom is not None:
            for f in list(self.suspended):
                if f.key == dom:
                    f.state = "active"
                    f.last_evidence_at = ev.t
                    self.suspended.remove(f)
                    # Coming back to an excursion whose parent did not come
                    # back with it makes it the work, not an interruption.
                    if f.parent_id is not None and not any(
                            x.id == f.parent_id for x in self.stack):
                        f.parent_id = None
                    self.stack.append(f)
                    out.append(FrameEvent("resume", f, ev.t, "anchor_returned"))
                    self._pending = None
                    break

        # 3. nothing open yet.
        if not self.stack:
            f = self._new_frame(dom, ev.t, reason="first_evidence")
            self.stack.append(f)
            out.append(FrameEvent("open", f, ev.t, "first_evidence"))
            self._pending = None
            return self._land(ev, out)

        cur = self.active
        # 4. an unbound frame is a placeholder: the first real anchor adopts it
        #    rather than opening a second frame beside it.
        if cur.key is None and dom is not None:
            cur.key = dom
            cur.name = self._names.get(dom, "")
            cur.last_evidence_at = ev.t
            return self._land(ev, out)

        # 5. competitor logic.
        if dom is not None and cur.key is not None:
            stack_keys = [f.key for f in self.stack]
            if dom in stack_keys:
                # The excursion is over. Collapse back to the frame that owns
                # this anchor instead of nesting deeper — otherwise returning
                # to what you were doing opens a THIRD frame, and a morning of
                # glancing at mail builds a tower rather than a timeline.
                while self.active is not None and self.active.key != dom:
                    out.append(self._close(self.active, ev.t, "excursion_ended"))
                self._pending = None
            else:
                if self._pending is None or self._pending["key"] != dom:
                    self._pending = {"key": dom, "since": ev.t}
                dwell = ev.t - self._pending["since"]
                root = self.root
                if (self._share(dom) >= self.theta_in
                        and self._share(root.key) <= self.theta_out
                        and dwell >= self.switch_dwell_s):
                    out += self._close_stack(ev.t, "switch")
                    f = self._new_frame(dom, ev.t, reason="switch")
                    self.stack.append(f)
                    out.append(FrameEvent("switch", f, ev.t, "dwell_exceeded"))
                    self._pending = None
                    return self._land(ev, out)
                if dwell <= self.interrupt_max_s and len(self.stack) < STACK_DEPTH:
                    # Provisionally an interruption: nest it, keep the parent
                    # alive, and above all DO NOT cut the episode. If this turns
                    # out to be a real move, the promotion below catches it.
                    child = self._new_frame(dom, ev.t, parent_id=cur.id,
                                            reason="interrupt")
                    self.stack.append(child)
                    out.append(FrameEvent("interrupt", child, ev.t, "excursion"))
                    return self._land(ev, out)

        # 5b. an "interruption" that never ended was a switch all along.
        # Without this a child frame absorbs the rest of the day: once it is
        # the active frame its own anchor is no longer a competitor, so the
        # test above can never fire again.
        cur = self.active
        if cur is not None and cur.parent_id is not None and self.root is not None:
            if (ev.t - cur.started_at > self.interrupt_max_s
                    and self._share(self.root.key) <= self.theta_out
                    and self._share(cur.key) >= self.theta_in):
                # PROMOTE the child in place rather than opening a replacement.
                # Re-creating it would start the new frame before its parent
                # closed (overlapping episodes on a timeline) and would strand
                # the excursion's own events under the frame it interrupted.
                # The parent ends when the excursion began, which is also the
                # truthful answer: that is when the user stopped.
                for f in [x for x in reversed(self.stack) if x is not cur]:
                    out.append(self._close(f, cur.started_at, "switch"))
                cur.parent_id = None
                cur.switch_reason = "excursion_became_switch"
                out.append(FrameEvent("switch", cur, ev.t,
                                      "excursion_became_switch"))
                self._pending = None
                return self._land(ev, out)

        # 6. defensive forced split.
        root = self.root
        if root is not None and ev.t - root.started_at >= self.max_frame_s:
            out += self._close_stack(ev.t, "max_duration")
            f = self._new_frame(dom, ev.t, reason="max_duration")
            self.stack.append(f)
            out.append(FrameEvent("open", f, ev.t, "max_duration"))
        return self._land(ev, out)

    def _land(self, ev: StreamEvent, out: list[FrameEvent]) -> Placement:
        """Attach the event to the innermost active frame.

        An event with no anchor of its own INHERITS — which is the whole point.
        A click carries no identifier and never will; it belongs to whatever the
        user was doing, and demanding that each event resolve on its own is what
        made per-event coverage a malformed measure in the first place.
        """
        f = self.active
        if f is None:
            return Placement(ev.event_id, None, False, tuple(out))
        f.n_events += 1
        own = bool(ev.anchors) and any(a.key == f.key for a in ev.anchors)
        if own:
            f.n_anchored += 1
            f.last_evidence_at = ev.t
        if ev.app:
            f.apps[ev.app] = f.apps.get(ev.app, 0) + 1
        return Placement(ev.event_id, f.id, not own, tuple(out))

    def close(self, t: float | None = None) -> list[FrameEvent]:
        """End of stream. Everything still open closes at the last evidence."""
        end = t if t is not None else (self._t or 0.0)
        out = self._close_stack(end, "stream_end")
        for f in list(self.suspended):
            out.append(self._close(f, f.last_evidence_at, "stream_end"))
        return out

    @property
    def frames(self) -> list[Frame]:
        """Every frame this stream produced, in start order."""
        return sorted(self.closed + self.stack + self.suspended,
                      key=lambda f: (f.started_at, f.id))


__all__ = ["Anchor", "StreamEvent", "Frame", "FrameEvent", "Placement",
           "Segmenter", "THETA_IN", "THETA_OUT", "SWITCH_DWELL_S",
           "INTERRUPT_MAX_S", "IDLE_SUSPEND_S", "FREEZE_S", "MAX_FRAME_S"]
