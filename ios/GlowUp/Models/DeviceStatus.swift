// DeviceStatus.swift
// GlowUp — LIFX Remote Control
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import Foundation

/// Status of a device's effect engine as reported by the server's
/// ``GET /api/devices/{ip}/status`` endpoint.
struct DeviceStatus: Codable {
    /// Whether the engine's render loop is active.
    let running: Bool

    /// Name of the currently playing effect, or ``nil``.
    let effect: String?

    /// Current parameter values of the running effect.
    let params: [String: AnyCodableValue]

    /// Target frames per second of the render loop.
    let fps: Int
}
