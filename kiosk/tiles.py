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


# ---------------------------------------------------------------------------
# Card background helper
# ---------------------------------------------------------------------------

def _draw_card(surf: pygame.Surface, rect: pygame.Rect,
               theme: Theme) -> None:
    """Draw a rounded card background with border.

    Args:
        surf:  Target surface.
        rect:  Card position and size.
        theme: Current color theme.
    """
    # Background.
    card_surf = pygame.Surface((rect.w, rect.h), pygame.SRCALPHA)
    pygame.draw.rect(card_surf, theme.card_bg,
                     (0, 0, rect.w, rect.h), border_radius=12)
    # Border.
    pygame.draw.rect(card_surf, theme.card_border,
                     (0, 0, rect.w, rect.h), width=1, border_radius=12)
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
    surf.blit(text, (rect.x + 10, rect.y + 8))
    return rect.y + 8 + text.get_height() + 4


# ---------------------------------------------------------------------------
# Clock tile
# ---------------------------------------------------------------------------

def draw_clock(surf: pygame.Surface, rect: pygame.Rect,
               data: DataPoller, theme: Theme) -> None:
    """Draw the main clock display.

    Shows time in 12-hour format with date below.
    No card background — the clock stands alone.
    """
    now: datetime = datetime.now()
    hour: int = now.hour % 12 or 12
    minute: int = now.minute
    period: str = "AM" if now.hour < 12 else "PM"

    # Time — large, clean sans-serif.
    time_str: str = f"{hour}:{minute:02d}"
    time_font = _sans(max(112, int(rect.h * 0.94)))
    time_surf = time_font.render(time_str, True, theme.clock)
    time_rect = time_surf.get_rect(
        centerx=rect.centerx, centery=rect.centery - rect.h * 7 // 4 // 8,
    )
    surf.blit(time_surf, time_rect)

    # AM/PM — smaller, to the right of time.
    ampm_font = _sans(max(20, rect.h * 7 // 4 // 8))
    ampm_surf = ampm_font.render(period, True, theme.ampm)
    surf.blit(ampm_surf, (time_rect.right + 6, time_rect.top + 4))

    # Date — below the time.
    date_str: str = now.strftime("%A, %B %-d")
    date_font = _sans(max(16, rect.h * 7 // 4 // 7))
    date_surf = date_font.render(date_str, True, theme.date)
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
        surf.blit(name_surf, (rect.x + 12, y))
        surf.blit(status_surf, (rect.right - 12 - status_surf.get_width(), y))
        y += row_h

    # Summary line + page dots combined on one line at bottom.
    devices: int = health.get("devices", 0)
    schedules: int = health.get("schedules", 0)
    summary: str = f"{devices} devices · {schedules} sched"
    sum_font = _sans(max(11, rect.h * 7 // 4 // 12))
    sum_surf = sum_font.render(summary, True, theme.dim)
    sum_y: int = rect.bottom - sum_surf.get_height() - 4
    surf.blit(sum_surf, (rect.x + 12, sum_y))

    # Page dots — right side of summary line.
    if total_pages > 1:
        dot_y: int = sum_y + sum_surf.get_height() // 2
        dot_total_w: int = total_pages * 10
        dot_x: int = rect.right - 12 - dot_total_w
        for p in range(total_pages):
            color = theme.text if p == _health_page % total_pages else theme.dim
            pygame.draw.circle(surf, color, (dot_x + p * 10 + 3, dot_y), 2)


# ---------------------------------------------------------------------------
# Locks tile
# ---------------------------------------------------------------------------

def draw_locks(surf: pygame.Surface, rect: pygame.Rect,
               data: DataPoller, theme: Theme) -> None:
    """Draw lock status tile."""
    locks_data: Optional[dict] = data.get("locks")
    if locks_data is None:
        return

    _draw_card(surf, rect, theme)
    y: int = _draw_title(surf, rect, "Locks", theme)

    locks: list = locks_data.get("locks", [])
    font = _sans(max(13, rect.h * 7 // 4 // 10))
    row_h: int = font.get_height() + 4

    small_font = _sans(max(11, rect.h * 7 // 4 // 14))

    for lock in locks:
        name: str = lock.get("name", "?")
        locked: bool = lock.get("locked", False)
        batt: int = lock.get("battery", 0)

        state_str: str = "Locked" if locked else "OPEN"
        color = theme.locked if locked else theme.unlocked

        # Name + state on first line.
        label = font.render(f"{name}: {state_str}", True, color)
        surf.blit(label, (rect.x + 12, y))
        y += font.get_height() + 5

        # Battery on second line, smaller.
        batt_surf = small_font.render(f"Battery {batt}%", True, theme.dim)
        surf.blit(batt_surf, (rect.x + 20, y))
        y += small_font.get_height() + 8


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
    """Draw current weather conditions."""
    weather: Optional[dict] = data.get("weather")
    if weather is None:
        return

    current: dict = weather.get("current", {})
    if not current:
        return

    _draw_card(surf, rect, theme)
    y: int = _draw_title(surf, rect, "Weather", theme)

    temp: float = current.get("temperature_2m", 0)
    humidity: float = current.get("relative_humidity_2m", 0)
    wind: float = current.get("wind_speed_10m", 0)
    code: int = current.get("weather_code", 0)
    condition: str = _WMO.get(code, "Unknown")

    # Temperature — big.
    temp_font = _sans(max(28, rect.h * 7 // 4 // 4))
    temp_surf = temp_font.render(f"{temp:.0f}°F", True, theme.temp)
    surf.blit(temp_surf, (rect.x + 12, y))
    y += temp_surf.get_height() + 2

    # Condition.
    cond_font = _sans(max(14, rect.h * 7 // 4 // 8))
    cond_surf = cond_font.render(condition, True, theme.text)
    surf.blit(cond_surf, (rect.x + 12, y))
    y += cond_surf.get_height() + 2

    # Humidity + wind — one per line.
    detail_font = _sans(max(12, rect.h * 7 // 4 // 10))
    hum_surf = detail_font.render(f"Humidity {humidity:.0f}%", True, theme.dim)
    surf.blit(hum_surf, (rect.x + 12, y))
    y += detail_font.get_height() + 2
    wind_surf = detail_font.render(f"Wind {wind:.0f} mph", True, theme.dim)
    surf.blit(wind_surf, (rect.x + 12, y))


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
    surf.blit(val_surf, (rect.x + 12, y))
    y += val_surf.get_height() + 2

    # Category label.
    cat_font = _sans(max(14, rect.h * 7 // 4 // 8))
    cat_surf = cat_font.render(cat_name, True, cat_color)
    surf.blit(cat_surf, (rect.x + 12, y))
    y += cat_surf.get_height() + 2

    # PM2.5 + PM10 — one per line.
    pm25: float = current.get("pm2_5", 0)
    pm10: float = current.get("pm10", 0)
    detail_font = _sans(max(12, rect.h * 7 // 4 // 10))
    pm25_surf = detail_font.render(f"PM2.5: {pm25:.0f}", True, theme.dim)
    surf.blit(pm25_surf, (rect.x + 12, y))
    y += detail_font.get_height() + 2
    pm10_surf = detail_font.render(f"PM10: {pm10:.0f}", True, theme.dim)
    surf.blit(pm10_surf, (rect.x + 12, y))


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
    surf.blit(name_surf, (rect.x + 12, y))
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
        surf.blit(val_surf, (rect.x + 12, y))
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
        surf.blit(det_surf, (rect.x + 12, y))

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
    """Draw NWS severe weather alerts."""
    alerts: Optional[list] = data.get("alerts")
    if not alerts:
        return

    _draw_card(surf, rect, theme)

    # Blinking red title.
    blink: bool = int(time.monotonic() * 2) % 2 == 0
    title_color = theme.bad if blink else theme.warn
    font = _sans(max(14, rect.h * 7 // 4 // 8))
    title = font.render(f"⚠ {len(alerts)} Alert(s)", True, title_color)
    surf.blit(title, (rect.x + 10, rect.y + 8))
    y: int = rect.y + 8 + title.get_height() + 4

    # Show first 2 alerts.
    detail_font = _sans(max(11, rect.h * 7 // 4 // 12))
    for alert in alerts[:2]:
        props = alert.get("properties", {})
        event: str = props.get("event", "Unknown")
        headline: str = props.get("headline", "")
        # Truncate to fit.
        max_chars: int = max(20, rect.w // 8)
        display: str = event if len(event) <= max_chars else event[:max_chars - 2] + "…"
        alert_surf = detail_font.render(display, True, theme.warn)
        surf.blit(alert_surf, (rect.x + 12, y))
        y += detail_font.get_height() + 2


# ---------------------------------------------------------------------------
# Security / alarm tile
# ---------------------------------------------------------------------------

def draw_security(surf: pygame.Surface, rect: pygame.Rect,
                  data: DataPoller, theme: Theme) -> None:
    """Draw alarm panel state."""
    security: Optional[dict] = data.get("security")
    if security is None:
        return

    _draw_card(surf, rect, theme)
    y: int = _draw_title(surf, rect, "Security", theme)

    alarm_state: str = security.get("alarm", "unknown")
    color = theme.ok if alarm_state == "disarmed" else theme.bad

    state_font = _sans(max(18, rect.h * 7 // 4 // 6))
    state_surf = state_font.render(alarm_state.upper(), True, color)
    surf.blit(state_surf, (
        rect.centerx - state_surf.get_width() // 2, y,
    ))
    y += state_surf.get_height() + 6

    # Door sensors.
    doors: list = security.get("doors", [])
    door_font = _sans(max(12, rect.h * 7 // 4 // 10))
    for door in doors:
        name: str = door.get("name", "?")
        is_open: bool = door.get("open", False)
        state_str: str = "OPEN" if is_open else "Closed"
        door_color = theme.bad if is_open else theme.ok
        door_surf = door_font.render(f"{name}: {state_str}", True, door_color)
        surf.blit(door_surf, (rect.x + 12, y))
        y += door_font.get_height() + 8
        if y > rect.bottom - 10:
            break
