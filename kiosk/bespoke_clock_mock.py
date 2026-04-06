"""Standalone pygame mock for a bespoke congregation clock.

This module intentionally avoids HTTP and external dependencies beyond
pygame. It renders a synagogue-style wall clock layout with mock data so
the composition can be explored before any hardware deployment.

Usage::

    python -m kiosk.bespoke_clock_mock --windowed
    python -m kiosk.bespoke_clock_mock --name CoM
"""

__version__: str = "1.0"

import argparse
import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime

import pygame
from PIL import Image, ImageDraw, ImageFont


FPS: int = 20
DEFAULT_WIDTH: int = 1366
DEFAULT_HEIGHT: int = 768
WINDOWED_WIDTH: int = 1400
WINDOWED_HEIGHT: int = 900
BORDER_RADIUS: int = 24
WOOD_STRIPE_HEIGHT: int = 14
PANEL_INSET: int = 28
CLOCK_TICK_COUNT: int = 12

BACKGROUND_TOP: tuple[int, int, int] = (72, 45, 27)
BACKGROUND_BOTTOM: tuple[int, int, int] = (41, 24, 14)
FRAME_OUTER: tuple[int, int, int] = (89, 88, 79)
FRAME_INNER: tuple[int, int, int] = (44, 25, 12)
BRASS_LIGHT: tuple[int, int, int] = (236, 212, 133)
BRASS_MID: tuple[int, int, int] = (194, 152, 57)
BRASS_DARK: tuple[int, int, int] = (102, 64, 18)
TEXT_IVORY: tuple[int, int, int] = (250, 235, 197)
TEXT_GOLD: tuple[int, int, int] = (241, 193, 82)
TEXT_AMBER: tuple[int, int, int] = (245, 176, 44)
SHADOW: tuple[int, int, int, int] = (0, 0, 0, 90)
PANEL_BG: tuple[int, int, int] = (74, 39, 23)
PANEL_EDGE: tuple[int, int, int] = (22, 10, 4)
FOOTER_BG: tuple[int, int, int] = (58, 28, 15)
CLOCK_FACE: tuple[int, int, int] = (239, 214, 144)
CLOCK_FACE_DARK: tuple[int, int, int] = (197, 156, 74)
HAND_COLOR: tuple[int, int, int] = (83, 24, 14)
TEXT_DIM: tuple[int, int, int] = (201, 177, 122)
TEXT_SUBDUED: tuple[int, int, int] = (166, 129, 79)
MOBILE_LATITUDE: float = 30.6954
MOBILE_LONGITUDE: float = -88.0399
TIMEZONE_ID: str = "America/Chicago"
HEBCAL_POLL_SECONDS: int = 15 * 60
HTTP_TIMEOUT_SECONDS: float = 10.0
DATE_FORMAT: str = "%b %d, %Y"
QR_ASSET_DIR: str = os.path.join(os.path.dirname(__file__), "assets")
QR_CODES: tuple[tuple[str, str], ...] = (
    ("Zelle", os.path.join(QR_ASSET_DIR, "donation_zelle.jpeg")),
    ("Cash App", os.path.join(QR_ASSET_DIR, "donation_cashapp.jpeg")),
    ("PayPal", os.path.join(QR_ASSET_DIR, "donation_paypal.jpeg")),
)

ZMANIM_ROWS: tuple[tuple[str, str], ...] = (
    ("alotHaShachar", "Dawn"),
    ("sunrise", "Sunrise"),
    ("sofZmanShma", "Latest Shema"),
    ("sofZmanTfilla", "Latest Tefillah"),
    ("chatzot", "Midday"),
    ("minchaGedola", "Mincha Gedola"),
    ("plagHaMincha", "Plag HaMincha"),
    ("sunset", "Sunset"),
    ("tzeit7083deg", "Nightfall"),
)


@dataclass(frozen=True)
class ScheduleEntry:
    """One row in the schedule panels."""

    label: str
    time_text: str
    color: tuple[int, int, int] = TEXT_IVORY


@dataclass(frozen=True)
class HebcalSnapshot:
    """Display data resolved from Hebcal for one screen refresh window."""

    hebrew_date: str
    left_panel: tuple[ScheduleEntry, ...]
    right_panel: tuple[ScheduleEntry, ...]
    detail_lines: tuple[str, ...]
    loaded_at: datetime | None
    stale: bool


