// Device.swift
// GlowUp — LIFX Remote Control
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import Foundation

/// A configured LIFX device as reported by the server's
/// ``GET /api/devices`` endpoint.
struct Device: Codable, Identifiable, Hashable {
    /// Device IP address (used as the unique identifier).
    let ip: String

    /// MAC address as a colon-separated hex string.
    let mac: String

    /// Human-readable device name assigned in the LIFX app.
    let label: String?

    /// User-assigned custom display name (set from this app).
    let nickname: String?

    /// Friendly product name (e.g., "String Light", "A19").
    let product: String?

    /// Device group name from the LIFX app.
    let group: String?

    /// Number of individually addressable zones.
    let zones: Int?

    /// Whether this device supports the multizone protocol.
    let isMultizone: Bool?

    /// Name of the currently running effect, if any.
    let currentEffect: String?

    /// Conform to ``Identifiable`` using the device IP.
    var id: String { ip }

    /// The best available display name: nickname, then label, then IP.
    var displayName: String {
        if let nickname = nickname, !nickname.isEmpty { return nickname }
        return label ?? ip
    }

    /// Coding keys to match the server's snake_case JSON.
    enum CodingKeys: String, CodingKey {
        case ip, mac, label, nickname, product, group, zones
        case isMultizone = "is_multizone"
        case currentEffect = "current_effect"
    }
}

/// Wrapper for the ``GET /api/devices`` JSON response.
struct DeviceListResponse: Codable {
    /// List of configured devices.
    let devices: [Device]
}
