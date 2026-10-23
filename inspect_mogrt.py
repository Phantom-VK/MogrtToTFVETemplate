#!/usr/bin/env python3
"""Dump the layer structure of a .mogrt so converter mismatches can be diagnosed.
    python inspect_mogrt.py mixkit-305.mogrt > mogrt_dump.txt
"""
import sys, zipfile, tempfile
from pathlib import Path
import py_aep

def find(g, mn):
    for p in getattr(g, "properties", []):
        if p.match_name == mn:
            return p

def walk(g, depth=0):
    for p in g.properties:
        yield p, depth
        if hasattr(p, "properties"):
            yield from walk(p, depth + 1)

def val(p, t=0.0):
    try:
        return p.value_at_time(t)
    except Exception:
        try:
            return p.value
        except Exception as e:
            return f"<{type(e).__name__}>"

def main(path):
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(path) as z:
            blob = z.read(next(n for n in z.namelist() if n.lower().endswith(".aegraphic")))
        inner = Path(td) / "i.aegraphic"; inner.write_bytes(blob)
        with zipfile.ZipFile(inner) as z2:
            aep = next(n for n in z2.namelist() if n.lower().endswith(".aep"))
            ap = Path(td) / "p.aep"; ap.write_bytes(z2.read(aep))
        app = py_aep.parse(str(ap))
    for c in app.project.compositions:
        print(f"\n=== COMP '{c.name}'  {c.width}x{c.height}  dur={c.duration}  fps={getattr(c,'frame_rate',None)}")
        for L in c.layers:
            src = getattr(L, "source", None)
            tg = find(L, "ADBE Transform Group")
            print(f"\n- [{type(L).__name__}] '{L.name}' enabled={L.enabled} parent={getattr(L.parent,'name',None)} "
                  f"in={L.in_point:.3f} out={L.out_point:.3f} start={getattr(L,'start_time',None)} "
                  f"src={type(src).__name__ if src is not None else None} "
                  f"{getattr(src,'width',None)}x{getattr(src,'height',None)} "
                  f"guide={getattr(L,'guide_layer',None)} 3d={getattr(L,'three_d_layer',None)}")
            if tg is not None:
                print("    transform children:", [(c.match_name, bool(getattr(c, 'expression_enabled', False)),
                      (getattr(c, 'expression', '') or '')[:90]) for c in tg.properties])
                for mn in ("ADBE Anchor Point", "ADBE Position", "ADBE Scale", "ADBE Rotate Z", "ADBE Opacity"):
                    p = find(tg, mn)
                    if p is None: continue
                    nk = len(getattr(p, "keyframes", []) or [])
                    ex = getattr(p, "expression", "") if getattr(p, "expression_enabled", False) else ""
                    print(f"    {mn:20s} t0={val(p,0.0)}  t_end={val(p,min(c.duration-0.01,4.0))}  keys={nk}  expr={ex!r}")
            for p, d in walk(L):
                if p.match_name in ("ADBE Vector Rect Size", "ADBE Vector Rect Position", "ADBE Vector Scale",
                                    "ADBE Vector Position", "ADBE Vector Anchor", "ADBE Vector Trim End",
                                    "ADBE Vector Trim Start"):
                    nk = len(getattr(p, "keyframes", []) or [])
                    ex = getattr(p, "expression", "") if getattr(p, "expression_enabled", False) else ""
                    print(f"    {'  '*d}{p.match_name:26s} t0={val(p,0.0)} t_end={val(p,3.0)} keys={nk} expr={ex!r}")

if __name__ == "__main__":
    main(sys.argv[1])