#!/usr/bin/env python3
"""
mogrt_to_tfvetemplate.py  --  Convert Adobe Motion Graphics Templates (.mogrt)
into The Free Video Editor motion templates (.tfvetemplate).

    pip install py-aep
    python mogrt_to_tfvetemplate.py my-title.mogrt                 # -> my-title.tfvetemplate
    python mogrt_to_tfvetemplate.py ./mogrts/ -o ./out/            # batch
    python mogrt_to_tfvetemplate.py a.mogrt --duration 5 --outro 1 --category Titles

HOW IT WORKS
------------
A .mogrt is a zip:  definition.json (the Essential-Graphics controls)  +
project.aegraphic (a zip holding the real After Effects project, *.aep)  +
thumb.png.  definition.json only lists the editable controls; every graphic and
every keyframe lives in the .aep.  This script reads the .aep with `py-aep`, and
for each text / shape / solid layer it:

  1. Evaluates the layer's WORLD transform (parent chain + anchor point, i.e. the
     exact After Effects matrix math) at every moment it can change.
  2. Bakes position / rotation / scale / opacity into flat keyframes
     (adaptive sampling, so AE's bezier easing is reproduced to within a
     fraction of a pixel).  Parents (nulls) therefore never need to exist in
     the output.
  3. Converts shape-layer "line + Trim Paths" into a rect clip whose width is
     animated, text layers into text clips (font, weight, size, tracking, colour),
     and Color-Control / expression-linked colours into published colour params.
  4. Turns the mogrt's Essential-Graphics controls into `publishedParams`.

Output schema == the reference file you supplied (motion-template-v1).

KNOWN FIDELITY LIMITS (also printed as warnings per file)
---------------------------------------------------------
* Track mattes / masks have no equivalent in the reference schema.  A matte-driven
  reveal is approximated by an opacity ramp whose timing is computed from the real
  geometry (how much of the text is inside the matte at each instant).
* Only: text layers, 2-point stroked lines (+Trim Paths), rect / ellipse /
  rectangular-path shapes and solids are converted.  Effects other than Fill and
  Color Control, text animators, 3D, expressions (except colour links) are skipped.
* Assumptions about the editor (change with flags if it renders differently):
    - position is in px relative to the centre of the 1920x1080 AE comp,
      +y is DOWN, rotation is degrees clockwise-positive;
    - a text clip's position/rotation/scale pivot is the bottom-centre of its box
      (verticalAnchor "bottom"), box bottom == text baseline  (--descent-ratio);
* Easing: AE's bezier ease is reproduced by densely sampled linear keyframes
  (adaptive, <=0.2px / <=0.2deg / <=0.15% scale error), so the result does not
  depend on how the editor interprets easeIn/easeOut handles.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import tempfile
import time
import zipfile
from pathlib import Path

try:
    import py_aep
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency.  Run:  pip install py-aep")

SCHEMA_URL = "https://thefreevideoeditor.com/schemas/motion-template-v1.json"
US = 1_000_000  # template time unit: microseconds

TM_ALPHA, TM_ALPHA_INV, TM_LUMA, TM_LUMA_INV = 5013, 5014, 5015, 5016

# per-channel absolute tolerances used when baking keyframes
TOL = {"pos": 0.2, "rot": 0.2, "scale": 0.0015, "opacity": 0.005}

WEIGHTS = {
    "thin": 100, "hairline": 100, "extralight": 200, "ultralight": 200, "light": 300,
    "regular": 400, "normal": 400, "book": 400, "roman": 400, "medium": 500,
    "semibold": 600, "demibold": 600, "bold": 700, "extrabold": 800, "ultrabold": 800,
    "black": 900, "heavy": 900,
}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
class Notes:
    def __init__(self):
        self.items: list[str] = []

    def warn(self, msg: str):
        if msg not in self.items:
            self.items.append(msg)


def find(group, match_name):
    """Direct child property/group of `group` with the given AE match name."""
    if group is None:
        return None
    for p in group.properties:
        if p.match_name == match_name:
            return p
    return None


def is_group(p) -> bool:
    return hasattr(p, "properties")


def walk(group):
    for p in group.properties:
        yield p
        if is_group(p):
            yield from walk(p)


def key_times(layer) -> list[float]:
    out = []
    for p in walk_layer(layer):
        if not is_group(p):
            try:
                out.extend(k.time for k in p.keyframes)
            except Exception:
                pass
    return out


def walk_layer(layer):
    for p in layer.properties:
        yield p
        if is_group(p):
            yield from walk(p)


def slug(s: str) -> str:
    s = re.sub(r"\[.*?\]", "", s).strip()
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s or "template"


def r(v, n=4):
    return round(float(v) + 0.0, n)


def rv(vs, n=4):
    return [r(x, n) for x in vs]


def to_us(t: float) -> int:
    return int(round(t * US))


# --------------------------------------------------------------------------- #
# 2-D affine math   (a,b,c,d,e,f):  x' = a*x + c*y + e ;  y' = b*x + d*y + f
# --------------------------------------------------------------------------- #
IDENT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mmul(m, n):
    """m o n  (apply n first, then m)."""
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a1 * a2 + c1 * b2, b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1,
    )


def mapply(m, x, y):
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def trs(pos, rot_deg, scale, anchor):
    """T(pos) . R(rot) . S(scale) . T(-anchor)  -- After Effects layer matrix."""
    rr = math.radians(rot_deg)
    cs, sn = math.cos(rr), math.sin(rr)
    sx, sy = scale
    a, b, c, d = cs * sx, sn * sx, -sn * sy, cs * sy
    return (a, b, c, d, pos[0] - (a * anchor[0] + c * anchor[1]), pos[1] - (b * anchor[0] + d * anchor[1]))


# --------------------------------------------------------------------------- #
# polygon clipping (convex) -- used to measure how much of a text box a matte hides
# --------------------------------------------------------------------------- #
def poly_area(p):
    s = 0.0
    for i in range(len(p)):
        x1, y1 = p[i]
        x2, y2 = p[(i + 1) % len(p)]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def clip_convex(subject, clipper):
    if abs(poly_area(clipper)) < 1e-9 or len(subject) < 3:
        return []
    sign = 1.0 if poly_area(clipper) > 0 else -1.0

    def inside(pt, a, b):
        return sign * ((b[0] - a[0]) * (pt[1] - a[1]) - (b[1] - a[1]) * (pt[0] - a[0])) >= -1e-12

    def inter(p1, p2, a, b):
        x1, y1, x2, y2 = p1[0], p1[1], p2[0], p2[1]
        x3, y3, x4, y4 = a[0], a[1], b[0], b[1]
        den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
        if abs(den) < 1e-12:
            return p2
        t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den
        return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))

    out = list(subject)
    for i in range(len(clipper)):
        a, b = clipper[i], clipper[(i + 1) % len(clipper)]
        inp, out = out, []
        if not inp:
            break
        s = inp[-1]
        for e in inp:
            if inside(e, a, b):
                if not inside(s, a, b):
                    out.append(inter(s, e, a, b))
                out.append(e)
            elif inside(s, a, b):
                out.append(inter(s, e, a, b))
            s = e
    return out


# --------------------------------------------------------------------------- #
# keyframe baking
# --------------------------------------------------------------------------- #
def _vec(v):
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _err(v1, v2, tol):
    return max(abs(a - b) for a, b in zip(_vec(v1), _vec(v2))) / tol


def bake_adaptive(fn, breakpoints, t0, t1, tol, max_depth=11, min_dt=1.0 / 240):
    """Piecewise-linear keys approximating fn(t) on [t0,t1] within `tol`."""
    ts = sorted({min(max(t, t0), t1) for t in breakpoints} | {t0, t1})
    pts = [(t, fn(t)) for t in ts]
    out = [pts[0]]

    def refine(a, b, depth):
        ta, va = a
        tb, vb = b
        if depth >= max_depth or (tb - ta) <= min_dt:
            out.append(b)
            return
        ok = True
        for q in (0.2, 0.4, 0.5, 0.6, 0.8):
            tq = ta + (tb - ta) * q
            lin = [x + (y - x) * q for x, y in zip(_vec(va), _vec(vb))]
            if _err(lin, fn(tq), tol) > 1.0:
                ok = False
                break
        if ok:
            out.append(b)
        else:
            tm = (ta + tb) / 2
            m = (tm, fn(tm))
            refine(a, m, depth + 1)
            refine(m, b, depth + 1)

    for a, b in zip(pts, pts[1:]):
        refine(a, b, 0)
    return reduce_keys(out, tol)


def reduce_keys(keys, tol):
    """Ramer-Douglas-Peucker over (t, value) so redundant keys disappear."""
    if len(keys) <= 2:
        return keys
    keep = [False] * len(keys)
    keep[0] = keep[-1] = True

    def rec(i, j):
        ta, va = keys[i]
        tb, vb = keys[j]
        worst, idx = 0.0, -1
        for k in range(i + 1, j):
            tk, vk = keys[k]
            q = (tk - ta) / (tb - ta) if tb > ta else 0.0
            lin = [x + (y - x) * q for x, y in zip(_vec(va), _vec(vb))]
            e = _err(lin, vk, tol)
            if e > worst:
                worst, idx = e, k
        if worst > 0.5 and idx >= 0:
            keep[idx] = True
            rec(i, idx)
            rec(idx, j)

    rec(0, len(keys) - 1)
    return [k for k, f in zip(keys, keep) if f]


def is_static(keys, tol):
    return all(_err(k[1], keys[0][1], tol) <= 1.0 for k in keys)


# --------------------------------------------------------------------------- #
# font / text helpers
# --------------------------------------------------------------------------- #
def split_font(ps: str):
    fam, _, style = (ps or "Arial").partition("-")
    sl = style.lower()
    italic = "italic" in sl or "oblique" in sl
    s = re.sub(r"italic|oblique", "", sl).replace(" ", "").replace("-", "")
    weight = WEIGHTS.get(s, 400) if s else 400
    if s and s not in WEIGHTS:
        m = re.match(r"(.*?)(\d{3})$", s)
        weight = int(m.group(2)) if m else 400
    fam = re.sub(r"(PSMT|MT|PS)$", "", fam)
    fam = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", fam).strip() or "Arial"
    return fam, weight, italic


class TextMeter:
    """Width estimation. Uses real glyph advances if a matching font file is found in
    --font-dir, otherwise a calibrated heuristic (good enough for bounds + matte maths)."""

    def __init__(self, font_dir: str | None, notes: Notes):
        self.files = {}
        self.cache = {}
        self.notes = notes
        if font_dir:
            try:
                from fontTools.ttLib import TTFont
            except Exception:
                notes.warn("fontTools not available; using estimated text widths")
                return
            for p in Path(font_dir).rglob("*"):
                if p.suffix.lower() in (".ttf", ".otf"):
                    try:
                        f = TTFont(str(p), lazy=True)
                        ps = f["name"].getDebugName(6)
                        if ps:
                            self.files[ps] = p
                    except Exception:
                        pass

    def width(self, ps, size, text, tracking_px, weight):
        lines = text.split("\n")
        return max(self._line(ps, size, ln, tracking_px, weight) for ln in lines) if lines else 0.0

    def _line(self, ps, size, line, tracking_px, weight):
        if ps in self.files:
            try:
                from fontTools.ttLib import TTFont
                f = self.cache.get(ps) or TTFont(str(self.files[ps]))
                self.cache[ps] = f
                cmap, hmtx, upm = f.getBestCmap(), f["hmtx"], f["head"].unitsPerEm
                w = sum(hmtx[cmap.get(ord(ch), ".notdef")][0] for ch in line) * size / upm
                return w + tracking_px * max(len(line) - 1, 0)
            except Exception:
                pass
        base = 0.675 + 0.045 * (min(max(weight, 300), 900) - 300) / 600  # em per capital
        w = 0.0
        for ch in line:
            if ch == " ":
                w += 0.30
            elif ch.isupper() or ch.isdigit():
                w += base
            elif ch in "iljtf.,:;'!|":
                w += base * 0.45
            elif ch in "mwMW":
                w += base * 1.15
            else:
                w += base * 0.84
        return w * size + tracking_px * max(len(line) - 1, 0)


# --------------------------------------------------------------------------- #
# mogrt loading
# --------------------------------------------------------------------------- #
def load_mogrt(path: Path, workdir: Path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if "definition.json" not in names:
            raise SystemExit(f"{path.name}: no definition.json - not a .mogrt?")
        definition = json.loads(z.read("definition.json").decode("utf-8-sig"))
        aeg = next((n for n in names if n.lower().endswith((".aegraphic", ".aep"))), None)
        if aeg is None:
            raise SystemExit(f"{path.name}: no project.aegraphic inside (Premiere-native mogrts are not supported)")
        blob = z.read(aeg)
    aep_path = None
    if aeg.lower().endswith(".aep"):
        aep_path = workdir / Path(aeg).name
        aep_path.write_bytes(blob)
    else:
        inner = workdir / "inner.aegraphic"
        inner.write_bytes(blob)
        try:
            with zipfile.ZipFile(inner) as z2:
                aep = next((n for n in z2.namelist() if n.lower().endswith(".aep")), None)
                if aep is None:
                    raise SystemExit(f"{path.name}: no .aep inside project.aegraphic")
                aep_path = workdir / Path(aep).name
                aep_path.write_bytes(z2.read(aep))
        except zipfile.BadZipFile:
            raise SystemExit(f"{path.name}: project.aegraphic is not a zip archive")
    app = py_aep.parse(str(aep_path))
    comps = app.project.compositions
    if not comps:
        raise SystemExit(f"{path.name}: no composition found")
    name = (definition.get("sourceInfoLocalized") or {})
    main = None
    for loc in name.values():
        main = loc.get("name")
        break
    comp = next((c for c in comps if c.name == main), comps[0])
    comp, wrappers, outer = _descend_precomp(comp)
    return definition, comp, wrappers, outer


def _descend_precomp(comp):
    """Many mogrts wrap the real artwork in a nested comp (often 2x larger, scaled down by
    the wrapper layer).  If `comp` has no text / shape layers of its own, follow its first
    precomp layer down.  Returns (inner_comp, [wrapper layers outer->inner], outer_comp);
    the wrappers' transforms are applied later by Ctx so the output matches the outer comp."""
    outer, wrappers = comp, []
    for _ in range(5):
        layers = list(comp.layers)
        if any(type(L).__name__ in ("TextLayer", "ShapeLayer") for L in layers):
            break
        pick = next((L for L in layers if getattr(L, "source", None) is not None and hasattr(L.source, "layers")), None)
        if pick is None:
            break
        wrappers.append(pick)
        comp = pick.source
    return comp, wrappers, outer


