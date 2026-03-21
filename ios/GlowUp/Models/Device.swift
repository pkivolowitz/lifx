// Device.swift
// GlowUp — LIFX Remote Control
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import Foundation

/// A configured LIFX device or virtual group as reported by the
/// server's ``GET /api/devices`` endpoint.
///
/// Virtual groups combine multiple physical devices into a single
/// unified zone canvas.  They are identified by ``group:<name>``
/// instead of an IP address.
struct Device: Codable, Identifiable, Hashable {
    /// Device identifier: an IP address for physical devices, or
    /// ``group:<name>`` for virtual multizone groups.
    let ip: String

    /// MAC address as a colon-separated hex string ("virtual" for groups).
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

    /// Whether this entry represents a virtual multizone group.
    let isGroup: Bool?

    /// Power state as tracked by the server (true = on).
    let power: Bool?

    /// IP addresses of member devices (groups only).
    let memberIps: [String]?

    /// The best available stable identifier for API calls.
    ///
    /// Virtual groups always use ``ip`` (which holds ``group:<name>``).
    /// Physical devices prefer label → MAC → IP.  Labels are
    /// human-readable and survive DHCP changes.  MACs are stable but
    /// opaque.  IPs are a last resort.  Empty strings are skipped.
    var deviceId: String {
        if isVirtualGroup { return ip }
        if let label = label, !label.isEmpty { return label }
        if !mac.isEmpty && mac != "virtual" { return mac }
        return ip
    }

    /// Conform to ``Identifiable`` using the most stable identifier.
    var id: String { deviceId }

    /// True if this is a virtual multizone group.
    var isVirtualGroup: Bool { isGroup ?? false }

    /// The best available display name: nickname, then label, then IP.
    /// Virtual groups are prefixed with "Group: " for clarity.
    var displayName: String {
        let base: String
        if let nickname = nickname, !nickname.isEmpty {
            base = nickname
        } else {
            base = label ?? ip
        }
        return isVirtualGroup ? "Group: \(base)" : base
    }

    /// Coding keys to match the server's snake_case JSON.
    enum CodingKeys: String, CodingKey {
        case ip, mac, label, nickname, product, group, zones, power
        case isMultizone = "is_multizone"
        case currentEffect = "current_effect"
        case isGroup = "is_group"
        case memberIps = "member_ips"
    }
}

/// Wrapper for the ``GET /api/devices`` JSON response.
struct DeviceListResponse: Codable {
    /// List of configured devices.
    let devices: [Device]
}
