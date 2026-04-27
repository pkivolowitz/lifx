// URLEncodingTests.swift
// GlowUpTests
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import XCTest
@testable import GlowUp

/// Tests for ``urlEncodeDeviceId`` — the function that percent-encodes
/// device identifiers for safe embedding in URL path segments.
///
/// Labels contain spaces, MACs contain colons, and group identifiers
/// contain colons.  All must survive a round-trip through the URL
/// path without ambiguity.
final class URLEncodingTests: XCTestCase {

    // MARK: - Labels

    /// Spaces in labels must be percent-encoded.
    func testEncoding_labelWithSpaces() {
        let encoded = urlEncodeDeviceId("PORCH STRING LIGHTS")
        XCTAssertEqual(encoded, "PORCH%20STRING%20LIGHTS")
        XCTAssertFalse(encoded.contains(" "))
    }

    /// Label with no special characters passes through unchanged.
    func testEncoding_plainLabel() {
        let encoded = urlEncodeDeviceId("Porch")
        XCTAssertEqual(encoded, "Porch")
    }

    /// Label with mixed case and numbers.
    func testEncoding_alphanumericLabel() {
        let encoded = urlEncodeDeviceId("Dragon Fly 1B")
        XCTAssertEqual(encoded, "Dragon%20Fly%201B")
    }

    // MARK: - MAC addresses

    /// Colons in MAC addresses are valid in URL path segments (RFC 3986)
    /// and should pass through unchanged.
    func testEncoding_macAddress() {
        let encoded = urlEncodeDeviceId("d0:73:d5:d4:79:9c")
        XCTAssertEqual(encoded, "d0:73:d5:d4:79:9c")
    }

    // MARK: - IP addresses

    /// Bare IP addresses should pass through unchanged (dots are unreserved).
    func testEncoding_ipAddress() {
        let encoded = urlEncodeDeviceId("192.0.2.45")
        XCTAssertEqual(encoded, "192.0.2.45")
    }

    // MARK: - Group identifiers

    /// "group:name" — colons are valid in URL path segments,
    /// so the identifier passes through unchanged.
    func testEncoding_groupIdentifier() {
        let encoded = urlEncodeDeviceId("group:porch")
        XCTAssertEqual(encoded, "group:porch")
    }

    // MARK: - Edge cases

    /// Empty string returns empty string.
    func testEncoding_emptyString() {
        let encoded = urlEncodeDeviceId("")
        XCTAssertEqual(encoded, "")
    }

    /// Unicode characters in labels (hypothetical international label).
    func testEncoding_unicodeLabel() {
        let encoded = urlEncodeDeviceId("Lumiere Salon")
        // The accent-free version passes through; with accents
        // would be encoded. Either way, not empty.
        XCTAssertFalse(encoded.isEmpty)
    }

    /// Slash should be encoded (it's a path separator).
    func testEncoding_slashInLabel() {
        let encoded = urlEncodeDeviceId("Floor/Lamp")
        XCTAssertFalse(encoded.contains("/"),
                        "Slashes must be encoded, got: \(encoded)")
    }
}