# --------------------------------------------------------------------------- #
# layer model
# --------------------------------------------------------------------------- #
class Ctx:
    def __init__(self, comp, args, notes, wrappers=(), outer=None, definition=None):
        self.comp = comp
        self.eg = {}   # Essential-Graphics control defaults by UI name (numbers / bools)
        for ctl in (definition or {}).get("clientControls", []):
            if ctl.get("type") in (1, 2) and not isinstance(ctl.get("value"), (dict, list)):
                for d in ((ctl.get("uiName") or {}).get("strDB") or []):
                    self.eg[d.get("str", "")] = ctl.get("value")
        self.args = args
        self.notes = notes
        self.wrappers = list(wrappers)
        outer = outer or comp
        self.cx, self.cy = outer.width / 2.0, outer.height / 2.0   # canvas = outermost comp
        cw, ch = getattr(args, "canvas", None) or (outer.width, outer.height)
        self.k = min(cw / outer.width, ch / outer.height)           # fit the comp into the editor canvas
        if abs(self.k - 1.0) > 1e-9:
            notes.warn(f"comp is {outer.width}x{outer.height}; scaled x{self.k:.4g} to fit the {cw}x{ch} canvas (--canvas)")
        self._tr = {}
        self._outer_ids = set()
        if self.wrappers:
            c = outer
            for W in self.wrappers:
                self._outer_ids |= {id(x) for x in c.layers}
                c = W.source
            notes.warn(f"artwork lives in nested comp '{comp.name}' ({comp.width}x{comp.height}) inside "
                       f"'{outer.name}' ({outer.width}x{outer.height}); wrapper layer transform applied")
        self.meter = TextMeter(args.font_dir, notes)
        self._wm = {}

    # --- transforms --------------------------------------------------------
    def layer_named(self, name):
        return next((x for x in self.comp.layers if x.name == name), None)

    def eval_control(self, expr, L=None):
        """Evaluate  thisComp.layer("X").effect("Name")("Slider")  and  ....transform.x/yPosition."""
        expr = (expr or "").strip().rstrip(";")
        if L is not None and expr.startswith("effect("):
            expr = f'thisComp.layer("{L.name}").' + expr
        m = re.fullmatch(r'thisComp\.layer\("([^"]+)"\)\.effect\("([^"]+)"\)\("([^"]+)"\)', expr)
        if m:
            lname, ename, pname = m.groups()
            if isinstance(self.eg.get(ename), (int, float)) and not isinstance(self.eg.get(ename), bool):
                return float(self.eg[ename])
            Lc = self.layer_named(lname)
            ep = find(Lc, "ADBE Effect Parade") if Lc is not None else None
            e = next((x for x in ep.properties if x.name == ename), None) if ep is not None else None
            q = (find(e, pname) or next((c for c in e.properties if not is_group(c)), None)) if e is not None else None
            return float(q.value) if q is not None else None
        m = re.fullmatch(r'thisComp\.layer\("([^"]+)"\)\.transform\.(xPosition|yPosition)', expr)
        if m:
            lname, which = m.groups()
            i = 0 if which == "xPosition" else 1
            ev = self.eg.get("Position X" if i == 0 else "Position Y")
            if lname == "CONTROL" and isinstance(ev, (int, float)):
                return float(ev)
            Lc = self.layer_named(lname)
            if Lc is not None:
                return float(find(find(Lc, "ADBE Transform Group"), "ADBE Position").value[i])
        return None

    def position(self, L, g, t):
        pos = list(find(g, "ADBE Position").value_at_time(t))
        for i, mn in enumerate(("ADBE Position_0", "ADBE Position_1", "ADBE Position_2")):
            q = find(g, mn)
            if q is None or i >= len(pos):
                continue
            v = None
            if getattr(q, "expression_enabled", False):
                try:
                    v = self.eval_control(getattr(q, "expression", ""), L)
                except Exception:
                    v = None
                if v is None:
                    self.notes.warn(f"'{L.name}': separated position expression not understood; using stored value")
            elif getattr(q, "keyframes", None):      # genuinely animated separated dimension
                try:
                    v = q.value_at_time(t)
                except Exception:
                    v = None
            # (a static, un-animated Position_i is NOT trusted: it exists on every layer)
            if v is not None and math.isfinite(float(v)):
                pos[i] = float(v)
        return pos

    def tvals(self, L, t):
        g = find(L, "ADBE Transform Group")
        pos = self.position(L, g, t)
        anc = find(g, "ADBE Anchor Point").value_at_time(t)
        sc = find(g, "ADBE Scale").value_at_time(t)
        rot = find(g, "ADBE Rotate Z").value_at_time(t)
        op = find(g, "ADBE Opacity").value_at_time(t)
        return pos, anc, sc, rot, op

    def local(self, L, t):
        pos, anc, sc, rot, _ = self.tvals(L, t)
        return trs(pos[:2], rot, (sc[0] / 100.0, sc[1] / 100.0), anc[:2])

    def world(self, L, t):
        key = (id(L), round(t, 9))
        if key in self._wm:
            return self._wm[key]
        m = self.local(L, t)
        if L.parent is not None:
            m = mmul(self.world(L.parent, t), m)
        elif self.wrappers and id(L) not in self._outer_ids:
            for W in reversed(self.wrappers):          # inner wrapper first, outermost last
                m = mmul(self.world(W, t), m)
        self._wm[key] = m
        return m

    def chain(self, L):
        out = []
        while L is not None:
            out.append(L)
            L = L.parent
        return out[::-1]  # root -> leaf

    def decomposed(self, L, t, extra_rot=0.0, extra_scale=(1.0, 1.0)):
        """rotation / scale of the world matrix, computed from the TRS chain so that
        angles never wrap and negative scales keep their sign."""
        rot, sx, sy = extra_rot, extra_scale[0] * self.k, extra_scale[1] * self.k
        for a in [x for W in self.wrappers for x in self.chain(W)] + self.chain(L):
            _, _, sc, rt, _ = self.tvals(a, t)
            rot += rt
            sx *= sc[0] / 100.0
            sy *= sc[1] / 100.0
            if abs(abs(sc[0]) - abs(sc[1])) > 1e-6 and abs(rt) > 1e-6:
                self.notes.warn(f"layer chain of '{L.name}' has non-uniform scale together with rotation (skew not representable)")
        return rot, sx, sy

    def to_center(self, x, y):
        return ((x - self.cx) * self.k, (y - self.cy) * self.k)

    def text_rect(self, name):
        """Estimated After Effects sourceRectAtTime() of a text layer, in layer space."""
        if name in self._tr:
            return self._tr[name]
        L = next((x for x in self.comp.layers if x.name == name and type(x).__name__ == "TextLayer"), None)
        out = None
        if L is not None:
            try:
                doc = find(find(L, "ADBE Text Properties"), "ADBE Text Document").value
                text = (doc.text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x03", "\n")
                if getattr(doc, "all_caps", False):
                    text = text.upper()
                _, weight, _ = split_font(doc.font)
                size = float(doc.font_size)
                trk = float(doc.tracking or 0) / 1000.0 * size
                w = self.meter.width(doc.font, size, text, trk, weight) * self.args.text_width_factor
                nl = text.count("\n") + 1
                lead = float(getattr(doc, "leading", size * 1.2) or size * 1.2)
                h = size * self.args.text_height_ratio + (nl - 1) * lead
                just = int(doc.justification)
                b_shift = float(getattr(doc, "baseline_shift", 0.0) or 0.0)
                if getattr(doc, "box_text", False) and getattr(doc, "box_text_pos", None):
                    bx = float(doc.box_text_pos[0])
                    by = float(doc.box_text_pos[1])
                    bw = float(doc.box_text_size[0]) if getattr(doc, "box_text_size", None) else w
                    if just == 7413:      # left
                        left = bx
                    elif just == 7414:    # right
                        left = bx + bw - w
                    else:                 # center
                        left = bx + (bw - w) / 2.0
                    top = by - b_shift
                else:
                    left = {7413: 0.0, 7414: -w}.get(just, -w / 2.0)
                    top = -size * self.args.text_ascent_ratio - b_shift
                out = {"width": w, "height": h, "left": left, "top": top}
            except Exception as e:
                self.notes.warn(f"could not measure text layer '{name}' ({type(e).__name__})")
        self._tr[name] = out
        return out

    def all_keytimes(self, L):
        ts = []
        for a in [x for W in self.wrappers for x in self.chain(W)] + self.chain(L):
            ts += key_times(a)
        return ts


# --------------------------------------------------------------------------- #
# clip builders
# --------------------------------------------------------------------------- #
class Clip:
    """One output clip. `state(t)` returns the baked channel values for time t."""

    def __init__(self, kind, layer):
        self.kind, self.layer = kind, layer
        self.role = None
        self.source = {}
        self.static_props = {}
        self.state = None  # fn(t) -> dict(pos=[x,y], rot=deg, scale=[sx,sy], opacity=o)
        self.breakpoints: list[float] = []
        self.color_links: list[tuple[str, str]] = []  # (layer name, effect name)
        self.size = None  # font size for text
        self.family = None
        self.box_fn = None  # fn(t)-> list of 4 world points (centre-relative) for bounds
        self.default_color = None


def resolve_color(ctx: Ctx, L, comp):
    """Colour of a layer after its Fill effect (+ expression link to a Color Control)."""
    eff_parade = find(L, "ADBE Effect Parade")
    if eff_parade is None:
        return None, None
    for eff in eff_parade.properties:
        if eff.match_name != "ADBE Fill":
            continue
        if getattr(eff, "enabled", True) is False:
            continue
        cp = find(eff, "ADBE Fill-0002")
        if cp is None:
            continue
        expr = getattr(cp, "expression", "") or ""
        if getattr(cp, "expression_enabled", False) and expr:
            m = re.search(r'layer\("([^"]+)"\)\s*\.effect\("([^"]+)"\)\(\s*"([^"]+)"\s*\)', expr)
            if m:
                lname, ename = m.group(1), m.group(2)
                src = next((x for x in comp.layers if x.name == lname), None)
                if src is not None:
                    ep = find(src, "ADBE Effect Parade")
                    ce = next((e for e in ep.properties if e.name == ename), None) if ep else None
                    if ce is not None:
                        cprop = next((q for q in ce.properties if q.match_name == "ADBE Color Control-0001"), None)
                        if cprop is not None:
                            return rv(cprop.value, 6), (lname, ename)
            ctx.notes.warn(f"expression on Fill color of '{L.name}' not understood; using its static value")
        return rv(cp.value, 6), None
    return None, None


def matte_local_poly(ctx, L, t):
    """Matte outline in the matte layer's own space (a rectangle).  Solids use their
    size; shape layers use the bounding box of their first rect / path / ellipse,
    mapped through the shape group's transform.  Returns None if it can't be read."""
    src = getattr(L, "source", None)
    w, h = getattr(src, "width", None), getattr(src, "height", None)
    if w and h:
        return [(0, 0), (w, 0), (w, h), (0, h)]
    try:
        root = find(L, "ADBE Root Vectors Group")
        grp = next((q for q in root.properties if q.match_name == "ADBE Vector Group"), None)
        contents = find(grp, "ADBE Vectors Group")
        geom = next((q for q in contents.properties if q.match_name in (
            "ADBE Vector Shape - Rect", "ADBE Vector Shape - Group", "ADBE Vector Shape - Ellipse")), None)
        if geom is None:
            raise ValueError("no geometry")
        if geom.match_name == "ADBE Vector Shape - Group":
            sh = find(geom, "ADBE Vector Shape")
            v = (sh.value_at_time(t) if hasattr(sh, "value_at_time") else sh.value).vertices
            xs, ys = [q[0] for q in v], [q[1] for q in v]
            x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        else:
            tag = "Rect" if geom.match_name.endswith("Rect") else "Ellipse"
            size = find(geom, f"ADBE Vector {tag} Size").value_at_time(t)
            c = find(geom, f"ADBE Vector {tag} Position").value_at_time(t)
            x0, x1 = c[0] - size[0] / 2, c[0] + size[0] / 2
            y0, y1 = c[1] - size[1] / 2, c[1] + size[1] / 2
        pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        gxf = find(grp, "ADBE Vector Transform Group")
        if gxf is not None:
            pos = find(gxf, "ADBE Vector Position").value_at_time(t)
            anc = find(gxf, "ADBE Vector Anchor").value_at_time(t)
            sc = find(gxf, "ADBE Vector Scale").value_at_time(t)
            rot = find(gxf, "ADBE Vector Rotation").value_at_time(t)
            m = trs(pos, rot, (sc[0] / 100.0, sc[1] / 100.0), anc)
            pts = [mapply(m, *q) for q in pts]
        return pts
    except Exception as e:  # unsupported matte geometry -> ignore the matte
        ctx.notes.warn(f"track matte '{L.name}': geometry not readable ({type(e).__name__}); matte ignored")
        return None


def build_text_clip(ctx: Ctx, L, matte_layer, matte_type, D) -> Clip | None:
    tp = find(L, "ADBE Text Properties")
    dprop = find(tp, "ADBE Text Document")
    doc = dprop.value
    if dprop.keyframes:
        ctx.notes.warn(f"'{L.name}': animated Source Text keyframes ignored")
    anim = find(tp, "ADBE Text Animators")
    if anim is not None and len(anim.properties) > 0:
        ctx.notes.warn(f"'{L.name}': text animators are not converted")
    if getattr(L, "three_d_layer", False):
        ctx.notes.warn(f"'{L.name}': 3D layer flattened to 2D")
    if getattr(doc, "box_text", False):
        ctx.notes.warn(f"'{L.name}': paragraph (box) text - treated as point text")

    text = (doc.text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x03", "\n")
    if getattr(doc, "all_caps", False):
        text = text.upper()
    fam, weight, italic = split_font(doc.font)
    size = float(doc.font_size)
    tracking_px = float(doc.tracking or 0) / 1000.0 * size
    just = int(doc.justification)
    align = {7413: "left", 7414: "right"}.get(just, "center")
    nlines = text.count("\n") + 1
    leading = float(getattr(doc, "leading", size * 1.2) or size * 1.2)
    width = ctx.meter.width(doc.font, size, text, tracking_px, weight)
    base_last = (nlines - 1) * leading                              # last baseline (layer y)
    b_shift = float(getattr(doc, "baseline_shift", 0.0) or 0.0)
    descent = ctx.args.descent_ratio * size

    if getattr(doc, "box_text", False) and getattr(doc, "box_text_pos", None):
        bx = float(doc.box_text_pos[0])
        by = float(doc.box_text_pos[1])
        bw = float(doc.box_text_size[0]) if getattr(doc, "box_text_size", None) else width
        if align == "left":
            px = bx
        elif align == "right":
            px = bx + width
        else:
            px = bx + width / 2.0
        py = by + (nlines - 1) * leading + 1.2 * size - b_shift
        x0 = bx if align == "left" else (bx + (bw - width) / 2.0 if align == "center" else bx + bw - width)
        x1 = x0 + width
        y0 = by - b_shift
        y1 = py
    else:
        px = 0.0
        py = base_last + descent - b_shift
        x0 = 0.0 if align == "left" else (-width / 2.0 if align == "center" else -width)
        x1 = x0 + width
        y0 = -size * ctx.args.text_ascent_ratio - b_shift
        y1 = py

    color, link = resolve_color(ctx, L, ctx.comp)
    if color is None:
        fc = list(doc.fill_color) + [1.0]
        color = rv(fc[:3] + [1.0], 6)
    clip = Clip("text", L)
    clip.size, clip.family = size, fam
    clip.default_color = color
    if link:
        clip.color_links.append(link)

    style = {"fontFamily": fam, "fontWeight": str(weight), "alignment": align, "letterSpacing": r(tracking_px, 3)}
    if italic or getattr(doc, "faux_italic", False):
        style["fontStyle"] = "italic"
    clip.source = {
        "type": "text",
        "text": text,
        "style": style,
        "box": {"autoSize": True, "verticalAnchor": "bottom", "paddingX": 0, "paddingY": 0, "frameStyle": "none"},
    }
    clip.static_props = {
        "text.fontSize": {"value": r(size, 3)},
        "text.letterSpacing": {"value": r(tracking_px, 3)},
        "text.fillColor": {"value": color},
    }
    pivot = (px, py)
    rect = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    ctx_ = ctx

    def world_rect(t):
        m = ctx_.world(L, t)
        return [mapply(m, *p) for p in rect]

    def frac(t):
        if matte_layer is None:
            return 1.0
        local = matte_local_poly(ctx_, matte_layer, t)
        if local is None:
            return 1.0
        mm = ctx_.world(matte_layer, t)
        mp = [mapply(mm, *p) for p in local]
        sub = world_rect(t)
        a = abs(poly_area(sub))
        if a < 1e-9:
            return 1.0
        inside = abs(poly_area(clip_convex(sub, mp))) / a
        return inside if matte_type == TM_ALPHA else 1.0 - inside

    def state(t):
        m = ctx_.world(L, t)
        x, y = ctx_.to_center(*mapply(m, *pivot))
        rot, sx, sy = ctx_.decomposed(L, t)
        op = ctx_.tvals(L, t)[4] / 100.0
        vis = 1.0 if (L.in_point - 1e-9 <= t <= L.out_point + 1e-9) else 0.0
        return {"pos": [x, y], "rot": rot, "scale": [sx, sy], "opacity": op * vis * (frac(t) if vis else 0.0)}

    clip.state = state
    clip.box_fn = lambda t: [ctx_.to_center(*p) for p in world_rect(t)]
    bp = ctx.all_keytimes(L) + [L.in_point, max(L.in_point - 1e-6, 0.0)]
    if matte_layer is not None:
        bp += ctx.all_keytimes(matte_layer)
    clip.breakpoints = bp
    return clip


_SAFE = re.compile(r"^[\w\s\.\+\-\*/\(\),\[\]]*$")


def eval_rect_expr(expr, rect):
    """Evaluate the common 'size/position from a text layer's sourceRectAtTime()' expression
    pattern  (w = s.sourceRectAtTime().width; ...; [w+200, h+10]).  Returns [x, y] or None."""
    src = re.sub(r"\w+\.sourceRectAtTime\([^)]*\)\.(width|height|top|left)", lambda m: "R_" + m.group(1), expr)
    env = {"R_" + k: float(v) for k, v in rect.items()}
    result = None
    for ln in (x.strip() for x in re.split(r"[;\r\n]+", src)):
        if not ln:
            continue
        m = re.match(r"^(\w+)\s*=\s*(.+)$", ln)
        rhs = m.group(2) if m else ln
        if "thisComp" in rhs or "content(" in rhs:
            if m:
                env[m.group(1)] = None
                continue
            return None
        if not _SAFE.match(rhs):
            return None
        try:
            val = eval(rhs, {"__builtins__": {}}, env)
        except Exception:
            return None
        if m:
            env[m.group(1)] = val
        else:
            result = val
    if isinstance(result, (list, tuple)) and len(result) >= 2:
        return [float(result[0]), float(result[1])]
    return None


def resolve_expr_vec(ctx, prop, L, what):
    """Value of an expression-driven 2-D property, or None if not an understood expression."""
    if prop is None or not getattr(prop, "expression_enabled", False):
        return None
    expr = getattr(prop, "expression", "") or ""
    m = re.search(r'layer\("([^"]+)"\)', expr)
    rect = ctx.text_rect(m.group(1)) if m else None
    val = eval_rect_expr(expr, rect) if rect else None
    if val is None:
        ctx.notes.warn(f"'{L.name}': expression on {what} not understood; using its (possibly stale) stored value")
        return None
    ctx.notes.warn(f"'{L.name}': {what} evaluated from text '{m.group(1)}' bounds (estimate; tune --text-width-factor / --font-dir)")
    return val


def build_shape_clip(ctx: Ctx, L) -> Clip | None:
    root = find(L, "ADBE Root Vectors Group")
    if root is None:
        return None
    grp = next((p for p in root.properties if p.match_name == "ADBE Vector Group"), None)
    if grp is None:
        return None
    contents = find(grp, "ADBE Vectors Group")
    gxf = find(grp, "ADBE Vector Transform Group")
    trims = [p for p in walk(root) if p.match_name == "ADBE Vector Filter - Trim"]
    geom = stroke = fill = None
    for p in contents.properties:
        if p.match_name in ("ADBE Vector Shape - Group", "ADBE Vector Shape - Rect", "ADBE Vector Shape - Ellipse") and geom is None:
            geom = p
        elif p.match_name == "ADBE Vector Graphic - Stroke":
            stroke = p
        elif p.match_name == "ADBE Vector Graphic - Fill":
            fill = p
        elif p.match_name == "ADBE Vector Filter - Trim":
            trims.append(p)
    if geom is None:
        ctx.notes.warn(f"shape layer '{L.name}': no supported geometry")
        return None
    if geom.match_name in ("ADBE Vector Shape - Rect", "ADBE Vector Shape - Ellipse") and stroke is not None and fill is None:
        ctx.notes.warn(f"'{L.name}': stroke-only outline (rect/ellipse with Trim Paths) is not representable; skipped")
        return None
    if len(list(p for p in contents.properties if p.match_name.startswith("ADBE Vector Shape"))) > 1:
        ctx.notes.warn(f"shape layer '{L.name}': only the first shape in the group is converted")

    t0 = 0.0
    kind = "rect"
    angle0 = 0.0
    length = thickness = None
    cap_round = False
    if geom.match_name == "ADBE Vector Shape - Group":
        sh = find(geom, "ADBE Vector Shape").value
        v = sh.vertices
        if not sh.closed and len(v) == 2:
            (x0, y0), (x1, y1) = v
            length = math.hypot(x1 - x0, y1 - y0)
            angle0 = math.degrees(math.atan2(y1 - y0, x1 - x0))
            if stroke is None:
                ctx.notes.warn(f"line '{L.name}' has no stroke; skipped")
                return None
            thickness = float(find(stroke, "ADBE Vector Stroke Width").value)
            cap_round = int(find(stroke, "ADBE Vector Stroke Line Cap").value) == 2
            p0, p1 = (x0, y0), (x1, y1)
            geo_kind = "line"
        elif sh.closed and len(v) == 4 and all(not any(t_) for t_ in sh.in_tangents) and all(not any(t_) for t_ in sh.out_tangents):
            xs, ys = [p[0] for p in v], [p[1] for p in v]
            length, thickness = max(xs) - min(xs), max(ys) - min(ys)
            p0, p1 = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2), None
            geo_kind = "box"
        else:
            ctx.notes.warn(f"shape layer '{L.name}': free-form path not representable as rect; skipped")
            return None
    elif geom.match_name == "ADBE Vector Shape - Rect":
        sp, pp_ = find(geom, "ADBE Vector Rect Size"), find(geom, "ADBE Vector Rect Position")
        size = resolve_expr_vec(ctx, sp, L, "Rect Size") or sp.value
        length, thickness = float(size[0]), float(size[1])
        p0 = tuple((resolve_expr_vec(ctx, pp_, L, "Rect Position") or pp_.value)[:2])
        p1 = None
        geo_kind = "box"
    else:
        size = find(geom, "ADBE Vector Ellipse Size").value
        length, thickness = float(size[0]), float(size[1])
        p0, p1 = tuple(find(geom, "ADBE Vector Ellipse Position").value[:2]), None
        geo_kind = "ellipse"
        ctx.notes.warn(f"'{L.name}': ellipse emitted as shapeType 'ellipse' (not seen in the reference template - verify editor support)")

    # colour: Fill effect link > stroke colour (lines) / fill colour
    color, link = resolve_color(ctx, L, ctx.comp)
    if color is None:
        src = stroke if geo_kind == "line" else (fill or stroke)
        cprop = find(src, "ADBE Vector Stroke Color" if src is stroke else "ADBE Vector Fill Color") if src is not None else None
        color = rv(cprop.value, 6) if cprop is not None else [1, 1, 1, 1]
    clip = Clip("shape", L)
    clip.default_color = color
    if link:
        clip.color_links.append(link)
    radius = thickness / 2 if (geo_kind == "line" and cap_round) else 0
    clip.source = {
        "type": "shape",
        "shapeType": "ellipse" if geo_kind == "ellipse" else "rect",
        "width": r(length, 3),
        "height": r(thickness, 3),
        "cornerRadius": r(radius, 3),
        "fillColor": {"r": color[0], "g": color[1], "b": color[2], "a": color[3] if len(color) > 3 else 1.0},
        "strokeWidth": 0,
    }
    clip.size = None
    ctx_ = ctx

    def group_matrix(t):
        if gxf is None:
            return IDENT
        pp, ap = find(gxf, "ADBE Vector Position"), find(gxf, "ADBE Vector Anchor")
        pos, anc = pp.value_at_time(t), ap.value_at_time(t)
        if getattr(pp, "expression_enabled", False) and "anchorPoint" in (getattr(pp, "expression", "") or ""):
            pos = anc  # position is slaved to the anchor -> no net offset
        sc = find(gxf, "ADBE Vector Scale").value_at_time(t)
        rot = find(gxf, "ADBE Vector Rotation").value_at_time(t)
        return trs(pos, rot, (sc[0] / 100.0, sc[1] / 100.0), anc)

    def group_rs(t):
        if gxf is None:
            return 0.0, 1.0, 1.0
        sc = find(gxf, "ADBE Vector Scale").value_at_time(t)
        return find(gxf, "ADBE Vector Rotation").value_at_time(t), sc[0] / 100.0, sc[1] / 100.0

    def trim_range(t):
        s, e, off = 0.0, 1.0, 0.0  # visible fraction of the path, 0..1
        for tr in trims:
            ts = find(tr, "ADBE Vector Trim Start").value_at_time(t)
            te = find(tr, "ADBE Vector Trim End").value_at_time(t)
            to = find(tr, "ADBE Vector Trim Offset").value_at_time(t)
            lo, hi = min(ts, te) / 100.0, max(ts, te) / 100.0
            s, e = s + (e - s) * lo, s + (e - s) * hi
            off += to
        if abs(off) > 1e-6:
            ctx_.notes.warn(f"'{L.name}': Trim Paths offset ignored")
        return s, e

    if trims and geo_kind != "line":
        ctx.notes.warn(f"'{L.name}': Trim Paths on a non-line shape ignored")
        trims = []
    if trims and len(trims) and any(find(tr, "ADBE Vector Trim Type") and int(find(tr, "ADBE Vector Trim Type").value) == 2 for tr in trims):
        ctx.notes.warn(f"'{L.name}': Trim Paths 'Simultaneously/Individually' mode not distinguished")

    def state(t):
        gm = group_matrix(t)
        m = mmul(ctx_.world(L, t), gm)
        if geo_kind == "line":
            s, e = trim_range(t)
            ux, uy = (p1[0] - p0[0]) / length, (p1[1] - p0[1]) / length
            c = ((s + e) / 2.0) * length
            centre = (p0[0] + ux * c, p0[1] + uy * c)
            wfrac = max(e - s, 0.0)
        else:
            centre, wfrac = p0, 1.0
        x, y = ctx_.to_center(*mapply(m, *centre))
        grot, gsx, gsy = group_rs(t)
        rot, sx, sy = ctx_.decomposed(L, t, extra_rot=grot + angle0, extra_scale=(gsx, gsy))
        sx = sx * max(wfrac, ctx_.args.min_scale)
        op = ctx_.tvals(L, t)[4] / 100.0
        vis = 1.0 if (L.in_point - 1e-9 <= t <= L.out_point + 1e-9) else 0.0
        if wfrac <= 0:
            vis = 0.0 if ctx_.args.hide_empty_trim else vis
        return {"pos": [x, y], "rot": rot, "scale": [sx, sy], "opacity": op * vis}

    def box(t):
        gm = group_matrix(t)
        m = mmul(ctx_.world(L, t), gm)
        if geo_kind == "line":
            ux, uy = (p1[0] - p0[0]) / length, (p1[1] - p0[1]) / length
            nx, ny = -uy, ux
            pts = [(p0[0] + nx * thickness / 2, p0[1] + ny * thickness / 2), (p1[0] + nx * thickness / 2, p1[1] + ny * thickness / 2),
                   (p1[0] - nx * thickness / 2, p1[1] - ny * thickness / 2), (p0[0] - nx * thickness / 2, p0[1] - ny * thickness / 2)]
        else:
            hx, hy = length / 2, thickness / 2
            pts = [(p0[0] - hx, p0[1] - hy), (p0[0] + hx, p0[1] - hy), (p0[0] + hx, p0[1] + hy), (p0[0] - hx, p0[1] + hy)]
        return [ctx_.to_center(*mapply(m, *p)) for p in pts]

    clip.state = state
    clip.box_fn = box
    bp = ctx.all_keytimes(L) + [L.in_point, max(L.in_point - 1e-6, 0.0)]
    clip.breakpoints = bp
    clip.aspect = length / max(thickness, 1e-6)
    return clip


def build_solid_clip(ctx: Ctx, L) -> Clip | None:
    src = L.source.main_source
    col = list(getattr(src, "color", [0.5, 0.5, 0.5]))[:3] + [1.0]
    w, h = L.source.width, L.source.height
    clip = Clip("shape", L)
    clip.default_color = rv(col, 6)
    clip.source = {
        "type": "shape", "shapeType": "rect", "width": w, "height": h, "cornerRadius": 0,
        "fillColor": {"r": col[0], "g": col[1], "b": col[2], "a": 1.0}, "strokeWidth": 0,
    }
    ctx_ = ctx

    def state(t):
        m = ctx_.world(L, t)
        x, y = ctx_.to_center(*mapply(m, w / 2, h / 2))
        rot, sx, sy = ctx_.decomposed(L, t)
        op = ctx_.tvals(L, t)[4] / 100.0
        vis = 1.0 if (L.in_point - 1e-9 <= t <= L.out_point + 1e-9) else 0.0
        return {"pos": [x, y], "rot": rot, "scale": [sx, sy], "opacity": op * vis}

    clip.state = state
    clip.box_fn = lambda t: [ctx_.to_center(*mapply(ctx_.world(L, t), *p)) for p in ((0, 0), (w, 0), (w, h), (0, h))]
    clip.breakpoints = ctx.all_keytimes(L) + [L.in_point, max(L.in_point - 1e-6, 0.0)]
    clip.aspect = w / max(h, 1e-6)
    return clip


# --------------------------------------------------------------------------- #
# channel baking + serialisation
# --------------------------------------------------------------------------- #
CHANNELS = (("position", "pos", "pos", 2), ("rotation", "rot", "rot", 1), ("scale", "scale", "scale", 2), ("opacity", "opacity", "opacity", 1))


def bake_clip(ctx: Ctx, clip: Clip, D: float, intro_end: float | None, outro: float, mode: str):
    """-> dict channel -> list of key dicts {t, v, [anchor], [interp, easeIn, easeOut]}  (t in seconds)"""
    fns = {}
    for name, key, tolkey, dim in CHANNELS:
        fns[name] = (lambda k: (lambda t: _round_state(clip.state(t)[k])))(key)
    bps = [t for t in clip.breakpoints if 0 <= t <= D] + [0.0, D]
    out = {}
    for name, key, tolkey, dim in CHANNELS:
        tol = TOL[tolkey]
        fn = fns[name]
        out[name] = [{"t": t, "v": v, "interp": "linear"} for t, v in bake_adaptive(fn, bps, 0.0, D, tol)]
    return out


def _round_state(v):
    return list(v) if isinstance(v, (list, tuple)) else float(v)


def channel_is_static(keys, tolkey):
    return is_static([(k["t"], k["v"]) for k in keys], TOL[tolkey])


def last_change_time(keys, tolkey):
    """Time at which a channel stops changing (end of the intro) or 0."""
    if len(keys) < 2:
        return 0.0
    fin = keys[-1]["v"]
    j = None
    for i, k in enumerate(keys):
        if _err(k["v"], fin, TOL[tolkey]) > 1.0:
            j = i
    return keys[j + 1]["t"] if j is not None and j + 1 < len(keys) else 0.0


def make_outro(clip, name, tolkey, I, O, D, mode):
    """Mirror of the intro: value(D-O+s) = f(I*(1-s/O))."""
    fn_base = lambda t: _round_state(clip.state(t)[dict(position="pos", rotation="rot", scale="scale", opacity="opacity")[name]])
    g = lambda s: fn_base(max(I * (1.0 - s / O), 0.0))
    bps = [O * (1.0 - t / I) for t in clip.breakpoints if 0 <= t <= I] + [0.0, O]
    ks = [{"t": t, "v": v, "interp": "linear"} for t, v in bake_adaptive(g, bps, 0.0, O, TOL[tolkey])]
    for k in ks:
        k["t"] = D - O + k["t"]
        k["anchor"] = "end"
    return ks


def prec(name):
    return {"position": 3, "rotation": 3, "scale": 5, "opacity": 4}[name]


def fmt_val(name, v):
    n = prec(name)
    return [r(x, n) for x in v] if isinstance(v, list) else r(v, n)


def serialise_channel(name, keys, tolkey, flip_rot):
    sgn = -1.0 if (name == "rotation" and flip_rot) else 1.0

    def cv(v):
        if name == "rotation":
            return v * sgn
        return v

    if channel_is_static(keys, tolkey):
        return {"value": fmt_val(name, cv(keys[0]["v"]))}
    out, last_t = [], -1
    for k in keys:
        tus = to_us(k["t"])
        if tus <= last_t:
            tus = last_t + 1
        last_t = tus
        o = {"time": tus, "value": fmt_val(name, cv(k["v"])), "interpolation": "linear"}
        if k.get("anchor"):
            o["anchor"] = k["anchor"]
        out.append(o)
    return {"keyframes": out}


# --------------------------------------------------------------------------- #
# main conversion
# --------------------------------------------------------------------------- #
def convert(path: Path, args) -> tuple[dict, list[str]]:
    notes = Notes()
    with tempfile.TemporaryDirectory() as td:
        definition, comp, wrappers, outer = load_mogrt(path, Path(td))
        ctx = Ctx(comp, args, notes, wrappers, outer, definition)
        D = float(args.duration) if args.duration else float(comp.duration)
        layers = list(comp.layers)

        # -- which layers are mattes, which layers use them
        matte_for = {}
        for L in layers:
            mt = int(L.track_matte_type)
            if mt != 5012 and L.has_track_matte:
                matte_for[id(L)] = (L.track_matte_layer, mt)
        matte_ids = {id(m) for m, _ in matte_for.values()} | {id(L) for L in layers if L.is_track_matte}

        clips: list[Clip] = []
        for L in layers:
            kind = type(L).__name__
            if id(L) in matte_ids or not L.enabled:
                continue
            if L.adjustment_layer or L.null_layer:
                continue
            if kind == "TextLayer":
                ml, mt = matte_for.get(id(L), (None, 5012))
                if mt in (TM_LUMA, TM_LUMA_INV):
                    notes.warn(f"'{L.name}': luma matte not supported; matte ignored")
                    ml = None
                c = build_text_clip(ctx, L, ml, mt, D)
                if ml is not None:
                    notes.warn(f"'{L.name}': track-matte reveal approximated by an opacity ramp (no matte/mask in schema)")
            elif kind == "ShapeLayer":
                if id(L) in matte_for:
                    notes.warn(f"'{L.name}': matte on a shape layer ignored")
                c = build_shape_clip(ctx, L)
            elif kind == "AVLayer" and getattr(L.source, "main_source", None) is not None and type(L.source.main_source).__name__ == "SolidSource":
                c = build_solid_clip(ctx, L)
            else:
                notes.warn(f"layer '{L.name}' ({kind}) skipped")
                continue
            if c is not None:
                clips.append(c)
        if not clips:
            why = ("  [" + "; ".join(notes.items) + "]") if notes.items else ""
            raise SystemExit(f"{path.name}: nothing convertible found in comp '{comp.name}'{why}")

        # -- cross-check my matrix chain against py_aep's own evaluator
        for c in clips[:3]:
            for t in (0.0, D * 0.25, D * 0.9):
                try:
                    ref = c.layer.source_point_to_comp([0.0, 0.0], t)
                    mine = mapply(ctx.world(c.layer, t), 0.0, 0.0)
                    if math.hypot(ref[0] - mine[0], ref[1] - mine[1]) > 0.5:
                        notes.warn(f"transform cross-check differs by >0.5px on '{c.layer.name}' @ {t:.2f}s")
                        break
                except Exception:
                    break

        # -- roles / naming
        texts = [c for c in clips if c.kind == "text"]
        shapes = [c for c in clips if c.kind == "shape"]
        if texts:
            title = max(texts, key=lambda c: c.size)
            ty = title.state(D)["pos"][1]
            above = sorted([c for c in texts if c is not title and c.state(D)["pos"][1] < ty], key=lambda c: c.state(D)["pos"][1], reverse=True)
            below = sorted([c for c in texts if c is not title and c.state(D)["pos"][1] >= ty], key=lambda c: c.state(D)["pos"][1])
            title.role = "title"
            for i, c in enumerate(above):
                c.role = "eyebrow" if i == 0 else f"eyebrow{i + 1}"
            for i, c in enumerate(below):
                c.role = "subtitle" if i == 0 else f"subtitle{i + 1}"
        for i, c in enumerate(shapes):
            c.role = "divider" if (getattr(c, "aspect", 1) >= 10 and sum(1 for s in shapes if s.aspect >= 10) == 1) else (
                "divider" if getattr(c, "aspect", 1) >= 10 and i == 0 else f"shape{i + 1}")
        role_name = {"title": "Main Title Text", "eyebrow": "Eyebrow Text", "subtitle": "Subtitle Text", "divider": "Divider Line"}
        ordered = shapes + sorted(texts, key=lambda c: c.state(D)["pos"][1] if c.role != "title" else 0)
        order_key = {"eyebrow": 0, "title": 1, "subtitle": 2}
        ordered = shapes + sorted(texts, key=lambda c: (order_key.get(re.sub(r"\d+$", "", c.role), 3), c.role))
        # Track order = After Effects stacking order.  comp.layers is top-first, and the editor draws
        # earlier tracks on top (verified from a render), so keep AE's order; --bottom-first flips it.
        _idx = {id(L): i for i, L in enumerate(layers)}
        ordered = sorted(clips, key=lambda c: _idx.get(id(c.layer), 10 ** 6))
        if getattr(args, "bottom_first", False):
            ordered.reverse()

        # -- bake channels
        baked = {}
        for c in clips:
            baked[id(c)] = bake_clip(ctx, c, D, None, 0.0, "sampled")
        intro_end = 0.0
        for c in clips:
            for name, key, tolkey, dim in CHANNELS:
                intro_end = max(intro_end, last_change_time(baked[id(c)][name], tolkey))
        outro = float(args.outro or 0.0)
        if outro > 0 and (intro_end <= 0 or intro_end + outro > D):
            notes.warn("outro skipped: it would not fit after the intro within the duration (raise --duration)")
            outro = 0.0
        if outro > 0:
            for c in clips:
                for name, key, tolkey, dim in CHANNELS:
                    keys = baked[id(c)][name]
                    if channel_is_static(keys, tolkey) and name != "opacity":
                        continue
                    base = [k for k in keys if k["t"] <= intro_end + 1e-9]
                    if not base or base[-1]["t"] < intro_end - 1e-9:
                        base.append({"t": intro_end, "v": c.state(intro_end)[dict(position="pos", rotation="rot", scale="scale", opacity="opacity")[name]], "interp": "linear"})
                    hold_t = D - outro
                    if hold_t > base[-1]["t"] + 1e-6:
                        base.append({"t": hold_t, "v": base[-1]["v"], "interp": "linear"})
                    baked[id(c)][name] = base + make_outro(c, name, tolkey, intro_end, outro, D, "sampled")[1 if abs(base[-1]["t"] - (D - outro)) < 1e-6 else 0:]

        # -- bounds
        xs, ys = [0.0], [0.0]
        sample_ts = sorted({t for c in clips for t in c.breakpoints if 0 <= t <= D} | {0.0, D, intro_end})
        for c in clips:
            for t in sample_ts:
                if c.state(t)["opacity"] > 0.02:
                    for x, y in c.box_fn(t):
                        xs.append(abs(x))
                        ys.append(abs(y))
        pad = args.bounds_padding
        if args.bounds:
            bw, bh = args.bounds
        else:
            bw = int(math.ceil((2 * max(xs) + 2 * pad) / 20.0) * 20)
            bh = int(math.ceil((2 * max(ys) + 2 * pad) / 20.0) * 20)

        # -- clips -> tracks
        tracks = []
        for c in ordered:
            role = c.role
            tname = role_name.get(role) or role_name.get(re.sub(r"\d+$", "", role), role.title())
            if re.search(r"\d+$", role) and re.sub(r"\d+$", "", role) in role_name:
                tname = f"{role_name[re.sub(r'\d+$', '', role)]} {re.search(r'(\d+)$', role).group(1)}"
            props = {}
            for name, key, tolkey, dim in CHANNELS:
                ser = serialise_channel(name, baked[id(c)][name], tolkey, args.flip_rotation)
                if name in ("rotation", "scale") and "value" in ser:
                    v = ser["value"]
                    if (name == "rotation" and abs(v) < 1e-9) or (name == "scale" and all(abs(x - 1) < 1e-9 for x in v)):
                        continue
                props[name if name != "position" else "position"] = ser
            props.update(c.static_props)
            if c.kind == "text":
                ordered_props = {k: props[k] for k in ("position", "rotation", "scale", "opacity") if k in props}
                ordered_props.update({k: props[k] for k in props if k not in ordered_props})
                props = ordered_props
            tracks.append({
                "id": f"trk_{role}", "kind": "video", "name": tname, "muted": False, "locked": False, "hidden": False,
                "clips": [{
                    "id": f"clip_{role}", "start": 0, "duration": to_us(D), "sourceIn": 0, "sourceOut": to_us(D),
                    "speed": 1, "volume": 0, "source": c.source, "properties": props,
                }],
            })

        # -- published params
        params = build_params(definition, comp, ordered, ctx, args, notes)

        cap = definition.get("capsuleName") or path.stem
        cap_clean = re.sub(r"\s*\[.*?\]\s*", " ", cap).strip() or path.stem
        stem = re.sub(r"^[0-9a-f]{8}-", "", path.stem)
        tid = args.id or slug(stem if args.id_from_file else cap)
        tpl = {
            "$schema": SCHEMA_URL,
            "tfve_type": "motion_template",
            "version": 1,
            "id": tid,
            "name": args.name or cap_clean,
            "category": args.category,
            "description": args.description or definition.get("description") or f"Converted from {stem}.mogrt",
            "author": args.author,
            "createdAt": int(time.time() * 1000),
            "duration": to_us(D),
            "responsiveTiming": {"introDuration": to_us(intro_end), "outroDuration": to_us(outro)},
            "bounds": {"width": bw, "height": bh, "originX": 0, "originY": 0},
            "tracks": tracks,
            "publishedParams": params,
        }
        return tpl, notes.items


def build_params(definition, comp, ordered, ctx, args, notes):
    def norm_text(t):
        return (t or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x03", "\n").strip().lower()

    text_clips = [c for c in ordered if c.kind == "text"]
    used = set()
    params = []
    role_title = {"title": "Title", "eyebrow": "Eyebrow", "subtitle": "Subtitle"}

    def rname(role):
        base = re.sub(r"\d+$", "", role)
        num = re.search(r"(\d+)$", role)
        n = role_title.get(base, base.title())
        return f"{n} {num.group(1)}" if num else n

    controls = definition.get("clientControls", [])

    def locstr(ctl, key="uiName"):
        db = (ctl.get(key) or {}).get("strDB") or []
        return [d.get("str", "") for d in db]

    # text controls (emitted in track order, like the reference file)
    text_params = []
    for ctl in controls:
        if ctl.get("type") != 6:
            continue
        default = ((ctl.get("value") or {}).get("strDB") or [{}])[0].get("str", "")
        def_norm = norm_text(default)
        cands = [c for c in text_clips if norm_text(c.source.get("text", "")) == def_norm and id(c) not in used]
        if not cands:
            unused_text = [c for c in text_clips if id(c) not in used]
            if len(unused_text) == 1:
                cands = unused_text
        if not cands:
            notes.warn(f"text control '{locstr(ctl)[-1:]}' ({default!r}) matches no text layer; skipped")
            continue
        c = cands[0]
        used.add(id(c))
        text_params.append((ordered.index(c), {"id": f"param_{c.role}_text", "name": f"{rname(c.role)} Text", "type": "text",
                            "targetClipId": f"clip_{c.role}", "targetProperty": "text.content", "defaultValue": default}))
    params.extend(p for _, p in sorted(text_params, key=lambda x: x[0]))
    for c in ordered:
        if c.kind == "text" and id(c) not in used:
            notes.warn(f"text layer '{c.layer.name}' has no Essential-Graphics text control (not exposed as a param)")
    # font + size (title) -- same params as the reference file
    title = next((c for c in ordered if c.role == "title"), None) or next((c for c in ordered if c.kind == "text"), None)
    if title is not None:
        params.append({"id": "param_font_family", "name": "Font Family", "type": "font", "targetClipId": f"clip_{title.role}",
                       "targetProperty": "text.fontFamily", "defaultValue": title.family})
        if args.multi_target and sum(1 for c in ordered if c.kind == "text") > 1:
            params[-1]["targetClipIds"] = [f"clip_{c.role}" for c in ordered if c.kind == "text"]
        lo = int(title.size * 0.6)
        hi = int(round(title.size * 1.5 / 10.0) * 10)
        params.append({"id": "param_title_size", "name": f"{rname(title.role)} Size", "type": "number", "targetClipId": f"clip_{title.role}",
                       "targetProperty": "text.fontSize", "defaultValue": r(title.size, 2), "min": lo, "max": hi, "step": 1})
    # colour controls -> clips that link to them
    links = {}
    for c in ordered:
        for lname, ename in c.color_links:
            links.setdefault((lname, ename), []).append(c)
    colour_ctls = [c for c in controls if c.get("type") == 4]
    ctl_for = {}
    for (lname, ename), cl in links.items():
        match = next((k for k in colour_ctls if ename in locstr(k) and id(k) not in ctl_for.values()), None)
        if match is None:
            match = next((k for k in colour_ctls if id(k) not in ctl_for.values()), None)
        ctl_for[(lname, ename)] = id(match) if match else None
    seen_ids = {p["id"] for p in params}
    for (lname, ename), cl in links.items():
        shapes = [c for c in cl if c.kind == "shape"]
        txts = [c for c in cl if c.kind == "text"]
        if shapes:
            c0 = shapes[0]
            base = "param_accent_color"
            nm = "Divider Line Color" if c0.role == "divider" else "Accent Color"
            _add_color(params, seen_ids, base, nm, shapes, "shape.fill", c0.default_color, args)
        if txts:
            c0 = next((c for c in txts if c.role == "title"), txts[0])
            _add_color(params, seen_ids, "param_text_color", "Text Color", [c0] + ([] if not args.multi_target else [c for c in txts if c is not c0]), "text.fill", c0.default_color, args)
            if not args.multi_target:
                for c in txts:
                    if c is c0:
                        continue
                    _add_color(params, seen_ids, f"param_{c.role}_color", f"{rname(c.role)} Color", [c], "text.fill", c.default_color, args)
    return params


def _add_color(params, seen, pid, name, clips, target_prop, color, args):
    if pid in seen:
        return
    seen.add(pid)
    p = {"id": pid, "name": name, "type": "color", "targetClipId": f"clip_{clips[0].role}", "targetProperty": target_prop,
         "defaultValue": {"r": color[0], "g": color[1], "b": color[2], "a": color[3] if len(color) > 3 else 1.0}}
    if len(clips) > 1:
        p["targetClipIds"] = [f"clip_{c.role}" for c in clips]
    params.append(p)


# --------------------------------------------------------------------------- #
# validation + CLI
# --------------------------------------------------------------------------- #
def validate(tpl) -> list[str]:
    problems = []
    D = tpl["duration"]
    ids = set()
    for tr in tpl["tracks"]:
        for cl in tr["clips"]:
            if cl["id"] in ids:
                problems.append(f"duplicate clip id {cl['id']}")
            ids.add(cl["id"])
            for pname, prop in cl["properties"].items():
                for k in prop.get("keyframes", []) if isinstance(prop, dict) else []:
                    if not (0 <= k["time"] <= D):
                        problems.append(f"{cl['id']}.{pname}: key time {k['time']} outside [0,{D}]")
                ks = prop.get("keyframes", []) if isinstance(prop, dict) else []
                if any(b["time"] <= a["time"] for a, b in zip(ks, ks[1:])):
                    problems.append(f"{cl['id']}.{pname}: key times not strictly increasing")
    def _bad(o, path=""):
        if isinstance(o, float) and not math.isfinite(o):
            problems.append(f"non-finite number at {path}")
        elif isinstance(o, dict):
            for k, v in o.items():
                _bad(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                _bad(v, f"{path}[{i}]")
    _bad(tpl)
    for tr in tpl["tracks"]:
        for cl in tr["clips"]:
            pr = cl["properties"].get("position")
            pts = [k["value"] for k in pr.get("keyframes", [])] if isinstance(pr, dict) and "keyframes" in pr else ([pr["value"]] if isinstance(pr, dict) and "value" in pr else [])
            if pts and all(abs(x) > 4000 or abs(y) > 4000 for x, y in pts):
                problems.append(f"{cl['id']}: position is always far outside the canvas (e.g. {pts[0]})")
    for p in tpl["publishedParams"]:
        if p["targetClipId"] not in ids:
            problems.append(f"param {p['id']} targets unknown clip {p['targetClipId']}")
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description="Convert .mogrt files to .tfvetemplate", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("inputs", nargs="+", help=".mogrt file(s) or folder(s)")
    ap.add_argument("-o", "--out", help="output file (single input) or directory", default=None)
    ap.add_argument("--id"), ap.add_argument("--name"), ap.add_argument("--description")
    ap.add_argument("--id-from-file", action="store_true", help="derive template id from the file name instead of the capsule name")
    ap.add_argument("--category", default="Titles")
    ap.add_argument("--author", default="TheFreeVideoEditor")
    ap.add_argument("--duration", type=float, help="template length in seconds (default: AE comp length)")
    ap.add_argument("--outro", type=float, default=0.0, help="seconds of mirrored-intro outro appended at the end (0 = none, exact to source)")
    ap.add_argument("--descent-ratio", type=float, default=0.3, help="text box bottom = baseline + ratio*fontSize")
    ap.add_argument("--min-scale", type=float, default=0.001, help="smallest scale used when a trimmed line has zero length")
    ap.add_argument("--hide-empty-trim", action="store_true", help="also drive opacity to 0 while a trimmed line has zero length")
    ap.add_argument("--flip-rotation", action="store_true", help="negate rotation if the editor is counter-clockwise-positive")
    ap.add_argument("--multi-target", action="store_true", help="emit additive 'targetClipIds' so one colour/font param drives several clips")
    ap.add_argument("--bounds", type=int, nargs=2, metavar=("W", "H"))
    ap.add_argument("--bounds-padding", type=float, default=60.0)
    ap.add_argument("--font-dir", help="folder with .ttf/.otf files for exact text-width measurement")
    ap.add_argument("--compact", action="store_true", help="minified JSON")
    ap.add_argument("--bottom-first", action="store_true",
                    help="emit tracks bottom-to-top (use if your editor draws later tracks underneath)")
    ap.add_argument("--canvas", type=int, nargs=2, metavar=("W", "H"), default=[1920, 1080],
                    help="editor canvas; the comp is uniformly scaled to fit it (use the comp's own size for no scaling)")
    ap.add_argument("--text-width-factor", type=float, default=1.0,
                    help="multiply estimated text widths (box sizes follow text); e.g. 0.5 for a very condensed font")
    ap.add_argument("--text-height-ratio", type=float, default=1.0, help="sourceRect height = ratio * font size")
    ap.add_argument("--text-ascent-ratio", type=float, default=0.72, help="sourceRect top = -ratio * font size")
    args = ap.parse_args(argv)

    files = []
    for i in args.inputs:
        p = Path(i)
        files += sorted(p.rglob("*.mogrt")) if p.is_dir() else [p]
    if not files:
        sys.exit("no .mogrt files found")
    outdir = None
    if args.out and (len(files) > 1 or Path(args.out).is_dir() or not args.out.endswith(".tfvetemplate")):
        outdir = Path(args.out)
        outdir.mkdir(parents=True, exist_ok=True)

    rc = 0
    for f in files:
        try:
            tpl, notes = convert(f, args)
        except SystemExit as e:
            print(f"[skip] {e}", file=sys.stderr)
            rc = 1
            continue
        problems = validate(tpl)
        dest = Path(args.out) if (args.out and outdir is None) else (outdir or f.parent) / f"{tpl['id']}.tfvetemplate"
        dest.write_text(json.dumps(tpl, indent=None if args.compact else 2, ensure_ascii=False) + "\n", encoding="utf-8")
        nclips = sum(len(t["clips"]) for t in tpl["tracks"])
        nkeys = sum(len(p.get("keyframes", [])) for t in tpl["tracks"] for c in t["clips"] for p in c["properties"].values() if isinstance(p, dict))
        print(f"[ok]   {f.name} -> {dest}  ({nclips} clips, {nkeys} keyframes, {len(tpl['publishedParams'])} params)")
        for n in notes:
            print(f"       note: {n}")
        for p in problems:
            print(f"       PROBLEM: {p}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())