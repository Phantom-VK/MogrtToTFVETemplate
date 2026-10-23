# MogrtToTFVETemplate

Convert Adobe After Effects Motion Graphics Templates (`.mogrt`) into [The Free Video Editor](https://thefreevideoeditor.com) motion template format (`.tfvetemplate`).

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## Overview

A `.mogrt` package is a zip bundle containing:
- `definition.json`: Essential Graphics published controls and metadata.
- `project.aegraphic`: A zipped After Effects project file (`.aep`) containing all layers, expressions, shapes, and keyframes.
- `thumb.png`: Template preview thumbnail.

**MogrtToTFVETemplate** parses the underlying `.aep` project via `py-aep`, evaluates the complete layer transform chain (including parent-child relationships and nested pre-comps), bakes Bézier-eased animation into adaptive linear keyframes, translates shapes and typography, and publishes Essential Graphics controls into The Free Video Editor schema (`motion-template-v1`).

---

## Key Features

- **Full Transform Hierarchy Evaluation**: Resolves parent null chains, anchor points, position, rotation, scale, and opacity in comp-space coordinates.
- **Adaptive Keyframe Sampling**: Faithfully reproduces AE's Bézier easing curves by adaptively sampling keyframes within strict tolerances ($\le 0.2\,\text{px}$, $\le 0.2^\circ$, $\le 0.15\%$ scale) without relying on renderer-specific Bézier handle interpretations.
- **Accurate Typography & Alignment**:
  - Handles font family, weight, tracking, font size, leading, all-caps styling, and baseline shifts.
  - Pixel-accurate point text and paragraph/box text placement matching The Free Video Editor's `layerGeometry.ts` engine (`alignment` and `verticalAnchor: "bottom"`).
  - Per-font baseline compensation: the editor draws the baseline about $0.25$ to $0.35 \times \text{fontSize}$ above the bottom of a text box, depending on the font. The converter uses the value measured for each font the editor bundles (`--descent-ratio` overrides it; $0.3$ is used for any other font).
  - A text layer's scale is folded into `text.fontSize` and `text.letterSpacing`. The editor rasterizes text at `fontSize` × `scale` and then applies the scale to the picture again, so a text clip keeps its scale at $1$ and is drawn at the size After Effects draws it.
  - Font substitution with `--font-map`: a font the editor does not have is drawn in a bundled one, with the weight snapped to one the font file really has (any other weight is drawn as synthetic bold, which is wider and taller than the real glyphs).
- **Expression Evaluation for Shapes**:
  - Automatically parses and resolves dynamic expressions linked to text bounding boxes (`s.sourceRectAtTime().width`, `top`, `left`, etc.), allowing background boxes (`[w+200, h+10]`, `[w+l, h+t]`) to accurately frame text.
  - `sourceRectAtTime()` is the box around the glyph outlines, not the advance box. With the font file available (`--font-dir`) the converter measures the outlines, so a box sized from capitals is $0.72 \times \text{fontSize}$ tall and starts at the first glyph's edge.
- **Shape & Trim Paths Support**:
  - Converts rectangles, ellipses, and stroked lines.
  - Converts 2-point stroked lines animated with Trim Paths into animated rect clips.
- **Essential Graphics Controls**:
  - Extracts text inputs, font styling, and color controls into `publishedParams`.
  - Normalizes text matching case-insensitively so default values accurately link to target text layers.
- **Pre-comp Unwrapping & Canvas Rescaling**:
  - Automatically follows nested pre-comps (e.g. 4K $3840 \times 2160$ assets) down to artwork layers while applying wrapper transforms to fit target editor resolutions (e.g. 1080p $1920 \times 1080$).

---

## Installation

### Prerequisites
- Python 3.10 or higher
- `pip`

### Setup

```bash
# Clone the repository
git clone https://github.com/GajananPalepwad/MogrtToTFVETemplate.git
cd MogrtToTFVETemplate

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

---

## Usage

### Single File Conversion

```bash
python converter.py mixkit-305.mogrt -o out/lowerthird-01.tfvetemplate \
  --duration 5 \
  --font-map "AcuminPro=Bebas Neue" \
  --font-dir /path/to/TheFreeVideoEditor/public/fonts
```

This is the command that produced `out/lowerthird-01.tfvetemplate`, and `mixkit-305-preview.mp4` is the look it targets.
The font options are explained in [Fonts and Text Measurement](#fonts-and-text-measurement).

### Batch Directory Conversion

```bash
python converter.py ./mogrts/ -o ./out/
```

### Custom Template Options

```bash
python converter.py title.mogrt \
  -o out/title.tfvetemplate \
  --name "Minimal Lower Third" \
  --category "Lower Thirds" \
  --duration 6.0 \
  --outro 1.5 \
  --canvas 1920 1080
```

---

## CLI Options Reference

| Argument | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `inputs` | `str...` | *(required)* | One or more `.mogrt` files or directory paths. |
| `-o, --out` | `str` | `None` | Output file path (single file) or destination directory. |
| `--name` | `str` | Mogrt name | Display name in the template metadata. |
| `--category` | `str` | `Titles` | Template category (e.g., `Titles`, `Lower Thirds`, `Social`). |
| `--author` | `str` | `TheFreeVideoEditor` | Author name in metadata. |
| `--duration` | `float` | Comp length | Target template duration in seconds. |
| `--outro` | `float` | `0.0` | Seconds of mirrored intro animation appended at the end as an outro. |
| `--canvas` | `int int` | `1920 1080` | Target editor canvas size; comp is uniformly scaled to fit. |
| `--descent-ratio` | `float` | per font | Ratio of font size between baseline and box bottom. Default: the value measured for the editor font (`0.35` for Bebas Neue), else `0.3`. |
| `--font-map` | `FROM=TO` | `None` | Draw an After Effects font in an editor font, e.g. `"AcuminPro=Bebas Neue"`. Repeatable. |
| `--font-dir` | `str` | `None` | Directory containing `.ttf`/`.otf` files (e.g. the editor's `public/fonts`) for exact glyph widths and heights. Files are found by PostScript or family name. |
| `--text-width-factor` | `float` | `1.0` | Multiplier for estimated text widths (for fine-tuning custom condensed fonts). |
| `--multi-target` | `flag` | `False` | Group multiple clips under shared published parameters when linking colors. |
| `--bottom-first` | `flag` | `False` | Reverse track order if target compositor renders earlier tracks underneath. |
| `--compact` | `flag` | `False` | Minify output JSON. |

---

## Fonts and Text Measurement

Text boxes, and shapes sized from text (`[w+200, h+10]`), are only as accurate as the text measurement. The editor draws text in the fonts it bundles: Bebas Neue, Inter, Montserrat, Playfair Display, Roboto Mono and Caveat. Any other font is drawn in the browser's fallback font (a serif), and the converter prints a note.

1. Pick the editor font that looks like the source font with `--font-map "FROM=TO"`. `FROM` is the After Effects family or PostScript name (`AcuminPro` matches `AcuminPro-Bold`).
2. Give `--font-dir` the folder with that font's file so widths and heights come from the real glyphs. Without it the converter falls back to a width heuristic for a generic wide font, which is far too wide for a condensed face (`--text-width-factor` corrects it by hand).

`mixkit-305.mogrt` asks for Acumin Pro, which the editor does not have. The preview video is drawn in a condensed face that matches Bebas Neue, so the sample uses `--font-map "AcuminPro=Bebas Neue"`.

---

## Testing

```bash
python -m unittest discover -s tests -v
```

`tests/test_reference_geometry.py` converts `mixkit-305.mogrt` and compares element positions and sizes at 2.0 s with numbers measured on the same frame of `mixkit-305-preview.mp4` (in 1920×1080 pixels, within 5 px). The tests that need Bebas Neue are skipped unless the editor's fonts folder is found: set `TFVE_FONT_DIR`, or keep a `TheFreeVideoEditor` checkout next to this repository.

---

## Technical Architecture & Coordinate Mapping

### Coordinate System
- **After Effects**: Origin $(0, 0)$ is at the top-left of the comp canvas, $+X$ right, $+Y$ down.
- **The Free Video Editor**: `transform.x` and `transform.y` are in pixels relative to the **canvas center** ($(0, 0)$ at center, $+X$ right, $+Y$ down).
- Point text in After Effects defines its layer origin at the first line's baseline. For left-aligned text, $X=0$ is the left edge; for center-aligned, $X=0$ is the center; for right-aligned, $X=0$ is the right edge.
- In The Free Video Editor renderer (`src/editor/core/layerGeometry.ts`), `alignment: "left"` places the left edge at `transform.x`, and `verticalAnchor: "bottom"` places the box bottom at `transform.y`. `converter.py` maps these coordinate systems with sub-pixel precision.
- The editor rasterizes a text clip at `text.fontSize` × `scale` and then applies the scale again, and it draws the baseline a font-dependent distance above the box bottom ($0.35 \times \text{fontSize}$ for Bebas Neue). The converter compensates for both, which is why text clips leave `scale` at $1$.

---

## Diagnostics & Inspection

Use the included `inspect_mogrt.py` utility to inspect layer hierarchies, property expressions, and keyframes inside any `.mogrt`:

```bash
python inspect_mogrt.py sample.mogrt
```

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
