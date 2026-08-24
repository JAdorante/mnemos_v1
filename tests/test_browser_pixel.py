"""Pixel fallback for graphics surfaces (canvas games/maps/editors/players).

A page can be fully visible and still expose nothing to the accessibility tree:
everything real is drawn into a <canvas>. These cover the whole path — detecting
such a surface, describing it to the model, offering the coordinate actions only
there, mapping screenshot pixels back to CSS pixels, confining clicks/drags to
the surface, and verifying a move that changes pixels but no DOM.
"""
from __future__ import annotations

import struct
import unittest
from unittest import mock

from browser_agent import config as bcfg
from browser_agent.browser import BrowserDriver
from browser_agent.perception import render_observation, signature
from browser_agent.surfaces import inside_surface, pixel_surface, wants_pixel_ui
from browser_agent.tools import ACTION_TOOLS, PIXEL_ACTIONS, PIXEL_TOOLS

VIEW = {"w": 1280, "h": 800}


def _scan(surfaces, elements=None, **extra):
    s = {"url": "https://example.com/", "title": "t", "count": len(elements or []),
         "elements": elements or [], "viewport": VIEW, "surfaces": surfaces,
         "dpr": 1, "scrollY": 0, "scrollMax": 0}
    s.update(extra)
    return s


def _canvas(w=1024, h=680, x=128, y=64, inner=0, kind="canvas", label=""):
    return {"kind": kind, "x": x, "y": y, "w": w, "h": h, "label": label,
            "inner": inner}


class SurfaceDetectionTests(unittest.TestCase):
    def test_dominant_canvas_is_a_pixel_surface(self):
        surf = pixel_surface(_scan([_canvas()]))
        self.assertIsNotNone(surf)
        assert surf is not None
        self.assertEqual(surf["kind"], "canvas")
        self.assertEqual((surf["x"], surf["y"], surf["w"], surf["h"]),
                         (128, 64, 1024, 680))

    def test_small_decorative_canvas_is_not(self):
        # A sparkline/avatar canvas must not switch the agent to coordinates.
        self.assertIsNone(pixel_surface(_scan([_canvas(w=200, h=120, x=0, y=0)])))
        self.assertFalse(wants_pixel_ui(_scan([_canvas(w=200, h=120)])))

    def test_surface_with_interactive_children_stays_dom_driven(self):
        # An <object>/role=application that does expose real controls is
        # describable — element_ids beat guessing at pixels.
        self.assertIsNone(pixel_surface(_scan([_canvas(inner=7, kind="object")])))

    def test_largest_surface_wins(self):
        scan = _scan([_canvas(w=700, h=700, x=0, y=0),
                      _canvas(w=1200, h=700, x=40, y=40)])
        surf = pixel_surface(scan)
        assert surf is not None
        self.assertEqual(surf["w"], 1200)

    def test_video_player_counts(self):
        surf = pixel_surface(_scan([_canvas(kind="video", label="Trailer")]))
        assert surf is not None
        self.assertEqual(surf["kind"], "video")
        self.assertEqual(surf["label"], "Trailer")

    def test_ordinary_page_has_no_surface(self):
        self.assertIsNone(pixel_surface(_scan([])))
        self.assertIsNone(pixel_surface({}))
        self.assertIsNone(pixel_surface(None))

    def test_inside_surface_bounds(self):
        surf = pixel_surface(_scan([_canvas()]))
        self.assertTrue(inside_surface(surf, 200, 200))
        self.assertFalse(inside_surface(surf, 20, 20))       # page chrome, left
        self.assertFalse(inside_surface(surf, 640, 780))     # below the canvas
        self.assertTrue(inside_surface(surf, 124, 200, pad=8))  # fuzzy edge
        self.assertFalse(inside_surface(None, 200, 200))


