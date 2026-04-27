// ModelDecodingTests.swift
// GlowUpTests
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import XCTest
@testable import GlowUp

/// Tests for JSON decoding of server response models.
///
/// Each test feeds a known JSON payload (matching the real server
/// format) into the Codable decoder and verifies that every field
/// is extracted correctly.  This catches drift between the server
/// response format and the iOS model layer.
final class ModelDecodingTests: XCTestCase {

    private let decoder = JSONDecoder()

    // MARK: - Device decoding

    /// Decode a physical LIFX device from a real server response.
    func testDecodeDevice_physical() throws {
        let json = """
        {
            "ip": "192.0.2.45",
            "label": "PORCH STRING LIGHTS",
            "nickname": null,
            "product": "String Light US",
            "zones": 102,
            "is_multizone": true,
            "current_effect": "aurora",
            "overridden": false,
            "is_group": false,
            "is_matrix": false,
            "mac": "d0:73:d5:d4:79:9c",
            "group": "porch"
        }
        """.data(using: .utf8)!

        let device = try decoder.decode(Device.self, from: json)
        XCTAssertEqual(device.ip, "192.0.2.45")
        XCTAssertEqual(device.label, "PORCH STRING LIGHTS")
        XCTAssertNil(device.nickname)
        XCTAssertEqual(device.product, "String Light US")
        XCTAssertEqual(device.zones, 102)
        XCTAssertEqual(device.isMultizone, true)
        XCTAssertEqual(device.isMatrix, false)
        XCTAssertEqual(device.currentEffect, "aurora")
        XCTAssertEqual(device.isGroup, false)
        XCTAssertEqual(device.mac, "d0:73:d5:d4:79:9c")
        XCTAssertEqual(device.group, "porch")
        XCTAssertNil(device.memberIps)
        XCTAssertFalse(device.isVirtualGroup)
        XCTAssertEqual(device.deviceType, "strip")
        // deviceId should prefer label.
        XCTAssertEqual(device.deviceId, "PORCH STRING LIGHTS")
    }

    /// Decode a virtual group device.
    func testDecodeDevice_virtualGroup() throws {
        let json = """
        {
            "ip": "group:all",
            "label": "all",
            "nickname": null,
            "product": "117-zone virtual multizone",
            "zones": 117,
            "is_multizone": true,
            "current_effect": null,
            "overridden": false,
            "is_group": true,
            "mac": "",
            "group": "all",
            "member_ips": ["192.0.2.45", "192.0.2.18"]
        }
        """.data(using: .utf8)!

        let device = try decoder.decode(Device.self, from: json)
        XCTAssertEqual(device.ip, "group:all")
        XCTAssertTrue(device.isVirtualGroup)
        XCTAssertEqual(device.memberIps?.count, 2)
        XCTAssertEqual(device.memberIps?.first, "192.0.2.45")
        // deviceId: label "all" wins over "group:all".
        XCTAssertEqual(device.deviceId, "all")
    }

    /// Decode a matrix device — deviceType should be "matrix".
    func testDecodeDevice_matrix() throws {
        let json = """
        {
            "ip": "192.0.2.99",
            "mac": "d0:73:d5:aa:bb:cc",
            "label": "Luna",
            "nickname": null,
            "product": "LIFX Tile",
            "zones": 35,
            "is_multizone": false,
            "is_matrix": true,
            "current_effect": null,
            "overridden": false,
            "is_group": false,
            "group": ""
        }
        """.data(using: .utf8)!

        let device = try decoder.decode(Device.self, from: json)
        XCTAssertEqual(device.isMatrix, true)
        XCTAssertEqual(device.deviceType, "matrix")
    }

    /// Decode a single-zone bulb — deviceType should be "bulb".
    func testDecodeDevice_bulb() throws {
        let json = """
        {
            "ip": "192.0.2.5",
            "mac": "d0:73:d5:6a:88:79",
            "label": "Living Room Floor Lamp",
            "nickname": null,
            "product": "Mini White",
            "zones": 1,
            "is_multizone": false,
            "is_matrix": false,
            "current_effect": null,
            "overridden": false,
            "is_group": false,
            "group": ""
        }
        """.data(using: .utf8)!

        let device = try decoder.decode(Device.self, from: json)
        XCTAssertEqual(device.deviceType, "bulb")
    }

