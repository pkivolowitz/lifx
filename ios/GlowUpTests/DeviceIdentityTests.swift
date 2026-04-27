// DeviceIdentityTests.swift
// GlowUpTests
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import XCTest
@testable import GlowUp

/// Tests for the label > MAC > IP device identity fallback chain.
///
/// The ``Device.deviceId`` property selects the most stable
/// identifier available.  These tests verify every combination
/// of present/absent/empty fields.
final class DeviceIdentityTests: XCTestCase {

    // MARK: - deviceId fallback chain

    /// Label present and non-empty — should be preferred.
    func testDeviceId_prefersLabel() throws {
        let device = makeDevice(label: "PORCH STRING LIGHTS", mac: "d0:73:d5:d4:79:9c", ip: "192.0.2.45")
        XCTAssertEqual(device.deviceId, "PORCH STRING LIGHTS")
    }

    /// Label nil — should fall back to MAC.
    func testDeviceId_fallsBackToMac_whenLabelNil() throws {
        let device = makeDevice(label: nil, mac: "d0:73:d5:d4:79:9c", ip: "192.0.2.45")
        XCTAssertEqual(device.deviceId, "d0:73:d5:d4:79:9c")
    }

    /// Label empty string — should fall back to MAC.
    func testDeviceId_fallsBackToMac_whenLabelEmpty() throws {
        let device = makeDevice(label: "", mac: "d0:73:d5:d4:79:9c", ip: "192.0.2.45")
        XCTAssertEqual(device.deviceId, "d0:73:d5:d4:79:9c")
    }

    /// Label nil, MAC empty — should fall back to IP.
    func testDeviceId_fallsBackToIp_whenLabelNilMacEmpty() throws {
        let device = makeDevice(label: nil, mac: "", ip: "192.0.2.45")
        XCTAssertEqual(device.deviceId, "192.0.2.45")
    }

    /// Virtual group device — MAC is "virtual", should skip to IP
    /// (which for groups is "group:name").
    func testDeviceId_skipsVirtualMac() throws {
        let device = makeDevice(label: nil, mac: "virtual", ip: "group:all")
        XCTAssertEqual(device.deviceId, "group:all")
    }

    /// Group device with a label — group:name always wins for groups
    /// because the server needs the ``group:`` prefix for routing.
    func testDeviceId_groupAlwaysUsesGroupPrefix() throws {
        let device = makeDevice(label: "all", mac: "", ip: "group:all", isGroup: true)
        XCTAssertEqual(device.deviceId, "group:all")
    }

    /// All three present — label still wins.
    func testDeviceId_labelWinsOverAll() throws {
        let device = makeDevice(label: "Dragon Fly 1B", mac: "d0:73:d5:6a:c9:af", ip: "192.0.2.18")
        XCTAssertEqual(device.deviceId, "Dragon Fly 1B")
    }

    // MARK: - Identifiable conformance

    /// Device.id must equal deviceId (used for SwiftUI navigation and list identity).
    func testIdentifiableId_equalsDeviceId() throws {
        let device = makeDevice(label: "Living Room Floor Lamp", mac: "d0:73:d5:6a:88:79", ip: "192.0.2.5")
        XCTAssertEqual(device.id, device.deviceId)
    }

    // MARK: - displayName

    /// Nickname takes priority over everything.
    func testDisplayName_prefersNickname() throws {
        let device = makeDevice(label: "Dragon Fly 1B", mac: "aa:bb:cc:dd:ee:ff", ip: "192.0.2.18", nickname: "My Favorite Light")
        XCTAssertEqual(device.displayName, "My Favorite Light")
    }

    /// No nickname — falls back to label.
    func testDisplayName_fallsBackToLabel() throws {
        let device = makeDevice(label: "PORCH STRING LIGHTS", mac: "aa:bb:cc:dd:ee:ff", ip: "192.0.2.45")
        XCTAssertEqual(device.displayName, "PORCH STRING LIGHTS")
    }

    /// No nickname, no label — falls back to IP.
    func testDisplayName_fallsBackToIp() throws {
        let device = makeDevice(label: nil, mac: "aa:bb:cc:dd:ee:ff", ip: "192.0.2.45")
        XCTAssertEqual(device.displayName, "192.0.2.45")
    }

    /// Group device display name is prefixed.
    func testDisplayName_groupPrefix() throws {
        let device = makeDevice(label: "porch", mac: "", ip: "group:porch", isGroup: true)
        XCTAssertEqual(device.displayName, "Group: porch")
    }

    // MARK: - Helpers

    /// Build a ``Device`` with only the fields under test.
    private func makeDevice(
        label: String?,
        mac: String,
        ip: String,
        isGroup: Bool = false,
        nickname: String? = nil
    ) -> Device {
        return Device(
            ip: ip,
            mac: mac,
            label: label,
            nickname: nickname,
            product: nil,
            group: nil,
            zones: nil,
            isMultizone: nil,
            currentEffect: nil,
            isGroup: isGroup,
            power: nil,
            memberIps: nil
        )
    }
}
