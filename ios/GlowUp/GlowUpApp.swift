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
 
/// Root content view — shows the login screen until the user
/// authenticates, then the main hub.
struct ContentView: View {
    @EnvironmentObject var apiClient: APIClient

    var body: some View {
        Group {
            if apiClient.isAuthenticated {
                HubView(apiClient: apiClient)
            } else {
                LoginView()
            }
        }
    }
}
