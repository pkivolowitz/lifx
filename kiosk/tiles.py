"""Tile renderers for the kiosk dashboard.

Each tile is a function that takes a pygame Surface, a Rect
(position/size), the data poller, the theme, and renders itself.
Tiles are stateless — all animation state (page indices, timers)
lives in the module-level dicts keyed by tile name.

Adding a new tile: write a ``draw_foo(surf, rect, data, theme)``
function and register it in ``TILE_REGISTRY`` in app.py.
"""

__version__: str = "1.0"

import logging
import math
import time
from datetime import datetime
from typing import Any, Optional

import pygame

from kiosk.theme import Theme
from kiosk.data import DataPoller

logger: logging.Logger = logging.getLogger("glowup.kiosk.tiles")

# ---------------------------------------------------------------------------
# Font cache — lazy-loaded, shared across tiles
# ---------------------------------------------------------------------------

_fonts: dict[str, pygame.font.Font] = {}

# Nunito Sans TTF path on Raspberry Pi OS (Trixie).
_NUNITO_TTF: str = (
    "/usr/share/fonts/truetype/nunito-sans/"
    "NunitoSans-VariableFont_YTLC,opsz,wdth,wght.ttf"
)


def _font(name: str, size: int) -> pygame.font.Font:
    """Get or create a cached font.

    Args:
        name: Font name (None for default, or a system font name).
        size: Font size in pixels.

    Returns:
        Cached pygame Font object.
    """
    key: str = f"{name}:{size}"
    if key not in _fonts:
        if name == "nunito":
            try:
                _fonts[key] = pygame.font.Font(_NUNITO_TTF, size)
            except Exception:
                _fonts[key] = pygame.font.SysFont(None, size)
        elif name is None:
            _fonts[key] = pygame.font.SysFont(None, size)
        else:
            _fonts[key] = pygame.font.SysFont(name, size)
    return _fonts[key]


def _sans(size: int) -> pygame.font.Font:
    """Get Nunito Sans at the given size — modern, clean."""
    return _font("nunito", size)


def _sans(size: int) -> pygame.font.Font:
    """Get the default sans-serif font at the given size."""
    return _font(None, size)


def _fit_font_width(
    base_size: int, text: str, max_w: int, min_size: int = 14,
) -> pygame.font.Font:
    """Return a sans font sized so ``text`` renders within ``max_w`` pixels.

    Starts at ``base_size`` (which is usually derived from rect.h) and
    shrinks proportionally if the rendered width would overflow max_w.
    Never shrinks below ``min_size``.
    """
    font = _sans(base_size)
    w, _ = font.size(text)
    if w > max_w and w > 0:
        scaled = max(min_size, int(base_size * max_w / w))
        font = _sans(scaled)
    return font


