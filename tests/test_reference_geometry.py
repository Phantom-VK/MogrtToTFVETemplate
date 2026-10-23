"""Element geometry of the converted sample against its Mixkit preview video.

Expected numbers were measured by hand on the settled frame (t = 2.0 s) of mixkit-305-preview.mp4
and scaled from 1280x720 to the 1920x1080 editor canvas, not copied from the converter's output.

Run:  python -m unittest discover -s tests -v
The text-width tests need Bebas Neue (the condensed face the preview is drawn in). Point
TFVE_FONT_DIR at the editor's public/fonts folder; a sibling TheFreeVideoEditor checkout is found
automatically.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import converter  # noqa: E402

MOGRT = ROOT / "mixkit-305.mogrt"
FONT_DIR = Path(os.environ.get("TFVE_FONT_DIR", ROOT.parent / "TheFreeVideoEditor" / "public" / "fonts"))
CANVAS_W, CANVAS_H = 1920, 1080
T = 2_000_000  # settled frame, microseconds

# The editor puts a text baseline this far (in em) above the clip's box bottom for Bebas Neue.
# Measured in the Program monitor with flat-bottomed caps at 36-200 px.
BEBAS_BASELINE_EM = 0.35

PX = 5.0  # tolerance for an edge, in 1080p pixels (the preview is a 1280x720 h264 frame)


def convert_sample(*extra):
    out = Path(tempfile.mkdtemp()) / "out.tfvetemplate"
    try:
        converter.main([str(MOGRT), "-o", str(out), "--duration", "4.333", *extra])
    except SystemExit as e:  # argparse and the converter exit instead of raising
        raise RuntimeError(f"converter exited with {e.code!r} for {extra}") from e
    return json.loads(out.read_text(encoding="utf-8"))


def value_at(channel, t, default):
    """Linear read of a serialised channel ({'value': v} or {'keyframes': [...]})."""
    if channel is None:
        return default
    if "value" in channel:
        return channel["value"]
    keys = channel["keyframes"]
    if t <= keys[0]["time"]:
        return keys[0]["value"]
    for a, b in zip(keys, keys[1:]):
        if a["time"] <= t <= b["time"]:
            f = (t - a["time"]) / (b["time"] - a["time"])
            va, vb = a["value"], b["value"]
            return [x + (y - x) * f for x, y in zip(va, vb)] if isinstance(va, list) else va + (vb - va) * f
    return keys[-1]["value"]


def clip_named(tpl, clip_id):
    for tr in tpl["tracks"]:
        for c in tr["clips"]:
            if c["id"] == clip_id:
                return c
    raise KeyError(clip_id)


def shape_rect(tpl, clip_id, t=T):
    """Left, top, right, bottom of a shape clip in 1080p pixels."""
    c = clip_named(tpl, clip_id)
    p = c["properties"]
    x, y = value_at(p.get("position"), t, [0, 0])
    sx, sy = value_at(p.get("scale"), t, [1, 1])
    w, h = c["source"]["width"] * abs(sx), c["source"]["height"] * abs(sy)
    cx, cy = CANVAS_W / 2 + x, CANVAS_H / 2 + y
    return cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2


def font_size(clip):
    """Size the editor draws the text at."""
    p = clip["properties"]
    return value_at(p.get("text.fontSize"), T, 0) * value_at(p.get("scale"), T, [1, 1])[0]


class TextSizeTest(unittest.TestCase):
    """No font file needed."""

    @classmethod
    def setUpClass(cls):
        cls.tpl = convert_sample()

    def test_title_is_drawn_at_the_after_effects_size(self):
        # AE: 287 px in a 3840-wide comp, shown on a 1920-wide canvas -> 287 * 0.5.
        title = clip_named(self.tpl, "clip_title")
        self.assertAlmostEqual(font_size(title), 143.5, delta=0.5)

    def test_subtitle_is_drawn_at_the_after_effects_size(self):
        # AE: 38 px text on a layer scaled 191 %, on a canvas half the comp size -> 38 * 1.91 * 0.5.
        sub = clip_named(self.tpl, "clip_subtitle")
        self.assertAlmostEqual(font_size(sub), 36.3, delta=0.5)

    def test_text_scale_is_not_stacked_on_font_size(self):
        # The editor rasterises text at fontSize x scale and then applies the scale again to the
        # picture, so a text clip must carry its size in text.fontSize with the scale left at 1.
        for clip_id in ("clip_title", "clip_subtitle"):
            scale = value_at(clip_named(self.tpl, clip_id)["properties"].get("scale"), T, [1, 1])
            self.assertEqual(scale, [1, 1], clip_id)

    def test_published_title_size_is_the_drawn_size(self):
        size = next(p for p in self.tpl["publishedParams"] if p["targetProperty"] == "text.fontSize")
        self.assertAlmostEqual(size["defaultValue"], 143.5, delta=0.5)
        self.assertLess(size["min"], size["defaultValue"])
        self.assertLess(size["defaultValue"], size["max"])


class FontMapTest(unittest.TestCase):
    def test_font_map_substitutes_family_and_clamps_weight(self):
        tpl = convert_sample("--font-map", "AcuminPro=Bebas Neue")
        for clip_id in ("clip_title", "clip_subtitle"):
            style = clip_named(tpl, clip_id)["source"]["style"]
            self.assertEqual(style["fontFamily"], "Bebas Neue")
            # Bebas Neue ships only a Regular; any other weight is drawn as synthetic bold.
            self.assertEqual(style["fontWeight"], "400")


@unittest.skipUnless(FONT_DIR.is_dir(), "needs the editor's fonts (set TFVE_FONT_DIR)")
class PreviewGeometryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tpl = convert_sample("--font-map", "AcuminPro=Bebas Neue", "--font-dir", str(FONT_DIR))

    def assertEdges(self, rect, expected, name):
        for got, want, edge in zip(rect, expected, ("left", "top", "right", "bottom")):
            self.assertAlmostEqual(got, want, delta=PX, msg=f"{name} {edge}: {got:.1f} vs preview {want}")

    def test_white_name_box(self):
        self.assertEdges(shape_rect(self.tpl, "clip_shape1"), (297.0, 754.5, 742.5, 864.0), "white box")

    def test_blue_accent_behind_the_name_box(self):
        # Same top and bottom as the white box, ends 27 px to its right.
        self.assertEdges(shape_rect(self.tpl, "clip_shape2"), (297.0, 754.5, 771.0, 864.0), "accent")

    def test_blue_subtitle_strip(self):
        self.assertEdges(shape_rect(self.tpl, "clip_divider"), (297.0, 873.0, 804.0, 907.5), "strip")

    def _baseline_and_left(self, clip_id):
        c = clip_named(self.tpl, clip_id)
        x, y = value_at(c["properties"]["position"], T, [0, 0])
        return CANVAS_H / 2 + y - BEBAS_BASELINE_EM * font_size(c), CANVAS_W / 2 + x

    def test_title_baseline_and_left_edge(self):
        baseline, left = self._baseline_and_left("clip_title")
        self.assertAlmostEqual(baseline, 859.5, delta=2.0)  # ink bottom of "JON DEE" in the preview
        self.assertAlmostEqual(left, 306.7, delta=2.0)  # preview ink starts at 309, minus the 2.3 px side bearing

    def test_text_is_solid_once_revealed(self):
        # The preview shows both texts at full strength from about 1.5 s on. The reveal matte covers
        # the whole text by then, so no part of the text may be faded out.
        for clip_id in ("clip_title", "clip_subtitle"):
            opacity = value_at(clip_named(self.tpl, clip_id)["properties"].get("opacity"), T, 1.0)
            self.assertGreater(opacity, 0.98, clip_id)

    def test_subtitle_baseline_and_left_edge(self):
        baseline, left = self._baseline_and_left("clip_subtitle")
        self.assertAlmostEqual(baseline, 902.7, delta=2.0)
        self.assertAlmostEqual(left, 311.6, delta=2.0)


@unittest.skipUnless(FONT_DIR.is_dir(), "needs the editor's fonts (set TFVE_FONT_DIR)")
class OutlineLineTest(unittest.TestCase):
    """The thin white line that runs round the title: from the top-left corner of the outline along the
    top, then down the right side, and back the same way when the template leaves.

    Expected ranges were measured on the frames of the preview video (white pixels in the row band of
    the top edge, y 733-742, and the column band of the right edge, x 765-774, at 1080p).
    """

    FONT_ARGS = ("--font-map", "AcuminPro=Bebas Neue", "--font-dir", str(FONT_DIR))

    @classmethod
    def setUpClass(cls):
        cls.tpl = convert_sample(*cls.FONT_ARGS)

    def line(self, tpl, clip_id, seconds):
        """Rect of an outline clip in 1080p pixels, or None while it is not drawn."""
        t = round(seconds * 1_000_000)
        if value_at(clip_named(tpl, clip_id)["properties"].get("opacity"), t, 1.0) < 0.5:
            return None
        return shape_rect(tpl, clip_id, t)

    def assertSpan(self, got, want, name, axis):
        lo, hi = (got[0], got[2]) if axis == "x" else (got[1], got[3])
        self.assertAlmostEqual(lo, want[0], delta=PX - 1, msg=f"{name} start {lo:.1f} vs preview {want[0]}")
        self.assertAlmostEqual(hi, want[1], delta=PX - 1, msg=f"{name} end {hi:.1f} vs preview {want[1]}")

    def test_only_the_edges_the_line_visits_are_drawn(self):
        ids = {tr["id"] for tr in self.tpl["tracks"]}
        self.assertLessEqual({"trk_outline_top", "trk_outline_right"}, ids)
        self.assertFalse({"trk_outline_bottom", "trk_outline_left"} & ids)

    def test_line_slides_along_the_top_edge(self):
        for t, span in ((0.40, (302, 360)), (0.50, (333, 494)), (0.60, (441, 640)), (0.70, (596, 742))):
            rect = self.line(self.tpl, "clip_outline_top", t)
            self.assertIsNotNone(rect, f"top edge at {t}s")
            self.assertSpan(rect, span, f"top edge at {t}s", "x")
            self.assertAlmostEqual((rect[1] + rect[3]) / 2, 737.1, delta=1.5)  # the outline's top, 17 px above the box
            self.assertAlmostEqual(rect[3] - rect[1], 3.75, delta=0.6)  # 7.5 px stroke at half size

    def test_line_turns_the_corner_and_runs_down_the_right_edge(self):
        self.assertIsNone(self.line(self.tpl, "clip_outline_top", 0.90))  # the top edge has been left behind
        for t, span in ((0.90, (760, 822)), (1.00, (810, 849)), (1.10, (842, 862)), (1.20, (860, 868))):
            rect = self.line(self.tpl, "clip_outline_right", t)
            self.assertIsNotNone(rect, f"right edge at {t}s")
            self.assertSpan(rect, span, f"right edge at {t}s", "y")
            self.assertAlmostEqual((rect[0] + rect[2]) / 2, 769.0, delta=1.5)

    def test_line_is_not_there_before_it_starts_or_after_it_has_run_out(self):
        for t in (0.20, 1.50, 2.00, 3.00):
            for clip_id in ("clip_outline_top", "clip_outline_right"):
                self.assertIsNone(self.line(self.tpl, clip_id, t), f"{clip_id} at {t}s")


if __name__ == "__main__":
    unittest.main()