class ObservationTests(unittest.TestCase):
    def test_canvas_page_is_described_not_reported_empty(self):
        # The failure this fixes: a menu-only element list on a canvas game read
        # as "there is nothing here I can act on".
        obs = render_observation(_scan(
            [_canvas(label="game")],
            [{"id": 0, "role": "link", "name": "Spider"}]))
        self.assertIn("Graphics surface", obs)
        self.assertIn("canvas", obs)
        self.assertIn("click_at", obs)
        self.assertIn("pixels, not elements", obs)

    def test_ordinary_page_observation_unchanged(self):
        obs = render_observation(_scan([], [{"id": 0, "role": "button",
                                             "name": "Go"}]))
        self.assertNotIn("Graphics surface", obs)
        self.assertIn("[0] button: Go", obs)


class SignatureTests(unittest.TestCase):
    def test_pixel_hash_distinguishes_frames_with_a_frozen_dom(self):
        a = signature(_scan([_canvas()], pixel_hash="aaaa1111"))
        b = signature(_scan([_canvas()], pixel_hash="bbbb2222"))
        self.assertEqual(a["content_hash"], b["content_hash"])  # DOM identical
        self.assertNotEqual(a, b)                               # move still seen

    def test_absent_on_ordinary_pages(self):
        self.assertNotIn("pixel_hash", signature(_scan([])))


class ToolGatingTests(unittest.TestCase):
    def test_pixel_tools_are_not_part_of_the_default_vocabulary(self):
        self.assertFalse(PIXEL_ACTIONS & {t["name"] for t in ACTION_TOOLS})
        self.assertEqual(PIXEL_ACTIONS, {"click_at", "drag", "press_key"})

    def _fake_llm(self):
        from browser_agent.llm import LLM

        calls = []

        class _Block:
            type = "tool_use"
            name = "click_at"
            input = {"x": 1, "y": 2}

        class _Resp:
            content = [_Block()]
            usage = None

        class _Messages:
            def create(self, **kw):
                calls.append(kw)
                return _Resp()

        llm = LLM.__new__(LLM)
        llm.client = mock.Mock(messages=_Messages())
        llm.usage = {}
        llm.cloud_ok = True
        return llm, calls

    def test_pixel_tools_offered_only_when_asked_for(self):
        llm, calls = self._fake_llm()
        llm.choose_action("obs", pixel=False)
        llm.choose_action("obs", pixel=True)
        plain = {t["name"] for t in calls[0]["tools"]}
        pixel = {t["name"] for t in calls[1]["tools"]}
        self.assertFalse(PIXEL_ACTIONS & plain)
        self.assertTrue(PIXEL_ACTIONS <= pixel)
        self.assertTrue({t["name"] for t in ACTION_TOOLS} <= pixel)


def _png(w: int, h: int) -> bytes:
    """Minimal PNG header — enough for _png_size / _fit_for_grounding."""
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", w, h)


class _FakePage:
    def __init__(self, inner_width=1280):
        self.mouse = mock.Mock()
        self.keyboard = mock.Mock()
        self.inner_width = inner_width

    def evaluate(self, *_a, **_k):
        return self.inner_width

    def wait_for_timeout(self, _ms):
        pass


