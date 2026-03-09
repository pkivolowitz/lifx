// HSBKColor.swift
// GlowUp — LIFX Remote Control
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import SwiftUI

/// Maximum value for LIFX HSBK components (16-bit unsigned).
private let hsbkMax: Double = 65535.0

/// Convert a LIFX HSBK zone color to a SwiftUI ``Color``.
///
/// LIFX uses 16-bit unsigned integers for hue, saturation, and
/// brightness (0–65535), with hue wrapping at 65535 = 360°.
/// This function normalizes to the 0–1 range that SwiftUI expects.
///
/// When saturation is zero the bulb is in "white" mode and the
/// kelvin value determines warmth, but for visualization purposes
/// we simply show the desaturated brightness.
///
/// - Parameter zone: The HSBK color to convert.
/// - Returns: A SwiftUI ``Color`` representing the zone's color.
func hsbkToColor(_ zone: ZoneColor) -> Color {
    let hue = Double(zone.h) / hsbkMax
    let saturation = Double(zone.s) / hsbkMax
    let brightness = Double(zone.b) / hsbkMax

    return Color(
        hue: hue,
        saturation: saturation,
        brightness: brightness
    )
}
