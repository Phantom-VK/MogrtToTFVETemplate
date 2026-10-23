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
  - Exact baseline descent compensation ($0.3 \times \text{fontSize}$) to prevent vertical drift.
- **Expression Evaluation for Shapes**:
  - Automatically parses and resolves dynamic expressions linked to text bounding boxes (`s.sourceRectAtTime().width`, `top`, `left`, etc.), allowing background boxes (`[w+200, h+10]`, `[w+l, h+t]`) to accurately frame text.
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
python converter.py mixkit-305.mogrt -o out/lowerthird-01.tfvetemplate
```

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
| `--descent-ratio` | `float` | `0.3` | Ratio of font size between baseline and box bottom (`0.3` for TFVE layout). |
| `--font-dir` | `str` | `None` | Directory containing local `.ttf`/`.otf` files for exact glyph width measurement. |
| `--text-width-factor` | `float` | `1.0` | Multiplier for estimated text widths (for fine-tuning custom condensed fonts). |
| `--multi-target` | `flag` | `False` | Group multiple clips under shared published parameters when linking colors. |
| `--bottom-first` | `flag` | `False` | Reverse track order if target compositor renders earlier tracks underneath. |
| `--compact` | `flag` | `False` | Minify output JSON. |

---

## Technical Architecture & Coordinate Mapping

### Coordinate System
- **After Effects**: Origin $(0, 0)$ is at the top-left of the comp canvas, $+X$ right, $+Y$ down.
- **The Free Video Editor**: `transform.x` and `transform.y` are in pixels relative to the **canvas center** ($(0, 0)$ at center, $+X$ right, $+Y$ down).
- Point text in After Effects defines its layer origin at the first line's baseline. For left-aligned text, $X=0$ is the left edge; for center-aligned, $X=0$ is the center; for right-aligned, $X=0$ is the right edge.
- In The Free Video Editor renderer (`src/editor/core/layerGeometry.ts`), `alignment: "left"` places the left edge at `transform.x`, and `verticalAnchor: "bottom"` places the box bottom at `transform.y`. `converter.py` maps these coordinate systems with sub-pixel precision.

---

## Diagnostics & Inspection

Use the included `inspect_mogrt.py` utility to inspect layer hierarchies, property expressions, and keyframes inside any `.mogrt`:

```bash
python inspect_mogrt.py sample.mogrt
```

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