    /// Decode a device with no label (unregistered bulb).
    func testDecodeDevice_noLabel() throws {
        let json = """
        {
            "ip": "192.0.2.11",
            "label": "192.0.2.11",
            "nickname": null,
            "product": "Mini White",
            "zones": 1,
            "is_multizone": false,
            "current_effect": null,
            "overridden": false,
            "is_group": false,
            "mac": "d0:73:d5:6c:1e:10",
            "group": ""
        }
        """.data(using: .utf8)!

        let device = try decoder.decode(Device.self, from: json)
        // The label is the IP itself — deviceId should still use it
        // (the label field is non-empty, even if it looks like an IP).
        XCTAssertEqual(device.deviceId, "192.0.2.11")
    }

    /// Unknown fields from the server (like "overridden") are silently ignored.
    func testDecodeDevice_extraFieldsIgnored() throws {
        let json = """
        {
            "ip": "192.0.2.5",
            "mac": "d0:73:d5:6a:88:79",
            "label": "Living Room Floor Lamp",
            "nickname": null,
            "product": "Mini White",
            "zones": 1,
            "is_multizone": false,
            "current_effect": null,
            "overridden": false,
            "is_group": false,
            "group": "",
            "future_field": "should be ignored"
        }
        """.data(using: .utf8)!

        // Should not throw despite the unknown "future_field".
        let device = try decoder.decode(Device.self, from: json)
        XCTAssertEqual(device.label, "Living Room Floor Lamp")
    }

    /// Decode a full device list response.
    func testDecodeDeviceListResponse() throws {
        let json = """
        {
            "devices": [
                {
                    "ip": "192.0.2.45",
                    "mac": "d0:73:d5:d4:79:9c",
                    "label": "PORCH STRING LIGHTS",
                    "nickname": null,
                    "product": "String Light US",
                    "zones": 102,
                    "is_multizone": true,
                    "current_effect": null,
                    "overridden": false,
                    "is_group": false,
                    "group": "porch"
                }
            ]
        }
        """.data(using: .utf8)!

        let response = try decoder.decode(DeviceListResponse.self, from: json)
        XCTAssertEqual(response.devices.count, 1)
        XCTAssertEqual(response.devices[0].label, "PORCH STRING LIGHTS")
    }

    // MARK: - DeviceStatus decoding

    /// Decode a device status with a running effect.
    func testDecodeDeviceStatus_running() throws {
        let json = """
        {
            "running": true,
            "effect": "flag",
            "params": {
                "brightness": 70,
                "country": "us",
                "speed": 1.5
            },
            "fps": 20,
            "overridden": false,
            "devices": [{"id": "192.0.2.45"}]
        }
        """.data(using: .utf8)!

        let status = try decoder.decode(DeviceStatus.self, from: json)
        XCTAssertTrue(status.running)
        XCTAssertEqual(status.effect, "flag")
        XCTAssertEqual(status.fps, 20)
        XCTAssertFalse(status.overridden)
        // Params should contain mixed types.
        XCTAssertEqual(status.params["brightness"]?.description, "70")
        XCTAssertEqual(status.params["country"]?.description, "us")
        XCTAssertEqual(status.params["speed"]?.description, "1.5")
    }

    /// Decode a device status with no effect running.
    func testDecodeDeviceStatus_idle() throws {
        let json = """
        {
            "running": false,
            "effect": null,
            "params": {},
            "fps": 0,
            "overridden": false
        }
        """.data(using: .utf8)!

        let status = try decoder.decode(DeviceStatus.self, from: json)
        XCTAssertFalse(status.running)
        XCTAssertNil(status.effect)
        XCTAssertTrue(status.params.isEmpty)
    }

    // MARK: - AnyCodableValue decoding

    /// Integer values decode as .int.
    func testAnyCodableValue_int() throws {
        let json = "42".data(using: .utf8)!
        let value = try decoder.decode(AnyCodableValue.self, from: json)
        if case .int(let v) = value {
            XCTAssertEqual(v, 42)
        } else {
            XCTFail("Expected .int, got \(value)")
        }
    }

