#!/usr/bin/env python3
"""Clock palette explorer — generate pleasing color schemes from a wall photo or paint color.

Given a JPG of the wall behind a kiosk clock OR a paint color ID
(Sherwin-Williams, Benjamin Moore, Behr), this tool generates
candidate palettes using color science and displays a grid of
miniature clock mockups.  Click to accept or reject each palette.

Usage::

    # From a wall photo:
    python3 tools/clock_palette.py wall_photo.jpg

    # From a Sherwin-Williams paint color:
    python3 tools/clock_palette.py --paint SW6001

    # From a Benjamin Moore color:
    python3 tools/clock_palette.py --paint "BM OC-17"

    # From a Behr color:
    python3 tools/clock_palette.py --paint "BEHR 100A-1"

    # Search paint colors by name:
    python3 tools/clock_palette.py --search "repose gray"

Dependencies: tkinter, Pillow, numpy (no sklearn needed).

Perry Kivolowitz, 2026. MIT License.
"""

__version__ = "1.0"

import argparse
import colorsys
import math
import os
import sys
import tkinter as tk
from datetime import datetime
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageTk

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Number of dominant colors to extract from the wall image.
_N_CLUSTERS: int = 5

# K-means iterations for color extraction.
_KMEANS_ITERS: int = 20

# Clock mockup dimensions (pixels).
_CLOCK_W: int = 200
_CLOCK_H: int = 280

# Grid layout.
_COLS: int = 4

# Minimum contrast ratio (WCAG AA) for text on background.
_MIN_CONTRAST: float = 4.5

# Palette generation strategies.
_STRATEGIES: list[str] = [
    "complementary",
    "analogous_warm",
    "analogous_cool",
    "triadic",
    "split_complementary",
    "monochrome_light",
    "monochrome_dark",
    "accent",
]


# ---------------------------------------------------------------------------
# Paint color catalog
# ---------------------------------------------------------------------------

# Directory containing paint color JSON files (adjacent to this module).
_PAINT_DIR: str = os.path.join(os.path.dirname(__file__), "paint_colors")

# Brand prefixes used in --paint argument.  Maps prefix → JSON filename.
_PAINT_BRANDS: dict[str, str] = {
    "SW": "sherwin-williams.json",
    "BM": "benjamin-moore.json",
    "BEHR": "behr.json",
}


def _load_paint_catalog(brand_file: str) -> list[dict[str, Any]]:
    """Load a paint brand's color catalog from JSON.

    Args:
        brand_file: Filename in the paint_colors directory.

    Returns:
        List of dicts with 'name', 'label', 'hex' keys.
    """
    import json as _json
    path: str = os.path.join(_PAINT_DIR, brand_file)
    if not os.path.exists(path):
        return []
    with open(path, "r") as f:
        return _json.load(f)


def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    """Convert '#RRGGBB' to (R, G, B)."""
    h: str = hex_str.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def lookup_paint(paint_id: str) -> tuple[str, tuple[int, int, int]] | None:
    """Look up a paint color by brand-prefixed ID.

    Supported formats:
        SW6001, SW 6001        — Sherwin-Williams
        BM OC-17, BM AF-5      — Benjamin Moore
        BEHR 100A-1             — Behr

    Args:
        paint_id: Paint color identifier with brand prefix.

    Returns:
        Tuple of (display_name, (R, G, B)) or None if not found.
    """
    paint_id = paint_id.strip().upper()

    # Parse brand prefix.
    brand_prefix: str = ""
    label_query: str = ""
    for prefix in sorted(_PAINT_BRANDS.keys(), key=len, reverse=True):
        if paint_id.startswith(prefix):
            brand_prefix = prefix
            label_query = paint_id[len(prefix):].strip().lstrip("#")
            break

    if not brand_prefix:
        return None

    catalog: list[dict[str, Any]] = _load_paint_catalog(
        _PAINT_BRANDS[brand_prefix],
    )
    if not catalog:
        return None

    # SW labels are numeric ints in the JSON; normalize for comparison.
    for entry in catalog:
        entry_label: str = str(entry.get("label", "")).strip().upper()
        if entry_label == label_query:
            name: str = entry.get("name", "Unknown")
            rgb: tuple[int, int, int] = _hex_to_rgb(entry["hex"])
            brand_name: str = {
                "SW": "Sherwin-Williams",
                "BM": "Benjamin Moore",
                "BEHR": "Behr",
            }.get(brand_prefix, brand_prefix)
            display: str = f"{brand_name} {entry_label} — {name}"
            return display, rgb

    return None


