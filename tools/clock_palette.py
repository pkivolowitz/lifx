#!/usr/bin/env python3
"""Clock palette explorer — generate pleasing color schemes from a wall photo.

Given a JPG of the wall behind a kiosk clock, this tool extracts
dominant colors, generates candidate palettes using color science
(complementary, analogous, triadic, split-complementary, monochrome),
and displays a grid of miniature clock mockups.  Click to accept or
reject each palette.

Usage::

    python3 tools/clock_palette.py /path/to/wall_photo.jpg

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

    Args:
        image_path: Path to the wall photo.
    """

    def __init__(self, image_path: str) -> None:
        """Initialize the palette explorer."""
        self._image_path: str = image_path
        self._root: tk.Tk = tk.Tk()
        self._root.title(f"Clock Palette Explorer \u2014 {os.path.basename(image_path)}")
        self._root.configure(bg="#1a1a1a")

        # Extract colors and generate palettes.
        print(f"Extracting colors from {image_path}...")
        self._dominant: list[tuple[int, int, int]] = extract_dominant_colors(image_path)
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
        description="Generate clock color palettes from a wall photo",
    )
    parser.add_argument(
        "image", type=str,
        help="Path to a JPG/PNG photo of the wall behind the clock",
    )
    args = parser.parse_args()

    if not os.path.exists(args.image):
        print(f"Error: file not found: {args.image}", file=sys.stderr)
        sys.exit(1)

    explorer = PaletteExplorer(args.image)
    explorer.run()


if __name__ == "__main__":
    main()
