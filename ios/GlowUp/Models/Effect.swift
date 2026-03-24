// Effect.swift
// GlowUp — LIFX Remote Control
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import Foundation

/// Metadata for a single effect parameter as reported by the server.
///
/// Mirrors the Python ``Param`` class: provides default, min, max,
/// description, type, and optional choices for auto-generating UI.
struct EffectParam: Codable {
    /// Default value (type matches ``type`` field).
    let `default`: AnyCodableValue

    /// Minimum allowed value (numeric params only).
    let min: AnyCodableValue?

    /// Maximum allowed value (numeric params only).
    let max: AnyCodableValue?

    /// Human-readable description of the parameter.
    let description: String

    /// Python type name: "float", "int", or "str".
    let type: String

    /// Allowed values for enum-like parameters.
    let choices: [String]?
}

/// An effect with its description and parameter metadata.
///
/// Decoded from the ``GET /api/effects`` response, which maps
/// effect names to their metadata.
struct Effect: Identifiable {
    /// Effect name (e.g., "cylon", "aurora").
    let name: String

    /// Human-readable one-line description.
    let description: String

    /// Parameter definitions keyed by parameter name.
    let params: [String: EffectParam]

    /// Whether this effect is hidden by default (name starts with ``_``).
    let hidden: Bool

    /// Device types this effect is designed for ("bulb", "strip", "matrix").
    ///
    /// Universal effects contain all three types.  Clients use this
    /// for UI filtering — the engine does not enforce affinity.
    let affinity: [String]

    /// Conform to ``Identifiable`` using the effect name.
    var id: String { name }

    /// Whether this effect supports the given device type.
    func supportsDeviceType(_ type: String) -> Bool {
        affinity.contains(type)
    }
}

/// Wrapper for the ``GET /api/effects`` JSON response.
///
/// The server returns ``{"effects": {"name": {...}, ...}}``.
/// Custom decoding converts this into a sorted array of ``Effect``.
struct EffectListResponse: Codable {
    /// Effects keyed by name, each containing description and params.
    let effects: [String: EffectDetail]

    /// The inner detail structure for each effect.
    struct EffectDetail: Codable {
        /// Human-readable description.
        let description: String

        /// Parameter definitions keyed by name.
        let params: [String: EffectParam]

        /// Whether this effect is hidden by default (name starts with ``_``).
        let hidden: Bool

        /// Device types this effect supports ("bulb", "strip", "matrix").
        let affinity: [String]
    }

    /// Convert to a sorted array of ``Effect`` for display.
    func toEffectArray() -> [Effect] {
        effects.map { name, detail in
            Effect(
                name: name,
                description: detail.description,
                params: detail.params,
                hidden: detail.hidden,
                affinity: detail.affinity
            )
        }
        .sorted { $0.name < $1.name }
    }
}

/// A type-erased Codable value for handling mixed JSON types
/// (int, float, string, null) in effect parameter metadata.
enum AnyCodableValue: Codable, CustomStringConvertible {
    case int(Int)
    case double(Double)
    case string(String)
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
            return
        }
        // Try int first (JSON integers decode as both Int and Double).
        if let intVal = try? container.decode(Int.self) {
            self = .int(intVal)
            return
        }
        if let doubleVal = try? container.decode(Double.self) {
            self = .double(doubleVal)
            return
        }
        if let strVal = try? container.decode(String.self) {
            self = .string(strVal)
            return
        }
        self = .null
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .int(let v): try container.encode(v)
        case .double(let v): try container.encode(v)
        case .string(let v): try container.encode(v)
        case .null: try container.encodeNil()
        }
    }

    /// The underlying value as a ``Double``, or ``nil``.
    var doubleValue: Double? {
        switch self {
        case .int(let v): return Double(v)
        case .double(let v): return v
        default: return nil
        }
    }

    /// The underlying value as a ``String``, or ``nil``.
    var stringValue: String? {
        switch self {
        case .string(let v): return v
        default: return nil
        }
    }

    /// Human-readable description for display in UI labels.
    var description: String {
        switch self {
        case .int(let v): return "\(v)"
        case .double(let v):
            // Show one decimal place for cleaner display.
            return String(format: "%.1f", v)
        case .string(let v): return v
        case .null: return "—"
        }
    }
}