DEFAULT_SNAPSHOT: HebcalSnapshot = HebcalSnapshot(
    hebrew_date="Loading...",
    left_panel=(
        ScheduleEntry("Dawn", "--:--", TEXT_AMBER),
        ScheduleEntry("Sunrise", "--:--"),
        ScheduleEntry("Latest Shema", "--:--"),
        ScheduleEntry("Latest Tefillah", "--:--"),
        ScheduleEntry("Midday", "--:--"),
        ScheduleEntry("Mincha Gedola", "--:--"),
        ScheduleEntry("Plag HaMincha", "--:--"),
        ScheduleEntry("Sunset", "--:--"),
        ScheduleEntry("Nightfall", "--:--"),
    ),
    right_panel=(
        ScheduleEntry("Candle Lighting", "--:--", TEXT_AMBER),
        ScheduleEntry("Havdalah", "--:--"),
        ScheduleEntry("Parasha", "Loading..."),
        ScheduleEntry("Holiday", "Loading..."),
        ScheduleEntry("Location", "Mobile, AL"),
    ),
    detail_lines=("Connecting to Hebcal",),
    loaded_at=None,
    stale=False,
)

_snapshot_lock = threading.Lock()
_snapshot: HebcalSnapshot = DEFAULT_SNAPSHOT


class FontProxy:
    """Small wrapper that normalizes text rendering across pygame font APIs."""

    def __init__(self, size: int, *, bold: bool = False,
                 italic: bool = False, mono: bool = False) -> None:
        """Create a renderable font object."""
        self._size = size
        self._bold = bold
        self._italic = italic
        self._mono = mono
        self._font = self._build_font()

    def _build_font(self) -> object:
        """Build a Pillow font so pygame text modules are never required."""
        font_candidates = [
            "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf" if self._bold else
            "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
            "/System/Library/Fonts/Supplemental/Georgia Bold.ttf" if self._bold else
            "/System/Library/Fonts/Supplemental/Georgia.ttf",
            "/System/Library/Fonts/Supplemental/Courier New Bold.ttf" if self._bold and self._mono else
            "/System/Library/Fonts/Supplemental/Courier New.ttf" if self._mono else
            "/System/Library/Fonts/Supplemental/Palatino.ttc",
            "/System/Library/Fonts/SFNS.ttf",
        ]
        for path in font_candidates:
            if not path:
                continue
            try:
                return ImageFont.truetype(path, self._size)
            except Exception:
                continue
        return ImageFont.load_default()

    def render(self, text: str, color: tuple[int, int, int]) -> pygame.Surface:
        """Render text to a surface."""
        if not text:
            return pygame.Surface((1, 1), pygame.SRCALPHA)

        temp_image = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
        temp_draw = ImageDraw.Draw(temp_image)
        bbox = temp_draw.textbbox((0, 0), text, font=self._font)
        width = max(1, bbox[2] - bbox[0] + 4)
        height = max(1, bbox[3] - bbox[1] + 4)

        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.text((2 - bbox[0], 2 - bbox[1]), text, font=self._font,
                  fill=(color[0], color[1], color[2], 255))
        return pygame.image.fromstring(image.tobytes(), image.size, "RGBA")


def _font(size: int, *, bold: bool = False,
          italic: bool = False, mono: bool = False) -> FontProxy:
    """Build a renderable font wrapper."""
    return FontProxy(size, bold=bold, italic=italic, mono=mono)


def _fill_vertical_gradient(
    surface: pygame.Surface, rect: pygame.Rect,
    top_color: tuple[int, int, int], bottom_color: tuple[int, int, int],
) -> None:
    """Fill a rectangle with a vertical gradient."""
    height = max(1, rect.height)
    for offset in range(height):
        ratio = offset / max(1, height - 1)
        color = (
            int(top_color[0] + (bottom_color[0] - top_color[0]) * ratio),
            int(top_color[1] + (bottom_color[1] - top_color[1]) * ratio),
            int(top_color[2] + (bottom_color[2] - top_color[2]) * ratio),
        )
        pygame.draw.line(
            surface,
            color,
            (rect.left, rect.top + offset),
            (rect.right - 1, rect.top + offset),
        )


