"""Notebook understanding — read a page, decide what it is, act on it.

Works on any still image (a phone photo), sidestepping the webcam entirely. This
is the richer version of the vision to-do watcher: not just to-do lists, but any
handwritten page — a task, a message to send, or notes — deciphered into
structured, correctly-routed actions:

    "Task — Text Conor Kane 'I got the software to work … — Justin'"
        -> kind=text_message, recipient='Conor Kane', body='I got …',
           surface=phone_link, risk=high (gated before sending)

    a page of notes
        -> compartmentalized summary + suggested next steps

Two stages:
  1. READ   — a VLM transcribes the page (app.services.vlm). Given raw text
              (e.g. a photo already transcribed), this stage is skipped.
  2. DECIDE — an LLM turns the reading into structured actions, each routed to a
              surface (phone_link / browser / none) and risk-classified. Actions
              are NEVER executed here; the caller surfaces them for approval.

Reuses the Personal Agent Layer's LLM handle + risk table (agent_planner) so the
whole system shares one router and one risk policy.
"""
from __future__ import annotations

from app.services.agent_planner import _llm, _draft_model, classify_risk

_ACTION_SYSTEM = (
    "You are vinceo.ai reading a page from the user's own notebook. Transcribe the "
    "intent faithfully, decide what each line IS, and for anything actionable "
    "produce a clean structured action. Rules: never invent a recipient, body, "
    "or fact not on the page; keep quoted message bodies VERBATIM (fix only "
    "obvious spelling); a line beginning 'Task -' or an imperative is actionable; "
    "plain observations are notes (actionable=false). For \"Text <name> '<msg>'\" "
    "set kind=text_message, recipient=<name>, body=<msg>."
)

_ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "page_type": {
            "type": "string",
            "enum": ["task", "message", "todo_list", "notes", "mixed", "other"],
            "description": "what the page mostly is",
        },
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["text_message", "email", "call", "web_task",
                                 "reminder", "note"],
                        "description": "what to do with this line",
                    },
                    "recipient": {"type": "string",
                                  "description": "person/number, if any"},
                    "subject": {"type": "string", "description": "email subject, if any"},
                    "body": {"type": "string",
                             "description": "the verbatim message/content to send or record"},
                    "goal": {"type": "string",
                             "description": "one-line imperative goal for the agent"},
                    "actionable": {"type": "boolean",
                                   "description": "true = do it now; false = just remember"},
                },
                "required": ["kind", "goal", "actionable"],
            },
        },
        "notes_summary": {
            "type": "string",
            "description": "if the page has notes, a compartmentalized summary + "
            "suggested next steps grounded only in the page",
        },
    },
    "required": ["page_type", "actions"],
}

# kind -> execution surface (which "hands" run it). none = record only, no agent.
_SURFACE = {"text_message": "phone_link", "call": "phone_link",
            "email": "browser", "web_task": "browser",
            "reminder": "none", "note": "none"}
# kinds that COMMIT something externally -> treated as a 'send' for risk (gated).
_SENDING = {"text_message", "email", "call"}


def extract_actions(text: str) -> dict:
    """Stage 2: reading -> structured actions. Best-effort; needs an LLM."""
    llm = _llm()
    if llm is None:
        return {"page_type": "unknown", "actions": [],
                "error": "no LLM available"}
    user = f"NOTEBOOK PAGE (transcribed):\n{text}\n\nExtract the structured actions."
    return llm._json_call(_draft_model(), _ACTION_SYSTEM, user, _ACTION_SCHEMA) or {}


def _route(action: dict) -> dict:
    """Assign a surface + risk to a structured action (no execution)."""
    kind = action.get("kind", "note")
    surface = _SURFACE.get(kind, "none")
    risk_kind = "send" if kind in _SENDING else "read"
    risk, approval = classify_risk(risk_kind, goal=action.get("goal", ""))
    return {**action, "surface": surface, "risk_level": risk,
            "approval_required": bool(action.get("actionable")) and approval}