def _clock_font_sizes(
    width: int, target_h: int, date_str: str,
) -> tuple[int, int]:
    """Pick (time_font_size, date_font_size) that fit width and target_h.

    Shared between ``measure_clock_height`` (layout planning) and
    ``draw_clock`` (rendering) so the two stay in sync.
    """
    time_probe: str = "00:00"
    target_w: int = int(width * 0.95)

    time_size: int = max(40, int(target_h * 0.94))
    tfont = _sans(time_size)
    tw, _ = tfont.size(time_probe)
    if tw > target_w:
        time_size = max(40, int(time_size * target_w / tw))

    date_size: int = max(16, target_h * 7 // 4 // 7)
    dfont = _sans(date_size)
    dw, _ = dfont.size(date_str)
    if dw > target_w:
        date_size = max(14, int(date_size * target_w / dw))

    return time_size, date_size


def measure_clock_height(width: int, target_h: int) -> int:
    """Return the pixel height the clock block actually needs.

    Lets ``app.py`` reserve exactly as much vertical space as the
    time+date block will consume, instead of a fixed fraction that
    leaves dead space below the date.
    """
    now = datetime.now()
    date_str: str = now.strftime("%A, %B %-d")
    time_size, date_size = _clock_font_sizes(width, target_h, date_str)
    time_h: int = _sans(time_size).get_height()
    date_h: int = _sans(date_size).get_height()
    # 24px top + 24px bottom padding so the card border has breathing room.
    return time_h + 4 + date_h + 48


# ---------------------------------------------------------------------------
# Card background helper
# ---------------------------------------------------------------------------

def _draw_card(surf: pygame.Surface, rect: pygame.Rect,
               theme: Theme) -> None:
    """Draw a rounded card background with border.

    The SRCALPHA surface is explicitly filled with transparent pixels
    before drawing — on this Pi's pygame/SDL stack the default init
    is opaque black, which caused the whole tile interior to blit as
    a dark rect over the canvas.
    """
    # Skip the whole blit when both fill and border are fully
    # transparent — happens at night where cards are intentionally
    # invisible. Avoids a per-frame SRCALPHA surface allocation.
    if theme.card_bg[3] == 0 and theme.card_border[3] == 0:
        return
    card_surf = pygame.Surface((rect.w, rect.h), pygame.SRCALPHA)
    card_surf.fill((0, 0, 0, 0))
    if theme.card_bg[3] > 0:
        pygame.draw.rect(card_surf, theme.card_bg,
                         (0, 0, rect.w, rect.h), border_radius=14)
    if theme.card_border[3] > 0:
        pygame.draw.rect(card_surf, theme.card_border,
                         (0, 0, rect.w, rect.h), width=3, border_radius=14)
    surf.blit(card_surf, rect.topleft)


def _draw_title(surf: pygame.Surface, rect: pygame.Rect,
                title: str, theme: Theme) -> int:
    """Draw a tile title and return the Y offset below it.

    Args:
        surf:  Target surface.
        rect:  Card rect.
        title: Title text.
        theme: Current theme.

    Returns:
        Y position below the title for content rendering.
    """
    font = _sans(max(14, rect.h * 7 // 4 // 10))
    text = font.render(title, True, theme.label)
    surf.blit(text, (rect.x + 20, rect.y + 16))
    return rect.y + 16 + text.get_height() + 6


# Wallclock content padding — inset from card border so giant text
# never crowds the rounded edges. 0.90 / 0.78 picked empirically.
_GIANT_W_FRAC: float = 0.90
_GIANT_H_FRAC: float = 0.78
_TWO_LINE_H_FRAC: float = 0.42  # per line, leaves a small gap
_GIANT_MIN_SIZE: int = 20  # never shrink below this — matches _fit_font_width default


def _draw_giant_centered(
    surf: pygame.Surface, rect: pygame.Rect,
    text: str, color: tuple[int, int, int],
) -> None:
    """Render ``text`` as large as possible, centered in ``rect``.

    Picks a font size whose rendered height fills ~78% of the card and
    whose width fits inside ~90% of the card.  Used by the wallclock
    tiles where one short phrase (e.g. "LOCKS OK", "67°F") owns the
    entire tile.
    """
    target_h: int = max(_GIANT_MIN_SIZE, int(rect.h * _GIANT_H_FRAC))
    target_w: int = int(rect.w * _GIANT_W_FRAC)
    font = _fit_font_width(target_h, text, target_w, _GIANT_MIN_SIZE)
    text_surf = font.render(text, True, color)
    surf.blit(text_surf, text_surf.get_rect(center=rect.center))


# _draw_two_line_centered was removed when all wallclock tiles
# collapsed to single-line phrases (more readable at night, in
# either grid or stacked layout).


# ---------------------------------------------------------------------------
# Wallclock phrase helpers — single source of truth for the strings
# rendered by both the day-mode grid tiles (draw_X) and the night-mode
# stacked rows (night_row_X).  Each returns ``(text, color)`` so the
# caller only has to pick a rect and blit.
# ---------------------------------------------------------------------------


def _temp_phrase(
    data: DataPoller, theme: Theme,
) -> Optional[tuple[str, tuple[int, int, int]]]:
    """Return (text, color) for the current temperature, or None.

    None means "data not available yet" — caller should render nothing.
    """
    weather: Optional[dict] = data.get("weather")
    if weather is None:
        return None
    current: dict = weather.get("current") or {}
    if not current:
        return None
    temp: float = current.get("temperature_2m", 0)
    return f"{temp:.0f}\u00b0F", theme.temp


def _locks_phrase(
    data: DataPoller, theme: Theme,
) -> Optional[tuple[str, tuple[int, int, int]]]:
    """Return (text, color) summarizing all locks in one phrase.

    All locked → ``LOCKS OK`` (theme.ok).  Any open → ``UNLOCKED:
    <first> +N`` (theme.bad).  None means data not yet available.
    """
    locks_data: Optional[dict] = data.get("locks")
    if locks_data is None:
        return None
    locks: list = locks_data.get("locks", [])
    open_names: list[str] = [
        lock.get("name", "?") for lock in locks
        if not lock.get("locked", False)
    ]
    if not open_names:
        return "LOCKS OK", theme.ok
    first: str = open_names[0]
    extra: str = f" +{len(open_names) - 1}" if len(open_names) > 1 else ""
    return f"UNLOCKED: {first}{extra}", theme.bad


def _doors_phrase(
    data: DataPoller, theme: Theme,
) -> Optional[tuple[str, tuple[int, int, int]]]:
    """Return (text, color) summarizing door sensors in one phrase.

    All closed → ``DOORS OK`` (theme.ok).  Any open → ``OPEN: <first>
    +N`` (theme.bad).  None means security data not yet available.
    """
    security: Optional[dict] = data.get("security")
    if security is None:
        return None
    doors: list = security.get("doors", [])
    open_doors: list[str] = [
        door.get("name", "?") for door in doors
        if door.get("open", False)
    ]
    if not open_doors:
        return "DOORS OK", theme.ok
    first: str = open_doors[0]
    extra: str = f" +{len(open_doors) - 1}" if len(open_doors) > 1 else ""
    return f"OPEN: {first}{extra}", theme.bad


def _alarm_phrase(
    data: DataPoller, theme: Theme,
) -> Optional[tuple[str, tuple[int, int, int]]]:
    """Return (text, color) for the alarm panel state.

    ``armed*`` states are theme.ok (the safe state in this bedroom);
    everything else is theme.bad.  Underscores in compound states
    like ``armed_home`` become spaces for readability.
    """
    security: Optional[dict] = data.get("security")
    if security is None:
        return None
    raw: str = security.get("alarm", "unknown")
    text: str = raw.upper().replace("_", " ")
    color = theme.ok if raw.lower().startswith("armed") else theme.bad
    return text, color


def _alerts_phrase(
    data: DataPoller, theme: Theme, *, blank_when_none: bool,
) -> Optional[tuple[str, tuple[int, int, int]]]:
    """Return (text, color) for severe weather alerts.

    When ``blank_when_none`` is True (day grid) and no alerts are
    active, returns None so the caller draws nothing.  When False
    (night stack), returns ``("NO ALERTS", theme.ok)`` so the row
    is visible and reassuring.

    Active alerts always render — count + first event, blinking
    between bad and warn for attention.
    """
    alerts: Optional[list] = data.get("alerts") or []
    if not alerts:
        if blank_when_none:
            return None
        return "NO ALERTS", theme.ok
    blink: bool = int(time.monotonic() * 2) % 2 == 0
    color = theme.bad if blink else theme.warn
    count_word: str = "ALERT" if len(alerts) == 1 else "ALERTS"
    count: str = f"{len(alerts)} {count_word}"
    first_event: str = (
        alerts[0].get("properties", {}).get("event", "Unknown")
    )
    return f"{count}: {first_event}", color


# ---------------------------------------------------------------------------
# Clock tile
# ---------------------------------------------------------------------------

def draw_clock(surf: pygame.Surface, rect: pygame.Rect,
               data: DataPoller, theme: Theme) -> None:
    """Draw the main clock display inside a bordered card.

    24-hour HH:MM, date below. Font sizes come from
    ``_clock_font_sizes`` so the rendered block fits rect.w and rect.h.
    Caller should size ``rect`` with ``measure_clock_height`` so the
    card hugs the content.
    """
    _draw_card(surf, rect, theme)

    now: datetime = datetime.now()
    time_str: str = f"{now.hour:02d}:{now.minute:02d}"
    date_str: str = now.strftime("%A, %B %-d")

    time_size, date_size = _clock_font_sizes(rect.w, rect.h, date_str)
    time_font = _sans(time_size)
    date_font = _sans(date_size)

    time_surf = time_font.render(time_str, True, theme.clock)
    date_surf = date_font.render(date_str, True, theme.date)

    total_h: int = time_surf.get_height() + 4 + date_surf.get_height()
    top_y: int = rect.y + max(0, (rect.h - total_h) // 2)

    time_rect = time_surf.get_rect(centerx=rect.centerx, top=top_y)
    surf.blit(time_surf, time_rect)

    date_rect = date_surf.get_rect(
        centerx=rect.centerx, top=time_rect.bottom + 4,
    )
    surf.blit(date_surf, date_rect)


# ---------------------------------------------------------------------------
# System health tile
# ---------------------------------------------------------------------------

# Paging state for health tile.
_health_page: int = 0
_health_last_cycle: float = 0.0
HEALTH_PAGE_SIZE: int = 4
HEALTH_CYCLE_S: float = 4.0


def draw_health(surf: pygame.Surface, rect: pygame.Rect,
                data: DataPoller, theme: Theme) -> None:
    """Draw the system health tile with paging."""
    global _health_page, _health_last_cycle

    health: Optional[dict] = data.get("health")
    if health is None:
        return

    _draw_card(surf, rect, theme)
    y: int = _draw_title(surf, rect, "System Health", theme)

    adapters: dict[str, bool] = health.get("adapters", {})
    keys: list[str] = list(adapters.keys())
    total_pages: int = max(1, math.ceil(len(keys) / HEALTH_PAGE_SIZE))

    # Cycle pages.
    now: float = time.monotonic()
    if now - _health_last_cycle >= HEALTH_CYCLE_S and total_pages > 1:
        _health_page = (_health_page + 1) % total_pages
        _health_last_cycle = now

    start: int = (_health_page % total_pages) * HEALTH_PAGE_SIZE
    end: int = min(start + HEALTH_PAGE_SIZE, len(keys))

    row_font = _sans(max(15, rect.h * 7 // 4 // 7 * 9 // 10))
    row_h: int = row_font.get_height() + 4

    for i in range(start, end):
        name: str = keys[i]
        ok: bool = adapters[name]
        name_surf = row_font.render(name, True, theme.label)
        status_surf = row_font.render(
            "OK" if ok else "DOWN",
            True, theme.ok if ok else theme.bad,
        )
        surf.blit(name_surf, (rect.x + 20, y))
        surf.blit(status_surf, (rect.right - 20 - status_surf.get_width(), y))
        y += row_h

    # Summary line + page dots combined on one line at bottom.
    devices: int = health.get("devices", 0)
    schedules: int = health.get("schedules", 0)
    summary: str = f"{devices} devices · {schedules} sched"
    sum_font = _sans(max(11, rect.h * 7 // 4 // 12))
    sum_surf = sum_font.render(summary, True, theme.dim)
    sum_y: int = rect.bottom - sum_surf.get_height() - 4
    surf.blit(sum_surf, (rect.x + 20, sum_y))

    # Page dots — right side of summary line.
    if total_pages > 1:
        dot_y: int = sum_y + sum_surf.get_height() // 2
        dot_total_w: int = total_pages * 10
        dot_x: int = rect.right - 20 - dot_total_w
        for p in range(total_pages):
            color = theme.text if p == _health_page % total_pages else theme.dim
            pygame.draw.circle(surf, color, (dot_x + p * 10 + 3, dot_y), 2)


# ---------------------------------------------------------------------------
# Locks tile
# ---------------------------------------------------------------------------

def draw_locks(surf: pygame.Surface, rect: pygame.Rect,
               data: DataPoller, theme: Theme) -> None:
    """Draw lock status as one giant single-line phrase.

    Day-grid renderer.  Battery levels dropped — wallclock readability
    over completeness.  See ``_locks_phrase`` for the text/color rules.
    """
    phrase = _locks_phrase(data, theme)
    if phrase is None:
        return
    _draw_card(surf, rect, theme)
    text, color = phrase
    _draw_giant_centered(surf, rect, text, color)


# ---------------------------------------------------------------------------
# Weather tile
# ---------------------------------------------------------------------------

# WMO weather codes → descriptions.
_WMO: dict[int, str] = {
    0: "Clear", 1: "Mostly Clear", 2: "Partly Cloudy",
    3: "Overcast", 45: "Foggy", 48: "Rime Fog",
    51: "Light Drizzle", 53: "Drizzle", 55: "Heavy Drizzle",
    61: "Light Rain", 63: "Rain", 65: "Heavy Rain",
    71: "Light Snow", 73: "Snow", 75: "Heavy Snow",
    80: "Rain Showers", 81: "Mod Showers", 82: "Heavy Showers",
    95: "Thunderstorm", 96: "T-storm + Hail", 99: "Severe T-storm",
}


def draw_weather(surf: pygame.Surface, rect: pygame.Rect,
                 data: DataPoller, theme: Theme) -> None:
    """Draw current temperature as a single giant number.

    Day-grid renderer.  No condition, no humidity, no wind — just the
    temperature.  See ``_temp_phrase``.
    """
    phrase = _temp_phrase(data, theme)
    if phrase is None:
        return
    _draw_card(surf, rect, theme)
    text, color = phrase
    _draw_giant_centered(surf, rect, text, color)


# ---------------------------------------------------------------------------
# AQI tile
# ---------------------------------------------------------------------------

def _aqi_category(aqi: float) -> tuple[str, tuple[int, int, int]]:
    """Return AQI category name and color."""
    if aqi <= 50:
        return "Good", (102, 187, 106)
    elif aqi <= 100:
        return "Moderate", (255, 235, 59)
    elif aqi <= 150:
        return "Unhealthy (Sensitive)", (255, 152, 0)
    elif aqi <= 200:
        return "Unhealthy", (244, 67, 54)
    elif aqi <= 300:
        return "Very Unhealthy", (156, 39, 176)
    return "Hazardous", (136, 14, 79)


def draw_aqi(surf: pygame.Surface, rect: pygame.Rect,
             data: DataPoller, theme: Theme) -> None:
    """Draw air quality index."""
    aqi_data: Optional[dict] = data.get("aqi")
    if aqi_data is None:
        return

    current: dict = aqi_data.get("current", {})
    if not current:
        return

    _draw_card(surf, rect, theme)
    y: int = _draw_title(surf, rect, "Air Quality", theme)

    aqi: float = current.get("us_aqi", 0)
    cat_name, cat_color = _aqi_category(aqi)

    # AQI value — large.
    val_font = _sans(max(28, rect.h * 7 // 4 // 4))
    val_surf = val_font.render(str(int(aqi)), True, cat_color)
    surf.blit(val_surf, (rect.x + 20, y))
    y += val_surf.get_height() + 2

    # Category label.
    cat_font = _sans(max(14, rect.h * 7 // 4 // 8))
    cat_surf = cat_font.render(cat_name, True, cat_color)
    surf.blit(cat_surf, (rect.x + 20, y))
    y += cat_surf.get_height() + 2

    # PM2.5 + PM10 — one per line.
    pm25: float = current.get("pm2_5", 0)
    pm10: float = current.get("pm10", 0)
    detail_font = _sans(max(12, rect.h * 7 // 4 // 10))
    pm25_surf = detail_font.render(f"PM2.5: {pm25:.0f}", True, theme.dim)
    surf.blit(pm25_surf, (rect.x + 20, y))
    y += detail_font.get_height() + 2
    pm10_surf = detail_font.render(f"PM10: {pm10:.0f}", True, theme.dim)
    surf.blit(pm10_surf, (rect.x + 20, y))


# ---------------------------------------------------------------------------
# Soil moisture tile (paging like health)
# ---------------------------------------------------------------------------

_soil_index: int = 0
_soil_last_cycle: float = 0.0
SOIL_CYCLE_S: float = 4.0


def draw_soil(surf: pygame.Surface, rect: pygame.Rect,
              data: DataPoller, theme: Theme) -> None:
    """Draw soil moisture, cycling through sensors."""
    global _soil_index, _soil_last_cycle

    soil_data: Optional[dict] = data.get("soil")
    if soil_data is None:
        return

    sensors: list = soil_data.get("sensors", [])
    if not sensors:
        return

    _draw_card(surf, rect, theme)
    y: int = _draw_title(surf, rect, "Soil Moisture", theme)

    # Cycle through sensors.
    now: float = time.monotonic()
    if now - _soil_last_cycle >= SOIL_CYCLE_S and len(sensors) > 1:
        _soil_index = (_soil_index + 1) % len(sensors)
        _soil_last_cycle = now

    s: dict = sensors[_soil_index % len(sensors)]
    name: str = s.get("name", "?")
    moisture = s.get("soil_moisture")
    temp_c = s.get("temperature")
    batt = s.get("battery")

    # Sensor name.
    name_font = _sans(max(14, rect.h * 7 // 4 // 8))
    name_surf = name_font.render(name, True, theme.text)
    surf.blit(name_surf, (rect.x + 20, y))
    y += name_surf.get_height() + 4

    # Moisture value — large.
    if moisture is not None:
        pct: int = round(moisture)
        if pct >= 40:
            color = theme.soil_wet
        elif pct >= 20:
            color = theme.soil_dry
        else:
            color = theme.soil_critical
        val_font = _sans(max(28, rect.h * 7 // 4 // 4))
        val_surf = val_font.render(f"{pct}%", True, color)
        surf.blit(val_surf, (rect.x + 20, y))
        y += val_surf.get_height() + 2

    # Temperature + battery.
    details: list[str] = []
    if temp_c is not None:
        temp_f: float = temp_c * 9 / 5 + 32
        details.append(f"{temp_f:.0f}\u00b0F")
    if batt is not None:
        details.append(f"Bat {batt:.0f}%")
    if details:
        det_font = _sans(max(12, rect.h * 7 // 4 // 10))
        det_surf = det_font.render("  ".join(details), True, theme.dim)
        surf.blit(det_surf, (rect.x + 20, y))

    # Page dots.
    if len(sensors) > 1:
        dot_y: int = rect.bottom - 8
        dot_total_w: int = len(sensors) * 12
        dot_x: int = rect.centerx - dot_total_w // 2
        for i in range(len(sensors)):
            color = theme.text if i == _soil_index % len(sensors) else theme.dim
            pygame.draw.circle(surf, color, (dot_x + i * 12 + 4, dot_y), 3)


# ---------------------------------------------------------------------------
# Moon phase tile
# ---------------------------------------------------------------------------

def _moon_phase(year: int, month: int, day: int) -> float:
    """Calculate moon phase as 0.0–1.0 (0=new, 0.5=full).

    Simple Conway algorithm — accurate to ~1 day.
    """
    if month <= 2:
        year -= 1
        month += 12
    a: float = year / 100.0
    b: float = a / 4.0
    c: float = 2.0 - a + b
    e: float = 365.25 * (year + 4716)
    f: float = 30.6001 * (month + 1)
    jd: float = c + day + e + f - 1524.5
    days_since_new: float = jd - 2451549.5
    phase: float = (days_since_new % 29.53059) / 29.53059
    return phase


def draw_moon(surf: pygame.Surface, rect: pygame.Rect,
              data: DataPoller, theme: Theme) -> None:
    """Draw moon phase using simple circle geometry."""
    _draw_card(surf, rect, theme)
    y: int = _draw_title(surf, rect, "Moon", theme)

    now: datetime = datetime.now()
    phase: float = _moon_phase(now.year, now.month, now.day)

    # Phase name.
    if phase < 0.03 or phase > 0.97:
        name = "New Moon"
    elif phase < 0.22:
        name = "Waxing Crescent"
    elif phase < 0.28:
        name = "First Quarter"
    elif phase < 0.47:
        name = "Waxing Gibbous"
    elif phase < 0.53:
        name = "Full Moon"
    elif phase < 0.72:
        name = "Waning Gibbous"
    elif phase < 0.78:
        name = "Third Quarter"
    else:
        name = "Waning Crescent"

    # Draw moon circle.
    radius: int = min(rect.w, rect.h - (y - rect.y) - 30) // 3
    cx: int = rect.centerx
    cy: int = y + radius + 4

    # Dark circle (shadow).
    pygame.draw.circle(surf, theme.dim, (cx, cy), radius)

    # Illuminated portion — draw the lit side over the dark circle.
    illum_color = theme.text
    # Phase 0=new (dark), 0.5=full (bright).
    # Waxing (0→0.5): right side illuminated, terminator sweeps left.
    # Waning (0.5→1): left side illuminated, terminator sweeps right.
    for row in range(-radius, radius + 1):
        half_w: float = math.sqrt(max(0, radius * radius - row * row))
        # Terminator x-offset: cos maps phase to terminator position.
        # Map phase to terminator position via cosine.
        # phase=0 (new): cos(0)=1, term=+half_w → x1=right edge, no light.
        # phase=0.5 (full): cos(π)=-1, term=-half_w → x1=left edge, full light.
        term: float = half_w * math.cos(phase * 2 * math.pi)
        if phase <= 0.5:
            # Waxing — right side lit (Northern Hemisphere).
            x1: int = cx - int(half_w)
            x2: int = cx - int(term)
        else:
            # Waning — left side lit.
            x1 = cx + int(term)
            x2 = cx + int(half_w)
        if x2 > x1:
            pygame.draw.line(surf, illum_color,
                             (x1, cy + row), (x2, cy + row))

    # Phase name below.
    name_font = _sans(max(12, rect.h * 7 // 4 // 10))
    name_surf = name_font.render(name, True, theme.label)
    surf.blit(name_surf, (
        rect.centerx - name_surf.get_width() // 2,
        cy + radius + 12,
    ))


# ---------------------------------------------------------------------------
# NWS Alerts tile
# ---------------------------------------------------------------------------

def draw_alerts(surf: pygame.Surface, rect: pygame.Rect,
                data: DataPoller, theme: Theme) -> None:
    """Draw NWS severe weather alerts as one giant single-line phrase.

    Day-grid renderer.  Blank card when nothing is active (no
    placeholder text competing with the rest of the display).  When
    alerts exist, render count + first event giant and blinking.
    Night uses ``night_row_alerts`` instead, which shows ``NO ALERTS``
    so the row stays present.
    """
    phrase = _alerts_phrase(data, theme, blank_when_none=True)
    _draw_card(surf, rect, theme)
    if phrase is None:
        return
    text, color = phrase
    _draw_giant_centered(surf, rect, text, color)


# ---------------------------------------------------------------------------
# Security / alarm tile
# ---------------------------------------------------------------------------

def draw_security(surf: pygame.Surface, rect: pygame.Rect,
                  data: DataPoller, theme: Theme) -> None:
    """Draw security state as one giant single-line phrase.

    Day-grid renderer.  Combines alarm + door status with ``·`` so
    one tile shows both.  Night uses two separate rows
    (``night_row_alarm`` + ``night_row_doors``) per Perry's spec.
    Color is theme.bad if either component is bad.
    """
    alarm = _alarm_phrase(data, theme)
    doors = _doors_phrase(data, theme)
    if alarm is None and doors is None:
        return
    _draw_card(surf, rect, theme)

    parts: list[str] = []
    any_bad: bool = False
    if alarm is not None:
        parts.append(alarm[0])
        if alarm[1] == theme.bad:
            any_bad = True
    if doors is not None:
        parts.append(doors[0])
        if doors[1] == theme.bad:
            any_bad = True
    text: str = " \u00b7 ".join(parts)
    color = theme.bad if any_bad else theme.ok
    _draw_giant_centered(surf, rect, text, color)


# ---------------------------------------------------------------------------
# Night-mode stacked-row renderers
# ---------------------------------------------------------------------------
# At night, the wallclock dumps the 2x2 grid in favor of a vertical
# stack of full-width single-line rows.  Each row is one piece of
# information, centered, sized to fill the row.  Security splits into
# two rows (alarm + doors) so each phrase gets its own line.
#
# These functions intentionally do not call _draw_card — night theme
# already disables card chrome (alpha 0) and the helper short-circuits.

def _draw_night_row(
    surf: pygame.Surface, rect: pygame.Rect,
    phrase: Optional[tuple[str, tuple[int, int, int]]],
) -> None:
    """Render a night row's phrase, or skip if data not yet available."""
    if phrase is None:
        return
    text, color = phrase
    _draw_giant_centered(surf, rect, text, color)


def night_row_temp(surf: pygame.Surface, rect: pygame.Rect,
                   data: DataPoller, theme: Theme) -> None:
    """Night row: current temperature."""
    _draw_night_row(surf, rect, _temp_phrase(data, theme))


def night_row_locks(surf: pygame.Surface, rect: pygame.Rect,
                    data: DataPoller, theme: Theme) -> None:
    """Night row: lock summary."""
    _draw_night_row(surf, rect, _locks_phrase(data, theme))


def night_row_doors(surf: pygame.Surface, rect: pygame.Rect,
                    data: DataPoller, theme: Theme) -> None:
    """Night row: door sensor summary."""
    _draw_night_row(surf, rect, _doors_phrase(data, theme))


def night_row_alarm(surf: pygame.Surface, rect: pygame.Rect,
                    data: DataPoller, theme: Theme) -> None:
    """Night row: alarm panel state."""
    _draw_night_row(surf, rect, _alarm_phrase(data, theme))


def night_row_alerts(surf: pygame.Surface, rect: pygame.Rect,
                     data: DataPoller, theme: Theme) -> None:
    """Night row: NWS alerts (shows ``NO ALERTS`` when none)."""
    _draw_night_row(
        surf, rect,
        _alerts_phrase(data, theme, blank_when_none=False),
    )