class PixelActionTests(unittest.TestCase):
    def _driver(self, scale=1.0):
        d = BrowserDriver(headless=True)
        d.page = _FakePage()
        d._ghost = "off"
        d.pixel_surface = pixel_surface(_scan([_canvas()]))
        d.pixel_scale = scale
        d.shot_size = (1280, 800)
        return d

    def test_click_inside_the_surface(self):
        d = self._driver()
        res = d.execute("click_at", {"x": 400, "y": 300})
        self.assertTrue(res["ok"], res)
        d.page.mouse.click.assert_called_once()
        args, kwargs = d.page.mouse.click.call_args
        self.assertEqual((round(args[0]), round(args[1])), (400, 300))
        self.assertEqual(kwargs["click_count"], 1)

    def test_double_click(self):
        d = self._driver()
        d.execute("click_at", {"x": 400, "y": 300, "clicks": 2})
        self.assertEqual(d.page.mouse.click.call_args[1]["click_count"], 2)

    def test_click_outside_the_surface_is_refused_with_the_bounds(self):
        # Coordinates may only touch what the DOM can't describe; page chrome
        # keeps its element_ids, so a misread screenshot can't hit "Buy".
        d = self._driver()
        res = d.execute("click_at", {"x": 20, "y": 20})
        self.assertFalse(res["ok"])
        self.assertIn("outside the graphics surface", res["detail"])
        self.assertIn("element_id", res["detail"])
        d.page.mouse.click.assert_not_called()

    def test_screenshot_coordinates_are_scaled_to_css_pixels(self):
        # HiDPI / downscaled grounding shot: the model measures on the image,
        # the browser clicks in CSS pixels.
        d = self._driver(scale=2.0)
        d.execute("click_at", {"x": 200, "y": 150})
        args, _ = d.page.mouse.click.call_args
        self.assertEqual((round(args[0]), round(args[1])), (400, 300))

    def test_packed_coordinate_string_is_tolerated(self):
        d = self._driver()
        res = d.execute("click_at", {"x": "400, 300", "y": "300"})
        self.assertTrue(res["ok"], res)

    def test_drag_presses_moves_in_steps_and_releases(self):
        d = self._driver()
        res = d.execute("drag", {"from_x": 300, "from_y": 200,
                                 "to_x": 700, "to_y": 500})
        self.assertTrue(res["ok"], res)
        d.page.mouse.down.assert_called_once()
        d.page.mouse.up.assert_called_once()
        moves = d.page.mouse.move.call_args_list
        self.assertGreater(len(moves), 3)   # a single jump reads as a click
        self.assertEqual(tuple(round(v) for v in moves[0][0]), (300, 200))
        self.assertEqual(tuple(round(v) for v in moves[-1][0]), (700, 500))

    def test_drag_ending_outside_the_surface_is_refused(self):
        d = self._driver()
        res = d.execute("drag", {"from_x": 300, "from_y": 200,
                                 "to_x": 700, "to_y": 790})
        self.assertFalse(res["ok"])
        d.page.mouse.down.assert_not_called()

    def test_press_key_normalizes_model_shorthand(self):
        d = self._driver()
        self.assertTrue(d.execute("press_key", {"key": "ctrl+z"})["ok"])
        d.page.keyboard.press.assert_called_with("Control+z")
        d.execute("press_key", {"key": "esc"})
        d.page.keyboard.press.assert_called_with("Escape")
        d.execute("press_key", {"key": "space"})
        d.page.keyboard.press.assert_called_with("Space")
        d.execute("press_key", {"key": "arrowleft"})
        d.page.keyboard.press.assert_called_with("ArrowLeft")
        d.execute("press_key", {"key": "left"})
        d.page.keyboard.press.assert_called_with("ArrowLeft")

    def test_press_key_needs_no_surface(self):
        d = self._driver()
        d.pixel_surface = None
        self.assertTrue(d.execute("press_key", {"key": "enter"})["ok"])

    def test_pixel_actions_refused_when_no_surface_was_scanned(self):
        d = self._driver()
        d.pixel_surface = None
        res = d.execute("click_at", {"x": 400, "y": 300})
        self.assertFalse(res["ok"])
        self.assertIn("element_id", res["detail"])

    def test_disabled_by_config(self):
        d = self._driver()
        with mock.patch.object(bcfg, "BROWSER_PIXEL", False):
            res = d.execute("click_at", {"x": 400, "y": 300})
        self.assertFalse(res["ok"])
        self.assertIn("disabled", res["detail"])