def search_paint(query: str) -> list[tuple[str, str, str, tuple[int, int, int]]]:
    """Search all paint catalogs by name substring.

    Args:
        query: Search string (case-insensitive).

    Returns:
        List of (brand, label, name, (R,G,B)) tuples, max 20 results.
    """
    query_lower: str = query.lower()
    results: list[tuple[str, str, str, tuple[int, int, int]]] = []
    brand_names: dict[str, str] = {
        "SW": "SW",
        "BM": "BM",
        "BEHR": "BEHR",
    }

    for prefix, filename in _PAINT_BRANDS.items():
        catalog = _load_paint_catalog(filename)
        for entry in catalog:
            name: str = entry.get("name", "")
            if query_lower in name.lower():
                rgb = _hex_to_rgb(entry["hex"])
                label = str(entry.get("label", ""))
                results.append((brand_names[prefix], label, name, rgb))
                if len(results) >= 20:
                    return results

    return results


# ---------------------------------------------------------------------------
# Color science utilities
# ---------------------------------------------------------------------------

def _rgb_to_hsl(r: float, g: float, b: float) -> tuple[float, float, float]:
    """Convert RGB (0-1) to HSL (0-1)."""
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return h, s, l


def _hsl_to_rgb(h: float, s: float, l: float) -> tuple[int, int, int]:
    """Convert HSL (0-1) to RGB (0-255)."""
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return int(r * 255), int(g * 255), int(b * 255)


def _relative_luminance(r: int, g: int, b: int) -> float:
    """WCAG relative luminance from RGB (0-255)."""
    def _linearize(c: int) -> float:
        v: float = c / 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * _linearize(r) + 0.7152 * _linearize(g) + 0.0722 * _linearize(b)


def _contrast_ratio(c1: tuple[int, int, int], c2: tuple[int, int, int]) -> float:
    """WCAG contrast ratio between two RGB colors."""
    l1: float = _relative_luminance(*c1) + 0.05
    l2: float = _relative_luminance(*c2) + 0.05
    return max(l1, l2) / min(l1, l2)


def _ensure_contrast(
    fg: tuple[int, int, int],
    bg: tuple[int, int, int],
    min_ratio: float = _MIN_CONTRAST,
) -> tuple[int, int, int]:
    """Adjust fg lightness until it meets contrast ratio against bg."""
    r, g, b = fg
    h, s, l = _rgb_to_hsl(r / 255, g / 255, b / 255)
    bg_lum: float = _relative_luminance(*bg)

    # Try lightening or darkening based on background luminance.
    direction: float = 1.0 if bg_lum < 0.5 else -1.0
    for _ in range(50):
        candidate: tuple[int, int, int] = _hsl_to_rgb(h, s, l)
        if _contrast_ratio(candidate, bg) >= min_ratio:
            return candidate
        l = max(0.0, min(1.0, l + direction * 0.02))

    # Fallback: white on dark, black on light.
    return (255, 255, 255) if bg_lum < 0.5 else (0, 0, 0)


# ---------------------------------------------------------------------------
# K-means color extraction (no sklearn dependency)
# ---------------------------------------------------------------------------

def _kmeans(pixels: np.ndarray, k: int, iters: int = _KMEANS_ITERS) -> np.ndarray:
    """Simple K-means clustering on pixel array.

    Args:
        pixels: (N, 3) array of RGB values (0-255).
        k:      Number of clusters.
        iters:  Number of iterations.

    Returns:
        (k, 3) array of cluster centers (RGB).
    """
    # Initialize centers by sampling random pixels.
    rng = np.random.default_rng(42)
    indices = rng.choice(len(pixels), size=k, replace=False)
    centers: np.ndarray = pixels[indices].astype(np.float64)

    for _ in range(iters):
        # Assign each pixel to nearest center.
        diffs: np.ndarray = pixels[:, None, :].astype(np.float64) - centers[None, :, :]
        dists: np.ndarray = np.sum(diffs ** 2, axis=2)
        labels: np.ndarray = np.argmin(dists, axis=1)

        # Update centers.
        new_centers: np.ndarray = np.zeros_like(centers)
        for j in range(k):
            mask = labels == j
            if mask.any():
                new_centers[j] = pixels[mask].mean(axis=0)
            else:
                new_centers[j] = centers[j]
        centers = new_centers

    return centers.astype(np.uint8)