    /// Float values decode as .double.
    func testAnyCodableValue_double() throws {
        let json = "3.14".data(using: .utf8)!
        let value = try decoder.decode(AnyCodableValue.self, from: json)
        if case .double(let v) = value {
            XCTAssertEqual(v, 3.14, accuracy: 0.001)
        } else {
            XCTFail("Expected .double, got \(value)")
        }
    }

    /// String values decode as .string.
    func testAnyCodableValue_string() throws {
        let json = "\"hello\"".data(using: .utf8)!
        let value = try decoder.decode(AnyCodableValue.self, from: json)
        if case .string(let v) = value {
            XCTAssertEqual(v, "hello")
        } else {
            XCTFail("Expected .string, got \(value)")
        }
    }

    /// Null values decode as .null.
    func testAnyCodableValue_null() throws {
        let json = "null".data(using: .utf8)!
        let value = try decoder.decode(AnyCodableValue.self, from: json)
        if case .null = value {
            // Pass.
        } else {
            XCTFail("Expected .null, got \(value)")
        }
    }

    /// Round-trip: encode then decode should produce the same value.
    func testAnyCodableValue_roundTrip() throws {
        let encoder = JSONEncoder()
        let values: [AnyCodableValue] = [.int(99), .double(2.5), .string("test"), .null]
        for original in values {
            let data = try encoder.encode(original)
            let decoded = try decoder.decode(AnyCodableValue.self, from: data)
            XCTAssertEqual(original.description, decoded.description,
                           "Round-trip failed for \(original)")
        }
    }

    /// The .doubleValue accessor works for ints and doubles.
    func testAnyCodableValue_doubleValueAccessor() {
        XCTAssertEqual(AnyCodableValue.int(10).doubleValue, 10.0)
        XCTAssertEqual(AnyCodableValue.double(2.5).doubleValue, 2.5)
        XCTAssertNil(AnyCodableValue.string("x").doubleValue)
        XCTAssertNil(AnyCodableValue.null.doubleValue)
    }

    /// The .stringValue accessor works for strings only.
    func testAnyCodableValue_stringValueAccessor() {
        XCTAssertEqual(AnyCodableValue.string("hi").stringValue, "hi")
        XCTAssertNil(AnyCodableValue.int(1).stringValue)
        XCTAssertNil(AnyCodableValue.double(1.0).stringValue)
        XCTAssertNil(AnyCodableValue.null.stringValue)
    }

    // MARK: - Effect decoding

    /// Decode the effect list response and convert to sorted array.
    func testDecodeEffectListResponse() throws {
        let json = """
        {
            "effects": {
                "cylon": {
                    "description": "Larson scanner",
                    "params": {
                        "speed": {
                            "default": 2.0,
                            "min": 0.1,
                            "max": 10.0,
                            "description": "Scan speed",
                            "type": "float"
                        }
                    },
                    "hidden": false,
                    "affinity": ["bulb", "strip"]
                },
                "aurora": {
                    "description": "Northern lights",
                    "params": {},
                    "hidden": false,
                    "affinity": ["strip"]
                },
                "_test": {
                    "description": "Diagnostic effect",
                    "params": {},
                    "hidden": true,
                    "affinity": ["bulb", "matrix", "strip"]
                }
            }
        }
        """.data(using: .utf8)!

        let response = try decoder.decode(EffectListResponse.self, from: json)
        let effects = response.toEffectArray()

        // Should be sorted alphabetically.
        XCTAssertEqual(effects.map(\.name), ["_test", "aurora", "cylon"])

        // Hidden flag propagated.
        XCTAssertTrue(effects[0].hidden)
        XCTAssertFalse(effects[1].hidden)

        // Affinity propagated.
        XCTAssertEqual(effects[0].affinity, ["bulb", "matrix", "strip"])
        XCTAssertEqual(effects[1].affinity, ["strip"])
        XCTAssertEqual(effects[2].affinity, ["bulb", "strip"])
        XCTAssertTrue(effects[2].supportsDeviceType("strip"))
        XCTAssertTrue(effects[2].supportsDeviceType("bulb"))
        XCTAssertFalse(effects[2].supportsDeviceType("matrix"))

        // Parameter metadata intact.
        let speedParam = effects[2].params["speed"]
        XCTAssertNotNil(speedParam)
        XCTAssertEqual(speedParam?.type, "float")
        XCTAssertEqual(speedParam?.min?.doubleValue, 0.1)
        XCTAssertEqual(speedParam?.max?.doubleValue, 10.0)
        XCTAssertEqual(speedParam?.`default`.doubleValue, 2.0)
    }

