// KeychainHelper.swift
// GlowUp — LIFX Remote Control
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import Foundation
import Security

/// Simple wrapper around the iOS Keychain for storing and retrieving
/// the API bearer token securely.
///
/// Uses ``kSecClassGenericPassword`` with a fixed service identifier
/// so the token persists across app launches and is protected by the
/// device's Secure Enclave.
enum KeychainHelper {
    /// Service identifier for Keychain items.
    private static let service = "com.glowup.api"

    /// Account identifier for the bearer token.
    private static let tokenAccount = "bearer_token"

    /// Account identifier for the server URL.
    private static let urlAccount = "server_url"

    /// Save a string value to the Keychain.
    ///
    /// If an entry already exists for the given account, it is updated.
    ///
    /// - Parameters:
    ///   - value: The string to store.
    ///   - account: The Keychain account identifier.
    static func save(_ value: String, forAccount account: String) {
        let data = Data(value.utf8)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]

        // Delete any existing entry first.
        SecItemDelete(query as CFDictionary)

        // Add the new entry.
        var addQuery = query
        addQuery[kSecValueData as String] = data
        SecItemAdd(addQuery as CFDictionary, nil)
    }

    /// Load a string value from the Keychain.
    ///
    /// - Parameter account: The Keychain account identifier.
    /// - Returns: The stored string, or ``nil`` if not found.
    static func load(forAccount account: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]

        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess,
              let data = result as? Data,
              let string = String(data: data, encoding: .utf8)
        else {
            return nil
        }
        return string
    }

    /// Delete a value from the Keychain.
    ///
    /// - Parameter account: The Keychain account identifier.
    static func delete(forAccount account: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(query as CFDictionary)
    }

    // -- Convenience methods for the two values we store --

    /// Save the API bearer token.
    static func saveToken(_ token: String) {
        save(token, forAccount: tokenAccount)
    }

    /// Load the API bearer token.
    static func loadToken() -> String? {
        load(forAccount: tokenAccount)
    }

    /// Save the server URL.
    static func saveServerURL(_ url: String) {
        save(url, forAccount: urlAccount)
    }

    /// Load the server URL.
    static func loadServerURL() -> String? {
        load(forAccount: urlAccount)
    }
}
