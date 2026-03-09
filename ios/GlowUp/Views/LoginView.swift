// LoginView.swift
// GlowUp — LIFX Remote Control
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import SwiftUI

/// Login screen presented on every app launch.
///
/// Pre-fills the server URL and token from the Keychain so
/// returning users can connect with a single tap.  Fields use
/// ``textContentType`` hints for Apple Passwords autofill
/// compatibility.
struct LoginView: View {
    @EnvironmentObject var apiClient: APIClient

    /// Editable server URL, pre-filled from Keychain.
    @State private var serverURL: String = ""

    /// Editable bearer token, pre-filled from Keychain.
    @State private var token: String = ""

    /// Result of the connection test.
    @State private var testResult: TestResult?

    /// Whether a connection test is in progress.
    @State private var isConnecting: Bool = false

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                Spacer()

                // App branding.
                VStack(spacing: 12) {
                    Image("AppIconImage")
                        .resizable()
                        .aspectRatio(contentMode: .fit)
                        .frame(width: 100, height: 100)
                        .clipShape(RoundedRectangle(cornerRadius: 22))
                        .shadow(color: .black.opacity(0.25), radius: 8, y: 4)

                    Text("GlowUp")
                        .font(.largeTitle)
                        .fontWeight(.bold)
                }
                .padding(.bottom, 32)

                // Credential fields.
                VStack(spacing: 16) {
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
                }
                .padding(.horizontal, 32)

                // Connection result.
                if let result = testResult {
                    HStack {
                        Image(systemName: result.success
                              ? "checkmark.circle.fill"
                              : "xmark.circle.fill")
                            .foregroundStyle(result.success ? .green : .red)
                        Text(result.message)
                            .font(.subheadline)
                    }
                    .padding(.top, 16)
                }

                // Connect button.
                Button {
                    Task { await connect() }
                } label: {
                    HStack {
                        if isConnecting {
                            ProgressView()
                                .tint(.white)
                                .padding(.trailing, 4)
                        }
                        Text("Connect")
                            .fontWeight(.semibold)
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .disabled(
                    isConnecting || serverURL.isEmpty || token.isEmpty
                )
                .padding(.horizontal, 32)
                .padding(.top, 24)

                Spacer()

                // Footer.
                Text("Credentials are stored in the iOS Keychain.")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
                    .padding(.bottom, 16)
            }
            .onAppear {
                // Pre-fill from Keychain via the API client.
                serverURL = apiClient.serverURL
                token = apiClient.token
            }
        }
    }

    /// Test the connection and, on success, mark as authenticated.
    private func connect() async {
        isConnecting = true
        testResult = nil

        // Apply credentials for the test.
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
            // Credentials are saved to Keychain by the APIClient
            // didSet observers.  Mark the session as authenticated.
            apiClient.isAuthenticated = true
        } catch {
            testResult = TestResult(
                success: false,
                message: error.localizedDescription
            )
            // Restore original settings on failure.
            apiClient.serverURL = originalURL
            apiClient.token = originalToken
        }

        isConnecting = false
    }
}

/// Result of a connection test.
private struct TestResult {
    /// Whether the test succeeded.
    let success: Bool

    /// Human-readable result message.
    let message: String
}
