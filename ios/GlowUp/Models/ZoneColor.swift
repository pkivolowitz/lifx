// ZoneColor.swift
// GlowUp — LIFX Remote Control
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import Foundation

/// HSBK color for a single zone as reported by the server.
///
/// All values use the LIFX 16-bit range (0–65535) except kelvin
/// (1500–9000).
struct ZoneColor: Codable {
    /// Hue (0–65535).
    let h: Int

    /// Saturation (0–65535).
    let s: Int

    /// Brightness (0–65535).
    let b: Int

    /// Color temperature in Kelvin (1500–9000).
    let k: Int
}

/// Wrapper for the zone color snapshot and SSE stream data.
struct ZoneColorResponse: Codable {
    /// List of zone colors ordered by zone index.
    let zones: [ZoneColor]
}