def extract_dominant_colors(
    image_path: str, n: int = _N_CLUSTERS,
) -> list[tuple[int, int, int]]:
    """Extract dominant colors from an image.

    Args:
        image_path: Path to the image file.
        n:          Number of colors to extract.

    Returns:
        List of (R, G, B) tuples sorted by cluster size (largest first).
    """
    img: Image.Image = Image.open(image_path)
    # Resize for speed — color extraction doesn't need full resolution.
    img = img.resize((150, 150), Image.Resampling.LANCZOS)
    pixels: np.ndarray = np.array(img).reshape(-1, 3)

    # Remove near-black and near-white pixels (shadows, highlights).
    brightness: np.ndarray = pixels.mean(axis=1)
    mask = (brightness > 20) & (brightness < 235)
    filtered: np.ndarray = pixels[mask] if mask.sum() > n * 10 else pixels

    centers: np.ndarray = _kmeans(filtered, n)

    # Sort by how many pixels belong to each cluster.
    diffs = filtered[:, None, :].astype(np.float64) - centers[None, :, :].astype(np.float64)
    dists = np.sum(diffs ** 2, axis=2)
    labels = np.argmin(dists, axis=1)
    counts = [(labels == i).sum() for i in range(n)]
    order = sorted(range(n), key=lambda i: counts[i], reverse=True)

    return [tuple(int(c) for c in centers[i]) for i in order]


# ---------------------------------------------------------------------------
# Palette generation
# ---------------------------------------------------------------------------

def _hue_shift(h: float, degrees: float) -> float:
    """Shift a hue value by degrees (0-360 mapped to 0-1)."""
    return (h + degrees / 360.0) % 1.0


def generate_palettes(
    dominant: list[tuple[int, int, int]],
) -> list[dict[str, Any]]:
    """Generate candidate palettes from dominant wall colors.

    Each palette dict has:
        name:       Strategy name.
        background: RGB tuple for clock background.
        text:       RGB tuple for primary text (time, date).
        accent:     RGB tuple for secondary elements.
        dim:        RGB tuple for less important text.

    Args:
        dominant: List of dominant RGB colors from the wall.

    Returns:
        List of palette dicts.
    """
    palettes: list[dict[str, Any]] = []
    base: tuple[int, int, int] = dominant[0]
    r, g, b = base
    h, s, l = _rgb_to_hsl(r / 255, g / 255, b / 255)

    for strategy in _STRATEGIES:
        bg: tuple[int, int, int]
        text: tuple[int, int, int]
        accent: tuple[int, int, int]
        dim: tuple[int, int, int]

        if strategy == "complementary":
            bg = base
            comp_h: float = _hue_shift(h, 180)
            text = _hsl_to_rgb(comp_h, min(s * 0.8, 0.6), 0.9)
            accent = _hsl_to_rgb(comp_h, min(s, 0.7), 0.6)
            dim = _hsl_to_rgb(h, s * 0.3, 0.6)

        elif strategy == "analogous_warm":
            bg = base
            text = _hsl_to_rgb(_hue_shift(h, 30), min(s * 0.7, 0.5), 0.85)
            accent = _hsl_to_rgb(_hue_shift(h, 60), min(s, 0.6), 0.65)
            dim = _hsl_to_rgb(_hue_shift(h, 15), s * 0.3, 0.55)

        elif strategy == "analogous_cool":
            bg = base
            text = _hsl_to_rgb(_hue_shift(h, -30), min(s * 0.7, 0.5), 0.85)
            accent = _hsl_to_rgb(_hue_shift(h, -60), min(s, 0.6), 0.65)
            dim = _hsl_to_rgb(_hue_shift(h, -15), s * 0.3, 0.55)

        elif strategy == "triadic":
            bg = base
            text = _hsl_to_rgb(_hue_shift(h, 120), min(s * 0.6, 0.5), 0.85)
            accent = _hsl_to_rgb(_hue_shift(h, 240), min(s, 0.6), 0.6)
            dim = _hsl_to_rgb(h, s * 0.2, 0.6)

        elif strategy == "split_complementary":
            bg = base
            text = _hsl_to_rgb(_hue_shift(h, 150), min(s * 0.7, 0.5), 0.85)
            accent = _hsl_to_rgb(_hue_shift(h, 210), min(s, 0.6), 0.6)
            dim = _hsl_to_rgb(h, s * 0.25, 0.55)

        elif strategy == "monochrome_light":
            bg = _hsl_to_rgb(h, s * 0.15, 0.12)
            text = _hsl_to_rgb(h, s * 0.3, 0.92)
            accent = _hsl_to_rgb(h, min(s, 0.5), 0.65)
            dim = _hsl_to_rgb(h, s * 0.2, 0.45)

        elif strategy == "monochrome_dark":
            bg = _hsl_to_rgb(h, s * 0.2, 0.08)
            text = _hsl_to_rgb(h, s * 0.4, 0.85)
            accent = _hsl_to_rgb(h, min(s * 0.8, 0.6), 0.55)
            dim = _hsl_to_rgb(h, s * 0.15, 0.4)

        elif strategy == "accent":
            # Use secondary dominant color as accent.
            sec: tuple[int, int, int] = dominant[1] if len(dominant) > 1 else base
            bg = _hsl_to_rgb(h, s * 0.15, 0.1)
            text = (230, 230, 230)
            accent = sec
            dim = _hsl_to_rgb(h, s * 0.1, 0.4)

        else:
            continue

        # Ensure text is readable on background.
        text = _ensure_contrast(text, bg)
        accent = _ensure_contrast(accent, bg, min_ratio=3.0)

        palettes.append({
            "name": strategy.replace("_", " ").title(),
            "background": bg,
            "text": text,
            "accent": accent,
            "dim": dim,
        })

    # Generate palettes from each secondary dominant color too.
    for i, color in enumerate(dominant[1:3], start=1):
        cr, cg, cb = color
        ch, cs, cl = _rgb_to_hsl(cr / 255, cg / 255, cb / 255)
        bg = _hsl_to_rgb(ch, cs * 0.15, 0.1)
        text = _hsl_to_rgb(ch, cs * 0.3, 0.9)
        accent = color
        dim = _hsl_to_rgb(ch, cs * 0.2, 0.45)
        text = _ensure_contrast(text, bg)
        accent = _ensure_contrast(accent, bg, min_ratio=3.0)
        palettes.append({
            "name": f"Wall Color {i + 1}",
            "background": bg,
            "text": text,
            "accent": accent,
            "dim": dim,
        })

    return palettes