class GroundingShotTests(unittest.TestCase):
    def test_png_size_from_header(self):
        self.assertEqual(BrowserDriver._png_size(_png(1280, 800)), (1280, 800))
        self.assertEqual(BrowserDriver._png_size(b"not a png"), (0, 0))

    def test_same_size_shot_maps_one_to_one(self):
        d = BrowserDriver(headless=True)
        d.page = _FakePage(inner_width=1280)
        d._fit_for_grounding(_png(1280, 800))
        self.assertEqual(d.shot_size, (1280, 800))
        self.assertAlmostEqual(d.pixel_scale, 1.0)

    def test_hidpi_shot_scales_back_to_css_pixels(self):
        # A 2x display screenshots 2560px wide for a 1280px viewport. Without
        # Pillow it stays that size; with it, it is fitted under the vision
        # resize cap. Either way the recorded scale maps image -> CSS.
        d = BrowserDriver(headless=True)
        d.page = _FakePage(inner_width=1280)
        d._fit_for_grounding(_png(2560, 1600))
        img_w = d.shot_size[0]
        self.assertLessEqual(img_w, max(2560, bcfg.PIXEL_SHOT_MAX_EDGE))
        self.assertAlmostEqual(d.pixel_scale, 1280 / img_w, places=4)
        self.assertAlmostEqual(img_w * d.pixel_scale, 1280, places=2)


class BlankPageTests(unittest.TestCase):
    def test_canvas_only_page_is_not_treated_as_still_rendering(self):
        # Otherwise every scan burns three render retries waiting for DOM that
        # a canvas game will never produce.
        self.assertTrue(BrowserDriver._looks_blank({"count": 0}))
        self.assertFalse(BrowserDriver._looks_blank(
            {"count": 0, "surfaces": [_canvas()]}))


# --- end-to-end against a real canvas page ---------------------------------
_CANVAS_PAGE = """
<body style="margin:0">
<a href="#s">Spider</a> <a href="#m">Mahjong</a>
<canvas id="board" width="1240" height="720" style="display:block"></canvas>
<script>
const c = document.getElementById('board'), g = c.getContext('2d');
window.log = [];
let cardX = 100, cardY = 100, dragging = false;
function draw() {
  g.fillStyle = '#060'; g.fillRect(0, 0, c.width, c.height);
  g.fillStyle = '#fff'; g.fillRect(cardX, cardY, 80, 110);
}
draw();
c.addEventListener('mousedown', e => {
  dragging = true; window.log.push('down:' + Math.round(e.offsetX)); });
c.addEventListener('mousemove', e => {
  if (dragging) { cardX = e.offsetX - 40; cardY = e.offsetY - 55; draw(); } });
c.addEventListener('mouseup', e => {
  dragging = false; window.log.push('up:' + Math.round(e.offsetX)); });
window.addEventListener('keydown', e => window.log.push('key:' + e.key));
</script></body>
"""