# --- page preprocessing: find the page, flatten/crop to it, upscale ----------
# A page held to a webcam fills a fraction of the frame, so its writing gets few
# pixels — the main cause of OCR errors. Cropping to just the page (and upscaling)
# gives the transcriber real resolution to work with.
def _order_pts(pts):
    import numpy as np
    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    rect[0], rect[2] = pts[np.argmin(s)], pts[np.argmax(s)]      # tl, br
    d = np.diff(pts, axis=1)
    rect[1], rect[3] = pts[np.argmin(d)], pts[np.argmax(d)]      # tr, bl
    return rect


def _find_page_quad(img):
    """Largest 4-sided bright contour that plausibly is the page, else None."""
    import cv2
    h, w = img.shape[:2]
    gray = cv2.GaussianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    edged = cv2.dilate(cv2.Canny(gray, 40, 120), None, iterations=2)
    cnts, _ = cv2.findContours(edged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for c in sorted(cnts, key=cv2.contourArea, reverse=True)[:5]:
        approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
        area = cv2.contourArea(approx)
        if len(approx) == 4 and 0.12 * w * h < area < 0.98 * w * h:
            return approx.reshape(4, 2).astype("float32")
    return None


def _warp(img, quad):
    import cv2
    import numpy as np
    rect = _order_pts(quad)
    tl, tr, br, bl = rect
    W = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
    H = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
    if W < 20 or H < 20:
        return img
    dst = np.array([[0, 0], [W - 1, 0], [W - 1, H - 1], [0, H - 1]], dtype="float32")
    return cv2.warpPerspective(img, cv2.getPerspectiveTransform(rect, dst), (W, H))


def _upscale(img, target_w=1600):
    import cv2
    h, w = img.shape[:2]
    if w >= target_w:
        return img
    return cv2.resize(img, (target_w, int(h * target_w / w)),
                      interpolation=cv2.INTER_CUBIC)


def preprocess_page(jpeg_bytes: bytes) -> bytes:
    """Crop/flatten to the page and upscale. Returns the original bytes if no
    page is confidently found (better a full frame than a wrong crop)."""
    try:
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(jpeg_bytes, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return jpeg_bytes
        quad = _find_page_quad(img)
        page = _warp(img, quad) if quad is not None else img
        page = _upscale(page, 1600)
        ok, buf = cv2.imencode(".jpg", page, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        return buf.tobytes() if ok else jpeg_bytes
    except Exception:
        return jpeg_bytes


def process_notebook(*, image_bytes: bytes | None = None,
                     text: str | None = None) -> dict:
    """Read a notebook page (image or pre-transcribed text) and return the
    structured, routed actions — WITHOUT executing them. For an image: crop to
    the page, then run a verbatim OCR pass (vlm.transcribe) before extraction."""
    read = text or ""
    vision = None
    pre_jpg = None
    if image_bytes is not None:
        try:
            from app.services.vlm import vlm
            pre_jpg = preprocess_page(image_bytes)
            read = vlm.transcribe(pre_jpg) or ""
            vision = {"provider": "claude", "mode": "transcribe",
                      "preprocessed": pre_jpg is not None}
        except Exception as exc:
            return {"read_text": read, "error": f"VLM read failed: {exc}",
                    "actions": [], "preprocessed_jpg": pre_jpg}

    extracted = extract_actions(read)
    actions = [_route(a) for a in extracted.get("actions", [])]
    return {
        "read_text": read,
        "vision": vision,
        "preprocessed_jpg": pre_jpg,
        "page_type": extracted.get("page_type"),
        "notes_summary": extracted.get("notes_summary"),
        "actions": actions,
    }


def offer_text(action: dict) -> str:
    """Render the chat offer for one actionable item (what the user would see)."""
    kind = action.get("kind", "task")
    goal = action.get("goal", "")
    where = {"phone_link": " via Phone Link", "browser": " in the browser"}.get(
        action.get("surface", ""), "")
    gate = (" I'll pause for your approval before sending."
            if action.get("approval_required") else "")
    return (f"I read a {kind.replace('_', ' ')} from your notebook: “{goal}”. "
            f"Want me to do it{where}?{gate} (yes/no)")