    /// Effect with choices parameter.
    func testDecodeEffect_withChoices() throws {
        let json = """
        {
            "effects": {
                "flag": {
                    "description": "Country flag",
                    "params": {
                        "country": {
                            "default": "us",
                            "min": null,
                            "max": null,
                            "description": "Country code",
                            "type": "str",
                            "choices": ["us", "gb", "fr", "de"]
                        }
                    },
                    "hidden": false,
                    "affinity": ["strip"]
                }
            }
        }
        """.data(using: .utf8)!

        let response = try decoder.decode(EffectListResponse.self, from: json)
        let effects = response.toEffectArray()
        let countryParam = effects[0].params["country"]
        XCTAssertEqual(countryParam?.choices, ["us", "gb", "fr", "de"])
        XCTAssertEqual(countryParam?.`default`.stringValue, "us")
    }

    // MARK: - Schedule entry decoding

    /// Decode a schedule entry with resolved times.
    func testDecodeScheduleEntry() throws {
        let json = """
        {
            "index": 0,
            "name": "porch evening aurora",
            "group": "porch",
            "effect": "aurora",
            "start": "sunset-30m",
            "stop": "23:00",
            "start_resolved": "18:33",
            "stop_resolved": "23:00",
            "days": "",
            "days_display": "Daily",
            "enabled": true,
            "active": false
        }
        """.data(using: .utf8)!

        let entry = try decoder.decode(ScheduleEntry.self, from: json)
        XCTAssertEqual(entry.index, 0)
        XCTAssertEqual(entry.name, "porch evening aurora")
        XCTAssertEqual(entry.group, "porch")
        XCTAssertEqual(entry.effect, "aurora")
        XCTAssertEqual(entry.start, "sunset-30m")
        XCTAssertEqual(entry.stop, "23:00")
        XCTAssertEqual(entry.startResolved, "18:33")
        XCTAssertEqual(entry.stopResolved, "23:00")
        XCTAssertEqual(entry.days, "")
        XCTAssertEqual(entry.daysDisplay, "Daily")
        XCTAssertTrue(entry.enabled)
        XCTAssertFalse(entry.active)
        // Identifiable id.
        XCTAssertEqual(entry.id, 0)
    }

    /// Decode a full schedule response.
    func testDecodeScheduleResponse() throws {
        let json = """
        {
            "entries": [
                {
                    "index": 0,
                    "name": "morning",
                    "group": "porch",
                    "effect": "flag",
                    "start": "sunrise",
                    "stop": "noon",
                    "start_resolved": "06:30",
                    "stop_resolved": "12:00",
                    "days": "MTWRF",
                    "days_display": "Weekdays",
                    "enabled": true,
                    "active": true
                },
                {
                    "index": 1,
                    "name": "evening",
                    "group": "porch",
                    "effect": "aurora",
                    "start": "sunset",
                    "stop": "23:00",
                    "start_resolved": "18:45",
                    "stop_resolved": "23:00",
                    "days": "",
                    "days_display": "Daily",
                    "enabled": false,
                    "active": false
                }
            ]
        }
        """.data(using: .utf8)!

        let response = try decoder.decode(ScheduleResponse.self, from: json)
        XCTAssertEqual(response.entries.count, 2)
        XCTAssertTrue(response.entries[0].active)
        XCTAssertFalse(response.entries[1].enabled)
    }

    // MARK: - ZoneColor decoding

    /// Decode a zone color response (SSE or GET).
    func testDecodeZoneColorResponse() throws {
        let json = """
        {
            "zones": [
                {"h": 120, "s": 65535, "b": 32768, "k": 3500},
                {"h": 240, "s": 0, "b": 65535, "k": 9000}
            ]
        }
        """.data(using: .utf8)!

        let response = try decoder.decode(ZoneColorResponse.self, from: json)
        XCTAssertEqual(response.zones.count, 2)
        XCTAssertEqual(response.zones[0].h, 120)
        XCTAssertEqual(response.zones[0].s, 65535)
        XCTAssertEqual(response.zones[0].b, 32768)
        XCTAssertEqual(response.zones[0].k, 3500)
    }
}