# ---------------------------------------------------------------------------
# Clock mockup renderer
# ---------------------------------------------------------------------------

def render_clock_mockup(
    palette: dict[str, Any],
    width: int = _CLOCK_W,
    height: int = _CLOCK_H,
) -> Image.Image:
    """Render a miniature clock mockup with the given palette.

    Args:
        palette: Palette dict with background, text, accent, dim.
        width:   Image width in pixels.
        height:  Image height in pixels.

    Returns:
        PIL Image of the clock mockup.
    """
    img: Image.Image = Image.new("RGB", (width, height), palette["background"])
    draw: ImageDraw.ImageDraw = ImageDraw.Draw(img)

    # Try to load a decent font; fall back to default.
    time_font: Any = None
    date_font: Any = None
    label_font: Any = None
    try:
        # macOS system fonts.
        for font_path in [
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/SFNSMono.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]:
            if os.path.exists(font_path):
                time_font = ImageFont.truetype(font_path, 42)
                date_font = ImageFont.truetype(font_path, 14)
                label_font = ImageFont.truetype(font_path, 10)
                break
    except Exception:
        pass

    if time_font is None:
        time_font = ImageFont.load_default()
        date_font = time_font
        label_font = time_font

    now = datetime.now()

    # Time.
    time_str: str = now.strftime("%I:%M")
    time_bbox = draw.textbbox((0, 0), time_str, font=time_font)
    tw: int = time_bbox[2] - time_bbox[0]
    x: int = (width - tw) // 2
    draw.text((x, 40), time_str, fill=palette["text"], font=time_font)

    # Date.
    date_str: str = now.strftime("%A, %B %d")
    date_bbox = draw.textbbox((0, 0), date_str, font=date_font)
    dw: int = date_bbox[2] - date_bbox[0]
    draw.text(((width - dw) // 2, 95), date_str, fill=palette["accent"], font=date_font)

    # Separator line.
    draw.line(
        [(20, 120), (width - 20, 120)],
        fill=palette["dim"], width=1,
    )

    # Fake weather tile.
    draw.text((20, 135), "72\u00b0F  Partly Cloudy", fill=palette["text"], font=date_font)

    # Fake sensor tile.
    draw.text((20, 160), "Humidity: 55%", fill=palette["dim"], font=date_font)

    # Separator.
    draw.line(
        [(20, 185), (width - 20, 185)],
        fill=palette["dim"], width=1,
    )

    # Fake status items.
    draw.text((20, 195), "Front Door: Locked", fill=palette["accent"], font=date_font)
    draw.text((20, 215), "Cameras: 3 Online", fill=palette["dim"], font=date_font)

    # Palette name label at bottom.
    draw.text(
        (10, height - 18), palette["name"],
        fill=palette["dim"], font=label_font,
    )

    # Color swatches at bottom right.
    swatch_y: int = height - 16
    for i, key in enumerate(["text", "accent", "dim"]):
        sx: int = width - 50 + i * 15
        draw.rectangle(
            [(sx, swatch_y), (sx + 10, swatch_y + 10)],
            fill=palette[key], outline=palette["dim"],
        )

    return img


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class PaletteExplorer:
    """Tkinter GUI for exploring clock palettes.

    Displays a grid of clock mockups. Click a mockup to accept
    the palette (prints to stdout). Right-click to reject it
    (removes from grid).

    Accepts either a wall photo path OR a pre-resolved paint color.

    Args:
        image_path: Path to the wall photo (or None if using paint_color).
        paint_color: Tuple of (display_name, (R,G,B)) from paint lookup.
    """

    def __init__(
        self,
        image_path: str | None = None,
        paint_color: tuple[str, tuple[int, int, int]] | None = None,
    ) -> None:
        """Initialize the palette explorer."""
        self._root: tk.Tk = tk.Tk()
        self._root.configure(bg="#1a1a1a")

        if paint_color is not None:
            # Paint color mode — use the single color as dominant.
            display_name, rgb = paint_color
            self._root.title(f"Clock Palette Explorer \u2014 {display_name}")
            print(f"Paint color: {display_name} = #{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}")
            # Generate a set of related colors by varying lightness.
            h, s, l = _rgb_to_hsl(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)
            self._dominant: list[tuple[int, int, int]] = [
                rgb,
                _hsl_to_rgb(h, max(s * 0.6, 0.1), max(l - 0.15, 0.05)),
                _hsl_to_rgb(h, max(s * 0.4, 0.05), min(l + 0.15, 0.95)),
                _hsl_to_rgb(_hue_shift(h, 30), s * 0.5, l),
                _hsl_to_rgb(_hue_shift(h, -30), s * 0.5, l),
            ]
        elif image_path is not None:
            # Photo mode — extract dominant colors.
            self._root.title(
                f"Clock Palette Explorer \u2014 {os.path.basename(image_path)}",
            )
            print(f"Extracting colors from {image_path}...")
            self._dominant = extract_dominant_colors(image_path)
        else:
            raise ValueError("Either image_path or paint_color is required")

        print(f"Dominant colors: {self._dominant}")
        self._palettes: list[dict[str, Any]] = generate_palettes(self._dominant)
        print(f"Generated {len(self._palettes)} palettes")

        # Track photo references to prevent GC.
        self._photos: list[ImageTk.PhotoImage] = []
        self._frames: list[tk.Frame] = []

        self._build_ui()

    def _build_ui(self) -> None:
        """Build the palette grid UI."""
        # Instructions.
        instr = tk.Label(
            self._root,
            text="Left-click: ACCEPT palette  |  Right-click: REJECT  |  Esc: Quit",
            bg="#1a1a1a", fg="#888888",
            font=("Helvetica", 12),
        )
        instr.pack(pady=8)

        # Dominant colors display.
        swatch_frame = tk.Frame(self._root, bg="#1a1a1a")
        swatch_frame.pack(pady=4)
        tk.Label(
            swatch_frame, text="Wall colors: ",
            bg="#1a1a1a", fg="#666666",
            font=("Helvetica", 10),
        ).pack(side=tk.LEFT)
        for color in self._dominant:
            hex_color: str = f"#{color[0]:02x}{color[1]:02x}{color[2]:02x}"
            swatch = tk.Label(
                swatch_frame, text="  ",
                bg=hex_color, width=4, height=1,
                relief=tk.RAISED,
            )
            swatch.pack(side=tk.LEFT, padx=2)

        # Scrollable grid.
        canvas = tk.Canvas(self._root, bg="#1a1a1a", highlightthickness=0)
        scrollbar = tk.Scrollbar(
            self._root, orient=tk.VERTICAL, command=canvas.yview,
        )
        self._grid_frame = tk.Frame(canvas, bg="#1a1a1a")

        self._grid_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=self._grid_frame, anchor=tk.NW)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Render mockups into grid.
        for i, palette in enumerate(self._palettes):
            self._add_mockup(i, palette)

        # Key bindings.
        self._root.bind("<Escape>", lambda e: self._root.destroy())

        # Size window to fit.
        cols: int = min(_COLS, len(self._palettes))
        rows: int = math.ceil(len(self._palettes) / _COLS)
        win_w: int = cols * (_CLOCK_W + 20) + 40
        win_h: int = min(rows * (_CLOCK_H + 20) + 80, 900)
        self._root.geometry(f"{win_w}x{win_h}")

    def _add_mockup(self, index: int, palette: dict[str, Any]) -> None:
        """Add a clock mockup to the grid."""
        row: int = index // _COLS
        col: int = index % _COLS

        mockup: Image.Image = render_clock_mockup(palette)
        photo: ImageTk.PhotoImage = ImageTk.PhotoImage(mockup)
        self._photos.append(photo)

        frame = tk.Frame(
            self._grid_frame, bg="#333333",
            padx=2, pady=2, relief=tk.RAISED, bd=1,
        )
        frame.grid(row=row, column=col, padx=6, pady=6)
        self._frames.append(frame)

        label = tk.Label(frame, image=photo, bg="#1a1a1a")
        label.pack()

        # Left-click: accept.
        label.bind("<Button-1>", lambda e, p=palette: self._accept(p))
        # Right-click: reject.
        label.bind("<Button-2>", lambda e, f=frame: self._reject(f))
        label.bind("<Button-3>", lambda e, f=frame: self._reject(f))

    def _accept(self, palette: dict[str, Any]) -> None:
        """Accept a palette — print config and exit."""
        def _hex(rgb: tuple[int, int, int]) -> str:
            return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"

        print("\n" + "=" * 50)
        print(f"ACCEPTED: {palette['name']}")
        print("=" * 50)
        print(f"  background: {_hex(palette['background'])}")
        print(f"  text:       {_hex(palette['text'])}")
        print(f"  accent:     {_hex(palette['accent'])}")
        print(f"  dim:        {_hex(palette['dim'])}")
        print()
        print("CSS variables:")
        print(f"  --clock-bg: {_hex(palette['background'])};")
        print(f"  --clock-text: {_hex(palette['text'])};")
        print(f"  --clock-accent: {_hex(palette['accent'])};")
        print(f"  --clock-dim: {_hex(palette['dim'])};")
        print("=" * 50)

        self._root.destroy()

    def _reject(self, frame: tk.Frame) -> None:
        """Reject a palette — remove from grid."""
        frame.destroy()

    def run(self) -> None:
        """Run the tkinter main loop."""
        self._root.mainloop()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Parse args and launch the palette explorer."""
    parser = argparse.ArgumentParser(
        description="Generate clock color palettes from a wall photo or paint color",
    )
    parser.add_argument(
        "image", type=str, nargs="?", default=None,
        help="Path to a JPG/PNG photo of the wall behind the clock",
    )
    parser.add_argument(
        "--paint", type=str, default=None,
        help=(
            "Paint color ID instead of a photo. "
            "Examples: SW6001, 'BM OC-17', 'BEHR 100A-1'"
        ),
    )
    parser.add_argument(
        "--search", type=str, default=None,
        help="Search paint catalogs by name (e.g., 'repose gray')",
    )
    args = parser.parse_args()

    # Search mode — print results and exit.
    if args.search is not None:
        results = search_paint(args.search)
        if not results:
            print(f"No paint colors matching '{args.search}'")
            sys.exit(1)
        print(f"\nPaint colors matching '{args.search}':\n")
        for brand, label, name, rgb in results:
            hex_str: str = f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
            print(f"  {brand} {label:>8s}  {hex_str}  {name}")
        print(f"\nUse --paint to explore palettes, e.g.:")
        print(f"  python3 tools/clock_palette.py --paint '{results[0][0]} {results[0][1]}'")
        sys.exit(0)

    # Paint mode — look up color and launch explorer.
    if args.paint is not None:
        result = lookup_paint(args.paint)
        if result is None:
            print(f"Error: paint color '{args.paint}' not found", file=sys.stderr)
            print("Try --search to find colors by name", file=sys.stderr)
            sys.exit(1)
        explorer = PaletteExplorer(paint_color=result)
        explorer.run()
        return

    # Photo mode — extract colors from image.
    if args.image is None:
        parser.print_help()
        sys.exit(1)

    if not os.path.exists(args.image):
        print(f"Error: file not found: {args.image}", file=sys.stderr)
        sys.exit(1)

    explorer = PaletteExplorer(image_path=args.image)
    explorer.run()


if __name__ == "__main__":
    main()
