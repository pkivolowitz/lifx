"""Newton's Cradle pendulum simulation with Phong-shaded spheres.

Models the classic Newton's Cradle: a row of steel balls suspended side by
side on strings.  The outermost balls swing alternately — the right ball
swings out and returns, strikes the row, and the left ball swings out in
turn.  Middle balls remain stationary throughout.

Ball shading
------------
Each ball is rendered as a 3-D sphere using a full Phong illumination model:

    I = I_a · ambient
      + I_d · max(0, N · L)              (Lambertian diffuse)
      + I_s · max(0, R · V)^shininess    (Phong specular)

The light source is fixed at 25° from vertical toward the left (upper-left
illumination), so the specular highlight sits on the left-of-centre of each
ball regardless of its position on the strip.  The specular bloom is blended
toward pure white via ``lerp_color`` (CIELAB) so the colour transition from
ball surface to hot-spot is perceptually smooth on any hue.

Layout and sizing
-----------------
Ball width is auto-computed so the full cradle (rest row plus swing arc on
each end) fits within the strip.  The gap between adjacent balls defaults to
3 zones — one LIFX string-light bulb — keeping separators aligned with
physical bulb boundaries.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import math

from colorspace import lerp_color

from . import (
    DEVICE_TYPE_STRIP,
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX,
    hue_to_u16, pct_to_u16,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Light source direction — unit vector FROM surface point TO light.
# 25° from vertical toward the left gives a classic upper-left studio look.
_LIGHT_ANGLE_RAD: float = math.radians(25.0)
LIGHT_X: float = -math.sin(_LIGHT_ANGLE_RAD)   # ≈ −0.423  (leftward component)
LIGHT_Y: float =  math.cos(_LIGHT_ANGLE_RAD)   # ≈  0.906  (upward component)

# View direction: viewer is directly in front (0, 1).
VIEW_Y: float = 1.0   # only the y-component matters for R·V

# Phong illumination weights.
# ambient + diffuse_peak * 1 ≈ 0.75; specular adds a bright bloom on top.
AMBIENT_FACTOR:    float = 0.10   # floor brightness on the shadowed side
DIFFUSE_FACTOR:    float = 0.65   # Lambertian peak weight
SPECULAR_FACTOR:   float = 0.80   # peak specular relative to max brightness

# Skip the CIELAB lerp call for near-zero specular (hot-path optimisation).
SPECULAR_THRESHOLD: float = 0.02

# Minimum ball width in zones (prevents degenerate single-zone balls).
MIN_BALL_WIDTH: int = 3

# Sentinel value meaning "compute automatically".
AUTO: int = 0


# ---------------------------------------------------------------------------
# Effect
# ---------------------------------------------------------------------------

class NewtonsCradle(Effect):
    """Newton's Cradle — alternating pendulum balls with Phong sphere shading.

    Five steel-coloured balls hang in a row separated by one-bulb gaps.
    The rightmost and leftmost balls swing alternately; the middle balls
    stay still.  Each ball is rendered as a 3-D sphere so the specular
    highlight glides across its surface as it swings.

    Shading tips
    ------------
    * ``--sat 0``           Pure brushed-steel (brightness only, no hue).
    * ``--sat 80 --hue 45`` Gold balls.
    * ``--hue 200 --sat 30`` Blue steel / titanium.
    * ``--shininess 8``     Matte / rubber balls.
    * ``--shininess 60``    Mirror-chrome highlight.
    * ``--speed 3.0``       Slow-motion cradle.
    """

    name: str = "newtons_cradle"
    description: str = (
        "Newton's Cradle — alternating pendulum balls with Phong sphere shading"
    )
    affinity: frozenset[str] = frozenset({DEVICE_TYPE_STRIP})

    # ------------------------------------------------------------------
    # Parameters
    # ------------------------------------------------------------------

    num_balls = Param(
        5, min=2, max=10,
        description="Number of balls in the cradle",
    )
    ball_width = Param(
        AUTO, min=0, max=30,
        description=(
            "Zones per ball; 0 = auto-size so the full swing arc fits the strip"
        ),
    )
    gap = Param(
        1, min=0, max=9,
        description=(
            "Zones between adjacent balls at rest. "
            "0 = touching (Phong dark-edges alone separate the balls); "
            "1 = one-zone sliver (default); "
            "3 = one LIFX bulb gap (maximally visible separation)"
        ),
    )
    swing = Param(
        AUTO, min=0, max=80,
        description=(
            "Swing arc amplitude in zones beyond the row end; "
            "0 = auto = one ball-width"
        ),
    )
    speed = Param(
        1.5, min=0.3, max=10.0,
        description="Full period in seconds (left-swing + right-swing = one cycle)",
    )
    hue = Param(
        200.0, min=0.0, max=360.0,
        description="Ball base hue in degrees (200 = steel-teal, 45 = gold, 0 = red)",
    )
    sat = Param(
        15, min=0, max=100,
        description="Ball base saturation percent (0 = pure gray / steel)",
    )
    brightness = Param(
        90, min=1, max=100,
        description="Maximum ball brightness percent",
    )
    shininess = Param(
        25, min=1, max=100,
        description=(
            "Specular highlight sharpness: "
            "8=matte, 25=brushed metal, 60=chrome, 100=mirror"
        ),
    )
    kelvin = Param(
        KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
        description="Color temperature in Kelvin",
    )

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one animation frame of the Newton's Cradle.

        Computes ball positions from the pendulum phase, then for each
        zone determines which ball (if any) covers it and applies Phong
        shading in the ball's local coordinate frame.

        Args:
            t:          Seconds elapsed since the effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        n:   int = int(self.num_balls)
        gap: int = int(self.gap)

        # Resolve layout — may depend on zone_count.
        bw, amp = self._resolve_layout(zone_count, n, gap)

        # At-rest ball centres (float zone positions).
        rest: list[float] = self._rest_centres(zone_count, n, bw, gap)

        # --- Pendulum phase ---
        # Phase 0 → 0.5 : rightmost ball swings rightward and returns.
        # Phase 0.5 → 1.0: leftmost  ball swings leftward  and returns.
        # sin(phase * 2π) gives smooth sinusoidal motion peaking at phase=0.25
        # (rightward) and phase=0.75 (leftward), with zero velocity at the ends
        # so the collision moment (phase=0 and 0.5) has maximum speed —
        # physically correct for a pendulum striking at the bottom of its arc.
        phase: float = (t % self.speed) / self.speed

        centres: list[float] = list(rest)
        if phase < 0.5:
            # Right ball swinging out to the right.
            centres[-1] = rest[-1] + amp * math.sin(phase * 2.0 * math.pi)
        else:
            # Left ball swinging out to the left.
            centres[0] = rest[0] - amp * math.sin((phase - 0.5) * 2.0 * math.pi)

        # --- Precompute per-frame colour constants ---
        hue_u16: int = hue_to_u16(self.hue)
        sat_u16: int = pct_to_u16(self.sat)
        max_bri: int = pct_to_u16(self.brightness)
        shin:    int = int(self.shininess)

        # --- Rasterise ---
        half:   float      = bw / 2.0
        dead:   HSBK       = (0, 0, 0, self.kelvin)
        colors: list[HSBK] = [dead] * zone_count

        for cx in centres:
            # Only iterate over the zone range this ball can possibly cover.
            lo: int = max(0,          int(math.floor(cx - half)))
            hi: int = min(zone_count, int(math.ceil( cx + half)) + 1)

            for z in range(lo, hi):
                # Normalised horizontal position in the ball: −1 (left edge)
                # to +1 (right edge).  Zones outside the unit circle are skipped.
                x_rel: float = (z - cx) / half
                if abs(x_rel) >= 1.0:
                    continue

                colors[z] = self._shade(x_rel, hue_u16, sat_u16, max_bri, shin)

        return colors

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_layout(
        self, zone_count: int, n: int, gap: int
    ) -> tuple[int, float]:
        """Compute ball width and swing amplitude, resolving AUTO values.

        When both are AUTO the layout is solved in two passes:

        1. Primary: divide the strip evenly between balls and two swing arms
           (treating swing = bw as a baseline):

               bw = (zone_count − (n−1)×gap) ÷ (n + 2)

        2. Secondary: distribute leftover zones (floor-division remainder)
           to the two swing arms so the pendulum travels as far as the
           strip permits — important on short 36-zone strips where the
           primary quotient is small.

        When only one is AUTO it is derived from the fixed value.

        Args:
            zone_count: Total zones available.
            n:          Number of balls.
            gap:        Zones between adjacent balls.

        Returns:
            ``(ball_width_zones, swing_amplitude_zones)``
        """
        bw: int   = int(self.ball_width)
        sw: float = float(self.swing)
        gap_total: int = (n - 1) * gap

        if bw == AUTO and sw == AUTO:
            # Primary solve: allocate equal space to each ball and each swing
            # arm — i.e. treat swing = bw and divide the strip evenly:
            #   zone_count ≥ (n + 2) × bw + gap_total
            bw = max(MIN_BALL_WIDTH, (zone_count - gap_total) // (n + 2))
            # Secondary pass: floor-division leaves leftover zones unused.
            # Distribute them across the two swing arms so the pendulum
            # travels as far as the strip allows — critical on short strips
            # (e.g. 36 zones) where the primary quotient is small.
            leftover: int = zone_count - (n * bw + gap_total + 2 * bw)
            sw = float(bw) + max(0, leftover) // 2
        elif bw == AUTO:
            bw = max(MIN_BALL_WIDTH,
                     (zone_count - gap_total - 2 * int(sw)) // n)
        elif sw == AUTO:
            sw = float(bw)

        return bw, sw

    def _rest_centres(
        self, zone_count: int, n: int, bw: int, gap: int
    ) -> list[float]:
        """Return the at-rest centre position (float zone index) for each ball.

        The cradle is centred within the strip.

        Args:
            zone_count: Total zones.
            n:          Number of balls.
            bw:         Ball width in zones.
            gap:        Zones between adjacent balls.

        Returns:
            List of *n* float centre positions.
        """
        total:   int   = n * bw + (n - 1) * gap
        origin:  float = (zone_count - total) / 2.0
        return [origin + i * (bw + gap) + bw / 2.0 for i in range(n)]

    def _shade(
        self,
        x_rel:   float,
        hue_u16: int,
        sat_u16: int,
        max_bri: int,
        shin:    int,
    ) -> HSBK:
        """Compute a Phong-shaded HSBK for one zone on a ball.

        The sphere surface normal at horizontal position *x_rel* is derived
        from the unit-circle cross-section:  N = (x_rel, √(1 − x_rel²)).
        The light direction is the module-level constant (upper-left, 25°
        from vertical).

        Lambertian diffuse:
            I_d = max(0, N · L)

        Phong specular (view direction V = (0, 1), so R · V = R_y):
            R   = 2 (N · L) N − L
            I_s = max(0, R_y)^shininess

        The specular component is blended toward a white highlight via
        ``lerp_color`` (CIELAB) so the colour transition is perceptually
        natural on any hue.

        Args:
            x_rel:   Normalised horizontal position on the ball (−1 to +1).
            hue_u16: Ball base hue (LIFX u16).
            sat_u16: Ball base saturation (LIFX u16).
            max_bri: Maximum brightness (LIFX u16).
            shin:    Phong shininess exponent.

        Returns:
            HSBK tuple for this zone.
        """
        # Sphere surface normal at this horizontal slice.
        y: float = math.sqrt(max(0.0, 1.0 - x_rel * x_rel))

        # --- Lambertian diffuse ---
        n_dot_l: float = x_rel * LIGHT_X + y * LIGHT_Y
        diffuse: float = max(0.0, n_dot_l)

        # --- Phong specular ---
        # R = 2(N·L)N − L; since V = (0,1), R·V = R_y.
        r_y: float      = 2.0 * n_dot_l * y - LIGHT_Y
        specular: float = max(0.0, r_y) ** shin

        # --- Ambient + diffuse brightness ---
        intensity: float = AMBIENT_FACTOR + DIFFUSE_FACTOR * diffuse
        bri: int         = int(min(intensity, 1.0) * max_bri)

        base: HSBK = (hue_u16, sat_u16, bri, self.kelvin)

        # --- Specular bloom: blend toward white via CIELAB ---
        if specular >= SPECULAR_THRESHOLD:
            # Brightness of the white highlight scales with specular intensity.
            spec_bri: int  = int(min(specular * SPECULAR_FACTOR, 1.0) * max_bri)
            white:    HSBK = (0, 0, spec_bri, self.kelvin)
            # Blend factor: 0 = pure base colour, 1 = pure white highlight.
            blend: float = min(1.0, specular * SPECULAR_FACTOR)
            return lerp_color(base, white, blend)

        return base
