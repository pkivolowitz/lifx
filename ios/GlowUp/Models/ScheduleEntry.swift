// ScheduleEntry.swift
// GlowUp — LIFX Remote Control
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import Foundation

/// A schedule entry as reported by ``GET /api/schedule``.
///
/// Each entry represents a time window during which an effect runs
/// on a device group.  The server resolves symbolic times (sunrise,
/// sunset) into clock times for today.
struct ScheduleEntry: Codable, Identifiable {
    /// Zero-based index in the schedule array (used for enable/disable).
    let index: Int

    /// Human-readable name (e.g., "porch evening aurora").
    let name: String

    /// Device group this entry targets.
    let group: String

    /// Effect name (e.g., "aurora", "cylon").
    let effect: String

    /// Raw start time spec (e.g., "sunset-30m", "23:00").
    let start: String

    /// Raw stop time spec.
    let stop: String

    /// Resolved start time for today (e.g., "18:42"), or nil.
    let startResolved: String?

    /// Resolved stop time for today, or nil.
    let stopResolved: String?

    /// Day-of-week letter string (e.g., "MTWRF"), empty for daily.
    let days: String

    /// Human-readable day label (e.g., "Weekdays", "Daily").
    let daysDisplay: String

    /// Whether this entry is enabled.
    let enabled: Bool

    /// Whether this entry is currently active (running right now).
    let active: Bool

    /// Conform to ``Identifiable`` using the index.
    var id: Int { index }

    /// Coding keys to match the server's snake_case JSON.
    enum CodingKeys: String, CodingKey {
        case index, name, group, effect, start, stop
        case startResolved = "start_resolved"
        case stopResolved = "stop_resolved"
        case days
        case daysDisplay = "days_display"
        case enabled, active
    }
}

/// Wrapper for the ``GET /api/schedule`` JSON response.
struct ScheduleResponse: Codable {
    /// Ordered list of schedule entries.
    let entries: [ScheduleEntry]
}