class RealCanvasPageTests(unittest.TestCase):
    """The whole path against a live headless Chromium: a canvas game whose only
    DOM is two menu links — the shape that used to end in "I can see the board
    but I can't move anything"."""

    @classmethod
    def setUpClass(cls):
        from browser_agent.browser import BrowserDriver as _BD

        cls._patch = mock.patch.object(bcfg, "GHOST_MODE", "off")
        cls._patch.start()
        try:
            cls.d = _BD(headless=True)
            cls.d.start()
        except Exception as e:   # no browser binary in this environment
            cls._patch.stop()
            raise unittest.SkipTest(f"chromium unavailable: {e}")
        cls.d.page.set_content(_CANVAS_PAGE)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.d.close()
        finally:
            cls._patch.stop()

    def test_scan_reports_the_surface_and_a_frame_hash(self):
        s = self.d.scan()
        self.assertEqual(s["count"], 2)          # only the two menu links
        self.assertTrue(s["surfaces"])
        self.assertEqual(s["surfaces"][0]["kind"], "canvas")
        self.assertTrue(s.get("pixel_hash"))
        self.assertIsNotNone(self.d.pixel_surface)
        self.assertIn("Graphics surface", render_observation(s))

    def test_click_and_drag_reach_the_canvas_and_move_the_pixels(self):
        before = signature(self.d.scan())
        self.d.page.evaluate("() => { window.log = []; }")
        self.assertTrue(self.d.execute(
            "click_at", {"x": 140, "y": 150})["ok"])
        self.assertTrue(self.d.execute(
            "drag", {"from_x": 140, "from_y": 150,
                     "to_x": 700, "to_y": 500})["ok"])
        self.assertTrue(self.d.execute("press_key", {"key": "ctrl+z"})["ok"])
        log = self.d.page.evaluate("() => window.log")
        self.assertTrue(any(e.startswith("down:") for e in log), log)
        self.assertTrue(any(e.startswith("up:700") for e in log), log)
        self.assertIn("key:z", log)
        after = signature(self.d.scan())
        # The DOM never moved; the rendered frame did — which is exactly the
        # evidence the step verifier now uses for a pixel action.
        self.assertEqual(before["content_hash"], after["content_hash"])
        self.assertNotEqual(before.get("pixel_hash"), after.get("pixel_hash"))

    def test_click_on_the_page_chrome_is_refused(self):
        self.d.scan()
        res = self.d.execute("click_at", {"x": 4, "y": 4})   # the menu links
        self.assertFalse(res["ok"])
        self.assertIn("outside the graphics surface", res["detail"])


_FRAME_HOST = """<body style="margin:0"><h1>Free Solitaire</h1>
<iframe src="/game" style="width:1240px;height:700px;border:0"></iframe></body>"""

_FRAME_GAME = """<body style="margin:0">
<canvas id="g" width="1200" height="680"></canvas>
<script>
const c = document.getElementById('g'), x = c.getContext('2d');
window.log = [];
x.fillStyle = '#080'; x.fillRect(0, 0, c.width, c.height);
c.addEventListener('mousedown', e => {
  window.log.push('down:' + Math.round(e.offsetX) + ',' + Math.round(e.offsetY));
  x.fillStyle = '#fff'; x.fillRect(e.offsetX, e.offsetY, 40, 40);
});
</script></body>"""


class EmbeddedCanvasTests(unittest.TestCase):
    """The other common shape: the game is served inside a frame. A DOM scan
    never crosses a frame boundary, but mouse coordinates do."""

    def setUp(self):
        self._patch = mock.patch.object(bcfg, "GHOST_MODE", "off")
        self._patch.start()
        try:
            self.d = BrowserDriver(headless=True)
            self.d.start()
        except Exception as e:
            self._patch.stop()
            raise unittest.SkipTest(f"chromium unavailable: {e}")
        self.d.page.route("**/game", lambda r: r.fulfill(
            status=200, content_type="text/html", body=_FRAME_GAME))
        self.d.page.route("**/host", lambda r: r.fulfill(
            status=200, content_type="text/html", body=_FRAME_HOST))
        self.d.page.goto("https://example.test/host")

    def tearDown(self):
        try:
            self.d.close()
        finally:
            self._patch.stop()

    def test_surface_inside_a_same_origin_frame_is_found_and_clickable(self):
        scan = self.d.scan()
        surfaces = scan.get("surfaces") or []
        self.assertTrue(surfaces, "canvas inside the frame was not seen")
        self.assertEqual(surfaces[0]["kind"], "canvas")
        # Offset by the frame's position on the page, not the frame's own origin.
        self.assertGreater(surfaces[0]["y"], 0)
        self.d.screenshot_bytes()
        self.assertTrue(self.d.execute("click_at", {"x": 300, "y": 300})["ok"])
        log = self.d.page.frames[1].evaluate("() => window.log")
        self.assertTrue(log and log[0].startswith("down:300,"), log)


if __name__ == "__main__":
    unittest.main()
