import io
import os
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from xiaozhi.service import ui_render


class ImuPanelRenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        candidates = (
            "C:/Windows/Fonts/msyh.ttc",
            "/maixapp/share/font/SourceHanSansCN-Regular.otf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        )
        font_path = next((p for p in candidates if os.path.isfile(p)), None)
        if font_path is None:
            self.skipTest("A Unicode TTF/OTF font is required")
        font_patch = patch.object(ui_render, "FONT_PATH", font_path)
        font_patch.start()
        self.addCleanup(font_patch.stop)
        ui_render._fonts.clear()
        self.addCleanup(ui_render._fonts.clear)
        self.sample = {
            "model": "LSM6DSOWTR",
            "acceleration_m_s2": {"x": 1.25, "y": -2.5, "z": 9.81},
            "angular_velocity_deg_s": {"x": -12.5, "y": 3.75, "z": 0.0},
            "temperature_c": None,
            "gyro_calibrated": False,
            "timestamp_unix": 1700000000.0,
        }
        self.texts = []
        original = ImageDraw.ImageDraw.text

        def capture(draw, xy, text, *args, **kwargs):
            self.texts.append((xy, text, kwargs.get("font")))
            return original(draw, xy, text, *args, **kwargs)

        text_patch = patch.object(ImageDraw.ImageDraw, "text", capture)
        text_patch.start()
        self.addCleanup(text_patch.stop)

    def render(self, **kwargs):
        return ui_render.render(os.path.join(self.tmp.name, "panel.rgba"),
                                clock=False, **kwargs)

    def test_six_signed_values_units_and_metadata(self):
        self.render(imu_sample=self.sample)
        texts = "\n".join(text for _, text, _ in self.texts)
        for expected in ("+1.25", "-2.50", "+9.81", "-12.50", "+3.75",
                         "+0.00", "m/s²", "°/s", "LSM6DSOWTR", "采样",
                         "温度未提供", "陀螺仪未校准", "self.imu.read"):
            self.assertIn(expected, texts)
        values = [f.size for _, text, f in self.texts if text == "+1.25"]
        self.assertTrue(values and min(values) >= 28)

    def test_panel_preserves_header_and_footer_and_is_opaque(self):
        baseline = self.render(state="listening", mcp_tool="self.imu.read")
        panel = self.render(state="listening", imu_sample=self.sample)
        for box in ((0, 0, 640, 59), (0, 438, 640, 480)):
            self.assertEqual(panel.crop(box).tobytes(), baseline.crop(box).tobytes())
        self.assertEqual(panel.getpixel((320, 240))[3], 255)
        self.assertEqual(panel.size, (640, 480))

    def test_invalid_values_do_not_become_zero(self):
        self.sample["acceleration_m_s2"] = {"x": None, "y": float("nan"), "z": float("inf")}
        self.sample["angular_velocity_deg_s"] = {"x": -float("inf"), "y": None}
        self.render(imu_sample=self.sample)
        texts = [text for _, text, _ in self.texts]
        self.assertEqual(texts.count("--"), 6)
        self.assertNotIn("+0.00", texts)

    def test_photo_has_priority(self):
        stream = io.BytesIO()
        Image.new("RGB", (640, 360), (220, 40, 60)).save(stream, "JPEG")
        kwargs = {"state": "listening", "photo_jpeg": stream.getvalue(), "mcp_tool": "self.camera.capture"}
        baseline = self.render(**kwargs)
        panel = self.render(imu_sample=self.sample, **kwargs)
        self.assertEqual(panel.tobytes(), baseline.tobytes())

    def test_omitted_or_none_sample_keeps_chat_layout(self):
        kwargs = {"state": "listening", "bubbles": [("ai", "聊天内容")]}
        baseline = self.render(**kwargs)
        self.render(imu_sample=self.sample, **kwargs)
        restored = self.render(imu_sample=None, **kwargs)
        self.assertEqual(baseline.tobytes(), restored.tobytes())
        self.assertEqual(self.render().getbbox(), None)

    def test_explicit_tool_name_is_preserved(self):
        self.render(imu_sample=self.sample, mcp_tool="self.imu.status")
        self.assertIn("MCP: self.imu.status", [t for _, t, _ in self.texts])

    def test_known_temperature_and_calibration(self):
        self.sample.update(temperature_c=26.5, gyro_calibrated=True)
        self.render(imu_sample=self.sample)
        texts = "\n".join(text for _, text, _ in self.texts)
        self.assertIn("26.5", texts)
        self.assertIn("°C", texts)
        self.assertIn("陀螺仪已校准", texts)

    def test_compact_values_share_footer_with_tool_and_keep_chat(self):
        self.render(state="listening", bubbles=[("ai", "聊天内容")],
                    imu_compact=self.sample, mcp_tool="self.imu.status")
        texts = [t for _, t, _ in self.texts]
        self.assertIn("聊天内容", texts)
        self.assertIn("MCP: self.imu.status", texts)
        expected = "A +1.25/-2.50/+9.81 m/s²  G -12.50/+3.75/+0.00 °/s"
        self.assertIn(expected, texts)
        for (x, y), text, f in self.texts:
            if text == expected or text.startswith("MCP:"):
                left, top, right, bottom = f.getbbox(text)
                self.assertGreaterEqual(y + top, 438)
                self.assertLessEqual(y + bottom, 480)
                self.assertLessEqual(x + right, 628)

    def test_compact_defaults_tool_and_handles_missing_large_values(self):
        self.sample["acceleration_m_s2"] = {"x": None, "y": float("inf"), "z": float("nan")}
        self.sample["angular_velocity_deg_s"] = {"x": 1e100, "y": -1e100, "z": None}
        self.render(imu_compact=self.sample)
        texts = [t for _, t, _ in self.texts]
        self.assertIn("MCP: self.imu.read", texts)
        row = next(item for item in self.texts if item[1].startswith("A "))
        (x, y), text, f = row
        self.assertIn("A --/--/-- m/s²", text)
        self.assertIn("/-- °/s", text)
        self.assertLessEqual(x + f.getbbox(text)[2], 628)
        self.assertNotIn("+0.00", text)

    def test_full_panel_does_not_repeat_compact_values(self):
        self.render(imu_sample=self.sample, imu_compact=self.sample)
        self.assertFalse(any(t.startswith("A ") for _, t, _ in self.texts))
        self.assertIn("IMU 六轴数据", [t for _, t, _ in self.texts])

    def test_compact_keeps_photo_and_only_changes_footer(self):
        stream = io.BytesIO()
        Image.new("RGB", (640, 360), (220, 40, 60)).save(stream, "JPEG")
        kwargs = {"state": "listening", "photo_jpeg": stream.getvalue()}
        baseline = self.render(**kwargs)
        panel = self.render(imu_sample=self.sample, imu_compact=self.sample, **kwargs)
        self.assertEqual(panel.crop((0, 0, 640, 438)).tobytes(),
                         baseline.crop((0, 0, 640, 438)).tobytes())
        self.assertTrue(any(t.startswith("A ") for _, t, _ in self.texts))
        self.assertFalse(any(t == "IMU 六轴数据" for _, t, _ in self.texts))

    def test_none_compact_is_pixel_identical(self):
        kwargs = {"state": "listening", "mcp_tool": "self.imu.read"}
        baseline = self.render(**kwargs)
        result = self.render(imu_compact=None, **kwargs)
        self.assertEqual(baseline.tobytes(), result.tobytes())


if __name__ == "__main__":
    unittest.main()
