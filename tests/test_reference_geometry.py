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


def shape_rect(tpl, clip_id):
    """Left, top, right, bottom of a shape clip in 1080p pixels."""
    c = clip_named(tpl, clip_id)
    p = c["properties"]
    x, y = value_at(p.get("position"), T, [0, 0])
    sx, sy = value_at(p.get("scale"), T, [1, 1])
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


if __name__ == "__main__":
    unittest.main()