def _fetch_json(url: str) -> dict:
    """Fetch one JSON document with a bounded timeout."""
    req = urllib.request.Request(url, headers={"User-Agent": "GlowUp-BespokeClock/1.0"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _format_time(timestamp: str | None) -> str:
    """Format an ISO timestamp as local 12-hour time."""
    if not timestamp:
        return "--:--"
    try:
        return datetime.fromisoformat(timestamp).strftime("%-I:%M")
    except ValueError:
        return "--:--"


def _today_holiday(items: list[dict]) -> str:
    """Extract today's holiday title from Hebcal shabbat items."""
    today = date.today()
    for item in items:
        category = item.get("category", "")
        if category not in {"holiday", "roshchodesh", "omer"}:
            continue
        item_date = item.get("date", "")
        try:
            parsed = datetime.fromisoformat(item_date).date()
        except ValueError:
            continue
        if parsed == today:
            return item.get("title", "")
    return "No special observance"


def _extract_snapshot() -> HebcalSnapshot:
    """Resolve one fresh Hebcal snapshot for Mobile, Alabama."""
    today = date.today().strftime("%Y-%m-%d")
    converter_url = (
        "https://www.hebcal.com/converter?cfg=json"
        f"&date={today}&g2h=1&gs=on"
    )
    zmanim_url = (
        "https://www.hebcal.com/zmanim?cfg=json"
        f"&latitude={MOBILE_LATITUDE}"
        f"&longitude={MOBILE_LONGITUDE}"
        f"&tzid={urllib.parse.quote(TIMEZONE_ID)}"
    )
    shabbat_url = (
        "https://www.hebcal.com/shabbat?cfg=json&geo=pos"
        f"&latitude={MOBILE_LATITUDE}"
        f"&longitude={MOBILE_LONGITUDE}"
        f"&tzid={urllib.parse.quote(TIMEZONE_ID)}&M=on&b=18"
    )

    converter = _fetch_json(converter_url)
    zmanim = _fetch_json(zmanim_url)
    shabbat = _fetch_json(shabbat_url)

    hebrew_date = converter.get("hebrew")
    if not hebrew_date:
        hebrew_date = f"{converter.get('hd', '')} {converter.get('hm', '')} {converter.get('hy', '')}".strip()

    times = zmanim.get("times", {})
    left_panel = tuple(
        ScheduleEntry(
            label,
            _format_time(times.get(key)),
            TEXT_AMBER if key == "alotHaShachar" else TEXT_IVORY,
        )
        for key, label in ZMANIM_ROWS
    )

    items = shabbat.get("items", [])
    candle_time = "--:--"
    havdalah_time = "--:--"
    parasha = ""
    for item in items:
        category = item.get("category")
        if category == "candles":
            candle_time = _format_time(item.get("date"))
        elif category == "havdalah":
            havdalah_time = _format_time(item.get("date"))
        elif category == "parashat":
            parasha = item.get("title", "")

    holiday_text = _today_holiday(items)
    right_panel = (
        ScheduleEntry("Candle Lighting", candle_time, TEXT_AMBER),
        ScheduleEntry("Havdalah", havdalah_time),
        ScheduleEntry("Parasha", parasha or "This week"),
        ScheduleEntry("Holiday", holiday_text),
    )

    detail_lines = ("Updated from Hebcal",)

    return HebcalSnapshot(
        hebrew_date=hebrew_date or "Hebrew date unavailable",
        left_panel=left_panel,
        right_panel=right_panel,
        detail_lines=detail_lines,
        loaded_at=datetime.now(),
        stale=False,
    )


def _poll_hebcal() -> None:
    """Background poller that refreshes the display snapshot forever."""
    global _snapshot
    while True:
        try:
            fresh = _extract_snapshot()
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
            with _snapshot_lock:
                old = _snapshot
                _snapshot = HebcalSnapshot(
                    hebrew_date=old.hebrew_date,
                    left_panel=old.left_panel,
                    right_panel=old.right_panel,
                    detail_lines=old.detail_lines,
                    loaded_at=old.loaded_at,
                    stale=True,
                )
        else:
            with _snapshot_lock:
                _snapshot = fresh
        time.sleep(HEBCAL_POLL_SECONDS)


def _current_snapshot() -> HebcalSnapshot:
    """Return the latest cached Hebcal snapshot."""
    with _snapshot_lock:
        return _snapshot


def _draw_frame(surface: pygame.Surface, rect: pygame.Rect) -> None:
    """Draw the clock enclosure and wood-toned interior."""
    pygame.draw.rect(surface, FRAME_OUTER, rect, border_radius=32)
    inner = rect.inflate(-26, -26)
    pygame.draw.rect(surface, FRAME_INNER, inner, border_radius=24)
    content = inner.inflate(-18, -18)
    _fill_vertical_gradient(surface, content, BACKGROUND_TOP, BACKGROUND_BOTTOM)

    stripe_top = pygame.Rect(content.left, content.top + 44, content.width, 26)
    _fill_vertical_gradient(surface, stripe_top, BRASS_LIGHT, BRASS_DARK)
    footer = pygame.Rect(content.left, content.bottom - 40, content.width, 18)
    _fill_vertical_gradient(surface, footer, BRASS_DARK, BRASS_LIGHT)

    for y in range(content.top + 90, content.bottom - 55, 48):
        alpha = 24 if (y // 48) % 2 == 0 else 12
        stripe = pygame.Surface((content.width, WOOD_STRIPE_HEIGHT), pygame.SRCALPHA)
        stripe.fill((255, 255, 255, alpha))
        surface.blit(stripe, (content.left, y))


def _draw_header(
    surface: pygame.Surface, rect: pygame.Rect, congregation_name: str,
    snapshot: HebcalSnapshot,
) -> None:
    """Draw the top name/date/time band."""
    title_font = _font(max(20, rect.height // 17), bold=True)
    date_font = _font(max(13, rect.height // 34), bold=True)
    time_font = _font(max(17, rect.height // 24), bold=True)

    now = datetime.now()
    left_text = now.strftime("%b %d, %Y")
    center_text = now.strftime("%-I:%M:%S %p").lower()
    right_text = snapshot.hebrew_date

    title = title_font.render(congregation_name.upper(), BRASS_LIGHT)
    title_rect = title.get_rect(center=(rect.centerx, rect.top + 18))
    surface.blit(title, title_rect)

    band_rect = pygame.Rect(rect.left, rect.top + 44, rect.width, 26)
    date_band_y = band_rect.centery
    left = date_font.render(left_text, HAND_COLOR)
    center = time_font.render(center_text, HAND_COLOR)
    right = date_font.render(right_text, HAND_COLOR)
    surface.blit(left, left.get_rect(midleft=(rect.left + 20, date_band_y - 1)))
    surface.blit(center, center.get_rect(center=(rect.centerx, date_band_y - 1)))
    surface.blit(right, right.get_rect(midright=(rect.right - 20, date_band_y - 1)))


def _draw_panel(
    surface: pygame.Surface, rect: pygame.Rect, title: str,
    entries: tuple[ScheduleEntry, ...], *, compact: bool = False,
) -> None:
    """Draw one brass-topped schedule panel."""
    shadow = pygame.Surface((rect.width, rect.height), pygame.SRCALPHA)
    pygame.draw.rect(shadow, SHADOW, shadow.get_rect(), border_radius=36)
    surface.blit(shadow, (rect.x + 5, rect.y + 8))

    pygame.draw.rect(surface, PANEL_BG, rect, border_radius=36)
    pygame.draw.rect(surface, PANEL_EDGE, rect, width=5, border_radius=36)

    top_bar = pygame.Rect(rect.left + 16, rect.top + 14, rect.width - 32, 18)
    _fill_vertical_gradient(surface, top_bar, BRASS_DARK, BRASS_LIGHT)
    lamp_center = (rect.centerx, rect.top + 28)
    pygame.draw.ellipse(surface, BRASS_LIGHT, (lamp_center[0] - 12, lamp_center[1] - 5, 24, 10))
    pygame.draw.ellipse(surface, (255, 255, 240), (lamp_center[0] - 7, lamp_center[1] - 2, 14, 5))

    title_font = _font(max(18, rect.width // 13), bold=True)
    row_font = _font(max(12 if compact else 14, rect.width // (22 if compact else 19)), bold=True)

    title_surf = title_font.render(title, BRASS_LIGHT)
    surface.blit(title_surf, title_surf.get_rect(center=(rect.centerx, rect.top + 48)))

    start_y = rect.top + 92
    row_height = max(16 if compact else 22, (rect.height - 118) // max(1, len(entries) + (2 if compact else 0)))
    label_x = rect.left + 20
    time_x = rect.right - 20
    for index, entry in enumerate(entries):
        if not entry.label:
            continue
        y = start_y + index * row_height
        label_surf = row_font.render(entry.label, entry.color)
        time_surf = row_font.render(entry.time_text, entry.color)
        surface.blit(label_surf, (label_x, y))
        surface.blit(time_surf, (time_x - time_surf.get_width(), y))


def _draw_right_panel(
    surface: pygame.Surface, rect: pygame.Rect, title: str,
    entries: tuple[ScheduleEntry, ...],
) -> None:
    """Draw the condensed right panel with integrated donation QR area."""
    shadow = pygame.Surface((rect.width, rect.height), pygame.SRCALPHA)
    pygame.draw.rect(shadow, SHADOW, shadow.get_rect(), border_radius=36)
    surface.blit(shadow, (rect.x + 5, rect.y + 8))

    pygame.draw.rect(surface, PANEL_BG, rect, border_radius=36)
    pygame.draw.rect(surface, PANEL_EDGE, rect, width=5, border_radius=36)

    top_bar = pygame.Rect(rect.left + 16, rect.top + 14, rect.width - 32, 18)
    _fill_vertical_gradient(surface, top_bar, BRASS_DARK, BRASS_LIGHT)
    lamp_center = (rect.centerx, rect.top + 28)
    pygame.draw.ellipse(surface, BRASS_LIGHT, (lamp_center[0] - 12, lamp_center[1] - 5, 24, 10))
    pygame.draw.ellipse(surface, (255, 255, 240), (lamp_center[0] - 7, lamp_center[1] - 2, 14, 5))

    title_font = _font(max(18, rect.width // 13), bold=True)
    row_font = _font(max(12, rect.width // 22), bold=True)
    title_surf = title_font.render(title, BRASS_LIGHT)
    surface.blit(title_surf, title_surf.get_rect(center=(rect.centerx, rect.top + 48)))

    start_y = rect.top + 92
    upper_region_bottom = rect.top + int(rect.height * 0.42)
    row_height = max(20, (upper_region_bottom - start_y) // max(1, len(entries)))
    label_x = rect.left + 20
    time_x = rect.right - 20
    last_y = start_y
    for index, entry in enumerate(entries):
        y = start_y + index * row_height
        last_y = y
        label_surf = row_font.render(entry.label, entry.color)
        time_surf = row_font.render(entry.time_text, entry.color)
        surface.blit(label_surf, (label_x, y))
        surface.blit(time_surf, (time_x - time_surf.get_width(), y))

    divider_y = max(last_y + 28, rect.top + int(rect.height * 0.48))
    divider_rect = pygame.Rect(rect.left + 22, divider_y, rect.width - 44, 10)
    _draw_engraved_divider(surface, divider_rect)

    donation_rect = pygame.Rect(
        rect.left + 16,
        rect.top + int(rect.height * 0.56),
        rect.width - 32,
        rect.bottom - (rect.top + int(rect.height * 0.56)) - 18,
    )
    _draw_donation_section(surface, donation_rect)


def _load_qr_surface(path: str, size: int) -> pygame.Surface | None:
    """Load and scale a QR image if it exists on disk."""
    if not os.path.exists(path):
        return None
    try:
        image = Image.open(path).convert("RGBA")
        image = image.resize((size, size), Image.Resampling.LANCZOS)
        return pygame.image.fromstring(image.tobytes(), image.size, "RGBA")
    except Exception:
        return None


def _draw_qr_placeholder(surface: pygame.Surface, rect: pygame.Rect, label: str) -> None:
    """Draw a tidy stand-in when the QR asset is not yet available."""
    pygame.draw.rect(surface, (244, 240, 228), rect, border_radius=10)
    pygame.draw.rect(surface, BRASS_DARK, rect, width=2, border_radius=10)
    corner = 20
    for dx, dy in ((0, 0), (rect.width - corner - 8, 0), (0, rect.height - corner - 8)):
        box = pygame.Rect(rect.left + dx + 8, rect.top + dy + 8, corner, corner)
        pygame.draw.rect(surface, BRASS_DARK, box, width=3, border_radius=4)
        pygame.draw.circle(surface, BRASS_DARK, box.center, 4)
    font = _font(max(13, rect.width // 9), bold=True)
    text = font.render(label, BRASS_DARK)
    surface.blit(text, text.get_rect(center=rect.center))


def _draw_engraved_divider(surface: pygame.Surface, rect: pygame.Rect) -> None:
    """Draw a shallow engraved separator line."""
    pygame.draw.line(surface, (32, 15, 8), (rect.left, rect.centery), (rect.right, rect.centery), 4)
    pygame.draw.line(surface, BRASS_DARK, (rect.left, rect.centery - 1), (rect.right, rect.centery - 1), 1)
    pygame.draw.line(surface, BRASS_LIGHT, (rect.left, rect.centery + 1), (rect.right, rect.centery + 1), 1)


def _draw_donation_section(surface: pygame.Surface, rect: pygame.Rect) -> None:
    """Draw the donation QR row."""
    title_font = _font(max(12, rect.height // 13), bold=True)
    label_font = _font(max(10, rect.height // 17), bold=True)
    title = title_font.render("Support Chabad of Mobile", TEXT_GOLD)
    surface.blit(title, title.get_rect(center=(rect.centerx, rect.top + 12)))

    qr_size = min(82, int(rect.height * 0.58), (rect.width - 40) // 3)
    gap = (rect.width - 3 * qr_size) // 4
    y = rect.bottom - qr_size - 28
    for index, (label, path) in enumerate(QR_CODES):
        x = rect.left + gap + index * (qr_size + gap)
        qr_rect = pygame.Rect(x, y, qr_size, qr_size)
        qr_surface = _load_qr_surface(path, qr_size)
        if qr_surface is None:
            _draw_qr_placeholder(surface, qr_rect, label)
        else:
            surface.blit(qr_surface, qr_rect.topleft)
        text = label_font.render(label, TEXT_IVORY)
        surface.blit(text, text.get_rect(center=(qr_rect.centerx, qr_rect.bottom + 12)))


def _draw_detail_lines(
    surface: pygame.Surface, rect: pygame.Rect, lines: tuple[str, ...], stale: bool,
) -> None:
    """Draw small status text under the analog clock."""
    label_font = _font(max(14, rect.width // 18), bold=True)
    value_font = _font(max(12, rect.width // 24))
    header = label_font.render("Mobile / Hebcal", TEXT_GOLD if not stale else TEXT_AMBER)
    surface.blit(header, header.get_rect(center=(rect.centerx, rect.bottom + 18)))

    for index, line in enumerate(lines):
        surf = value_font.render(line, TEXT_SUBDUED if not stale else TEXT_AMBER)
        surface.blit(surf, surf.get_rect(center=(rect.centerx, rect.bottom + 34 + index * 14)))


def _draw_analog_clock(surface: pygame.Surface, rect: pygame.Rect) -> None:
    """Draw the ornate center analog clock."""
    center = rect.center
    radius = min(rect.width, rect.height) // 2 - 10

    # Outer shadow behind the bezel.
    pygame.draw.circle(surface, (45, 20, 10), (center[0] + 6, center[1] + 10), radius + 24)

    # Raised outer bezel: darker at the base, bright highlight toward the top.
    for offset in range(18, -1, -1):
        ring_radius = radius + 18 - offset
        blend = offset / 18.0
        ring_color = (
            int(BRASS_DARK[0] * (1 - blend) + BRASS_LIGHT[0] * blend),
            int(BRASS_DARK[1] * (1 - blend) + BRASS_LIGHT[1] * blend),
            int(BRASS_DARK[2] * (1 - blend) + BRASS_LIGHT[2] * blend),
        )
        pygame.draw.circle(surface, ring_color, center, ring_radius)

    # Warm mahogany shoulder inside the domed brass.
    pygame.draw.circle(surface, (108, 46, 23), center, radius + 4)

    # Inner brass lip that turns inward toward the recessed face.
    for offset in range(10):
        ring_radius = radius - 2 - offset
        blend = offset / 9.0
        ring_color = (
            int(BRASS_LIGHT[0] * (1 - blend) + BRASS_DARK[0] * blend),
            int(BRASS_LIGHT[1] * (1 - blend) + BRASS_DARK[1] * blend),
            int(BRASS_LIGHT[2] * (1 - blend) + BRASS_DARK[2] * blend),
        )
        pygame.draw.circle(surface, ring_color, center, ring_radius, width=2)

    # Recess shadow well.
    recessed_center = (center[0], center[1] + 4)
    pygame.draw.circle(surface, (122, 77, 33), recessed_center, radius - 24)
    pygame.draw.circle(surface, (88, 49, 20), recessed_center, radius - 26, width=5)

    # Recessed face with subtle vertical dome lighting.
    face_radius = radius - 32
    for y_offset in range(-face_radius, face_radius + 1):
        norm = (y_offset + face_radius) / max(1, 2 * face_radius)
        top_glow = 1.0 - norm
        color = (
            int(CLOCK_FACE_DARK[0] * (1 - top_glow * 0.55) + CLOCK_FACE[0] * (top_glow * 0.55)),
            int(CLOCK_FACE_DARK[1] * (1 - top_glow * 0.55) + CLOCK_FACE[1] * (top_glow * 0.55)),
            int(CLOCK_FACE_DARK[2] * (1 - top_glow * 0.55) + CLOCK_FACE[2] * (top_glow * 0.55)),
        )
        half_width = int((face_radius * face_radius - y_offset * y_offset) ** 0.5)
        pygame.draw.line(
            surface,
            color,
            (recessed_center[0] - half_width, recessed_center[1] + y_offset),
            (recessed_center[0] + half_width, recessed_center[1] + y_offset),
        )

    # Inner face rim and soft upper highlight to sell the glass dome.
    pygame.draw.circle(surface, BRASS_DARK, recessed_center, face_radius + 2, width=2)
    pygame.draw.arc(
        surface,
        (255, 248, 220),
        (recessed_center[0] - face_radius + 8, recessed_center[1] - face_radius + 4,
         (face_radius - 8) * 2, (face_radius - 12) * 2),
        math.radians(210),
        math.radians(330),
        3,
    )
    pygame.draw.arc(
        surface,
        (150, 90, 30),
        (recessed_center[0] - face_radius + 6, recessed_center[1] - face_radius + 10,
         (face_radius - 6) * 2, (face_radius - 10) * 2),
        math.radians(20),
        math.radians(150),
        2,
    )

    numeral_font = _font(max(34, radius // 4), bold=False)
    for idx in range(CLOCK_TICK_COUNT):
        angle = math.radians((idx / CLOCK_TICK_COUNT) * 360 - 60)
        outer_x = center[0] + math.cos(angle) * (radius - 10)
        outer_y = center[1] + math.sin(angle) * (radius - 10)
        inner_x = center[0] + math.cos(angle) * (radius - 42)
        inner_y = center[1] + math.sin(angle) * (radius - 42)
        pygame.draw.line(
            surface,
            TEXT_IVORY,
            (inner_x, inner_y),
            (outer_x, outer_y),
            3,
        )

    numerals = {0: "12", 3: "3", 6: "6", 9: "9"}
    for position, text in numerals.items():
        angle = math.radians((position / CLOCK_TICK_COUNT) * 360 - 90)
        x = center[0] + math.cos(angle) * (radius - 62)
        y = center[1] + math.sin(angle) * (radius - 62) + 2
        numeral = numeral_font.render(text, BRASS_LIGHT)
        surface.blit(numeral, numeral.get_rect(center=(x, y)))

    now = datetime.now()
    hour_angle = math.radians(((now.hour % 12) + now.minute / 60.0) * 30 - 90)
    minute_angle = math.radians((now.minute + now.second / 60.0) * 6 - 90)
    second_angle = math.radians(now.second * 6 - 90)

    _draw_hand(surface, center, hour_angle, radius * 0.42, 8, HAND_COLOR)
    _draw_hand(surface, center, minute_angle, radius * 0.64, 5, HAND_COLOR)
    _draw_hand(surface, center, second_angle, radius * 0.70, 2, BRASS_DARK)
    pygame.draw.circle(surface, BRASS_DARK, center, 9)
    pygame.draw.circle(surface, BRASS_LIGHT, center, 4)


def _draw_hand(
    surface: pygame.Surface, center: tuple[int, int], angle: float,
    length: float, width: int, color: tuple[int, int, int],
) -> None:
    """Draw one clock hand."""
    end = (
        center[0] + math.cos(angle) * length,
        center[1] + math.sin(angle) * length,
    )
    pygame.draw.line(surface, color, center, end, width)


def _draw_footer(surface: pygame.Surface, rect: pygame.Rect, name: str) -> None:
    """Draw the mock footer plaque."""
    pygame.draw.rect(surface, FOOTER_BG, rect, border_radius=12)
    message_font = _font(max(22, rect.height // 2), bold=True)
    small_font = _font(max(12, rect.height // 3))

    hebrew = message_font.render("משיב הרוח  •  נותן ברכה", TEXT_IVORY)
    surface.blit(hebrew, hebrew.get_rect(center=(rect.centerx, rect.centery - 2)))

    badge = small_font.render(f"Mock for {name}", BRASS_LIGHT)
    surface.blit(badge, (rect.right - badge.get_width() - 12, rect.bottom - badge.get_height() - 4))


def _render(surface: pygame.Surface, name: str) -> None:
    """Render the entire bespoke clock scene."""
    snapshot = _current_snapshot()
    frame_rect = pygame.Rect(16, 16, surface.get_width() - 32, surface.get_height() - 32)
    _draw_frame(surface, frame_rect)

    content = frame_rect.inflate(-44, -44)
    _draw_header(surface, content, "Chabad of Mobile", snapshot)

    body_top = content.top + 92
    body_bottom = content.bottom - 56
    body_height = body_bottom - body_top
    panel_width = int(content.width * 0.28)
    clock_size = min(int(content.width * 0.34), int(body_height * 0.82))
    gap = int(content.width * 0.035)

    left_rect = pygame.Rect(content.left + PANEL_INSET, body_top, panel_width, body_height - 22)
    right_rect = pygame.Rect(content.right - PANEL_INSET - panel_width, body_top, panel_width, body_height - 22)
    clock_rect = pygame.Rect(
        left_rect.right + gap,
        body_top + (body_height - clock_size) // 2 - 12,
        clock_size,
        clock_size,
    )

    _draw_panel(surface, left_rect, "Daily Zmanim", snapshot.left_panel)
    _draw_right_panel(surface, right_rect, "Shabbat / Weekly", snapshot.right_panel)
    _draw_analog_clock(surface, clock_rect)
    _draw_detail_lines(surface, clock_rect, snapshot.detail_lines, snapshot.stale)

    footer_rect = pygame.Rect(content.left + 120, content.bottom - 34, content.width - 240, 24)
    _draw_footer(surface, footer_rect, name)


def main() -> None:
    """Run the bespoke clock mock."""
    parser = argparse.ArgumentParser(
        description="Standalone pygame mock for a bespoke congregation clock.",
    )
    parser.add_argument("--name", type=str, default="CoM", help="Mock congregation short name.")
    parser.add_argument("--windowed", action="store_true", help="Run in a window instead of fullscreen.")
    parser.add_argument("--width", type=int, default=WINDOWED_WIDTH, help="Window width in windowed mode.")
    parser.add_argument("--height", type=int, default=WINDOWED_HEIGHT, help="Window height in windowed mode.")
    args = parser.parse_args()

    pygame.init()
    pygame.display.set_caption(f"Bespoke Clock Mock - {args.name}")

    if args.windowed:
        screen = pygame.display.set_mode((args.width, args.height))
    else:
        screen = pygame.display.set_mode((DEFAULT_WIDTH, DEFAULT_HEIGHT))

    pygame.mouse.set_visible(False)
    poller = threading.Thread(target=_poll_hebcal, name="hebcal-poller", daemon=True)
    poller.start()
    clock = pygame.time.Clock()
    running = True

    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key in (pygame.K_ESCAPE, pygame.K_q):
                running = False

        screen.fill((30, 26, 22))
        _render(screen, args.name)
        pygame.display.flip()
        clock.tick(FPS)

    pygame.quit()


if __name__ == "__main__":
    main()
