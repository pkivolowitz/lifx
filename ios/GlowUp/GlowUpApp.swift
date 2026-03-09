// GlowUpApp.swift
// GlowUp — LIFX Remote Control
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import SwiftUI

/// Main entry point for the GlowUp iOS application.
///
/// Provides a tab-based interface for controlling LIFX devices
/// remotely through the GlowUp REST API server.
@main
struct GlowUpApp: App {
    /// Shared API client configured from persisted settings.
    @StateObject private var apiClient = APIClient()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(apiClient)
        }
    }
}

/// Root content view — shows Settings if not configured, otherwise
/// the main device list.
struct ContentView: View {
    @EnvironmentObject var apiClient: APIClient

    /// Controls presentation of the settings sheet.
    @State private var showSettings: Bool = false

    var body: some View {
        Group {
            if apiClient.isConfigured {
                DeviceListView()
            } else {
                // First launch: show settings immediately.
                VStack(spacing: 20) {
                    Image(systemName: "lightbulb.led.wide")
                        .font(.system(size: 60))
                        .foregroundStyle(.orange)
                    Text("Welcome to GlowUp")
                        .font(.title)
                        .fontWeight(.bold)
                    Text("Configure your server connection to get started.")
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal)
                    Button("Configure Server") {
                        showSettings = true
                    }
                    .buttonStyle(.borderedProminent)
                }
            }
        }
        .sheet(isPresented: $showSettings) {
            SettingsView()
        }
    }
}
