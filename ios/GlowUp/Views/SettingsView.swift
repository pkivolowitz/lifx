// SettingsView.swift
// GlowUp — LIFX Remote Control
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import SwiftUI

/// Settings screen for configuring the server connection.
///
/// The user enters the server URL (e.g., ``https://lights.example.com``)
/// and their API bearer token.  Both values are persisted in the iOS
/// Keychain via ``KeychainHelper``.
struct SettingsView: View {
    @EnvironmentObject var apiClient: APIClient
    @Environment(\.dismiss) private var dismiss

    /// Editable server URL (bound to APIClient).
    @State private var serverURL: String = ""

    /// Editable bearer token (bound to APIClient).
    @State private var token: String = ""

    /// Result of the connection test.
    @State private var testResult: TestResult?

    /// Whether a connection test is in progress.
    @State private var isTesting: Bool = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Server URL")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                        TextField(
                            "https://lights.example.com",
                            text: $serverURL
                        )
                        .textFieldStyle(.roundedBorder)
                        .textContentType(.URL)
                        .autocapitalization(.none)
                        .disableAutocorrection(true)
                        .keyboardType(.URL)
                    }

                    VStack(alignment: .leading, spacing: 4) {
                        Text("API Token")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                        SecureField("Bearer token", text: $token)
                            .textFieldStyle(.roundedBorder)
                            .textContentType(.password)
                            .autocapitalization(.none)
                            .disableAutocorrection(true)
                    }
                } header: {
                    Text("Connection")
                } footer: {
                    Text("The token is stored securely in the iOS Keychain.")
                }

                // Connection test section.
                Section {
                    Button {
                        Task { await testConnection() }
                    } label: {
                        HStack {
                            if isTesting {
                                ProgressView()
                                    .padding(.trailing, 8)
                            }
                            Text("Test Connection")
                        }
                    }
                    .disabled(
                        isTesting || serverURL.isEmpty || token.isEmpty
                    )

                    if let result = testResult {
                        HStack {
                            Image(systemName: result.success
                                  ? "checkmark.circle.fill"
                                  : "xmark.circle.fill")
                            .foregroundStyle(
                                result.success ? .green : .red
                            )
                            Text(result.message)
                                .font(.subheadline)
                        }
                    }
                }

                // About section.
                Section {
                    LabeledContent("App Version", value: "1.0")
                    LabeledContent("Server Port", value: "8420")
                } header: {
                    Text("About")
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") {
                        saveAndDismiss()
                    }
                }
            }
            .onAppear {
                // Load current values from the API client.
                serverURL = apiClient.serverURL
                token = apiClient.token
            }
        }
    }

    /// Test the connection to the server by fetching the device list.
    private func testConnection() async {
        isTesting = true
        testResult = nil

        // Temporarily apply the settings for the test.
        let originalURL = apiClient.serverURL
        let originalToken = apiClient.token
        apiClient.serverURL = serverURL
        apiClient.token = token

        do {
            let devices = try await apiClient.fetchDevices()
            testResult = TestResult(
                success: true,
                message: "Connected — \(devices.count) device(s) found"
            )
        } catch {
            testResult = TestResult(
                success: false,
                message: error.localizedDescription
            )
            // Restore original settings on failure.
            apiClient.serverURL = originalURL
            apiClient.token = originalToken
        }

        isTesting = false
    }

    /// Save settings to the API client and dismiss.
    private func saveAndDismiss() {
        apiClient.serverURL = serverURL
        apiClient.token = token
        dismiss()
    }
}

/// Result of a connection test.
private struct TestResult {
    /// Whether the test succeeded.
    let success: Bool

    /// Human-readable result message.
    let message: String
}
