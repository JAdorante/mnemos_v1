"""Linux mic + camera capture helpers (hermetic — no real hardware)."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services import audio as audio_mod
from app.services import camera


class _FakeCap:
    def __init__(self, opened: bool) -> None:
        self._opened = opened
        self.released = False

    def isOpened(self) -> bool:
        return self._opened

    def release(self) -> None:
        self.released = True

    def set(self, *args, **kwargs) -> bool:
        return True


class _FakeCv2:
    CAP_ANY = 0
    CAP_V4L2 = 200
    CAP_GSTREAMER = 180
    CAP_DSHOW = 700
    CAP_MSMF = 1400
    CAP_V4L = 200

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []
        self.open_ids: set[int] = set()

    def VideoCapture(self, index, backend):
        self.calls.append((int(index), int(backend)))
        return _FakeCap(int(backend) in self.open_ids)


class CameraBackendTests(unittest.TestCase):
    def test_linux_default_is_v4l2(self) -> None:
        self.assertEqual(camera.default_capture_backend(platform="linux",
                                                        os_name="posix"),
                         "v4l2")

    def test_windows_default_is_dshow(self) -> None:
        self.assertEqual(camera.default_capture_backend(platform="win32",
                                                        os_name="nt"),
                         "dshow")

    def test_macos_default_is_any(self) -> None:
        self.assertEqual(camera.default_capture_backend(platform="darwin",
                                                        os_name="posix"),
                         "any")

    def test_linux_hint(self) -> None:
        self.assertEqual(camera.camera_backend_hint(platform="linux",
                                                    os_name="posix"),
                         "v4l2|gstreamer|any")

    def test_diag_skips_zero_ids(self) -> None:
        cv2 = _FakeCv2()
        cv2.CAP_GSTREAMER = 0
        names = [n for n, _ in camera.platform_diag_backends(
            cv2, platform="linux", os_name="posix")]
        self.assertEqual(names, ["v4l2", "any"])

    def test_open_requests_v4l2_then_any(self) -> None:
        cv2 = _FakeCv2()
        cv2.open_ids = {cv2.CAP_ANY}
        cap = camera.open_camera(cv2, 0, "v4l2", platform="linux")
        self.assertTrue(cap.isOpened())
        self.assertEqual(cv2.calls, [(0, cv2.CAP_V4L2), (0, cv2.CAP_ANY)])

    def test_open_v4l2_succeeds_without_any(self) -> None:
        cv2 = _FakeCv2()
        cv2.open_ids = {cv2.CAP_V4L2}
        cap = camera.open_camera(cv2, 2, "v4l2", platform="linux")
        self.assertTrue(cap.isOpened())
        self.assertEqual(cv2.calls, [(2, cv2.CAP_V4L2)])


class AudioDeviceResolverTests(unittest.TestCase):
    DEVICES = [
        {"name": "Dummy Output", "max_input_channels": 0},
        {"name": "USB Mic Array", "max_input_channels": 1},
        {"name": "Built-in Audio Analog Stereo", "max_input_channels": 2},
    ]

    def test_empty_is_default(self) -> None:
        self.assertIsNone(audio_mod.resolve_input_device("", self.DEVICES))
        self.assertIsNone(audio_mod.resolve_input_device("  ", self.DEVICES))

    def test_index(self) -> None:
        self.assertEqual(audio_mod.resolve_input_device("1", self.DEVICES), 1)

    def test_substring(self) -> None:
        self.assertEqual(audio_mod.resolve_input_device("usb", self.DEVICES), 1)
        self.assertEqual(
            audio_mod.resolve_input_device("built-in", self.DEVICES), 2)

    def test_unknown_name_raises(self) -> None:
        with self.assertRaises(ValueError):
            audio_mod.resolve_input_device("no-such-mic", self.DEVICES)

    def test_first_input_skips_output_only(self) -> None:
        self.assertEqual(audio_mod.first_input_index(self.DEVICES), 1)


class VisionPipelineOpenTests(unittest.TestCase):
    def test_open_capture_uses_shared_helper(self) -> None:
        from app.services.vision import VisionPipeline

        cv2 = _FakeCv2()
        cv2.open_ids = {cv2.CAP_ANY}
        pipe = VisionPipeline.__new__(VisionPipeline)
        pipe.cfg = type("C", (), {"camera_index": 0, "capture_backend": "v4l2",
                                  "capture_fourcc": "", "capture_width": 0,
                                  "capture_height": 0})()
        with patch("app.services.vision.open_camera",
                   wraps=lambda c, i, b: camera.open_camera(
                       c, i, b, platform="linux")):
            cap = pipe._open_capture(cv2)
        self.assertTrue(cap.isOpened())
        self.assertEqual(cv2.calls[0], (0, cv2.CAP_V4L2))
        self.assertEqual(cv2.calls[-1], (0, cv2.CAP_ANY))
