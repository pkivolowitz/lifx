// EffectPickerView.swift
// GlowUp — LIFX Remote Control
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import SwiftUI

/// Screen for selecting an effect to play on a device.
///
/// Lists all available effects with their descriptions.  Tapping
/// an effect navigates to ``EffectConfigView`` where parameters
/// can be adjusted before playing.
struct EffectPickerView: View {
    @EnvironmentObject var apiClient: APIClient

    /// The target device for the selected effect.
    let device: Device

    /// Available effects fetched from the server.
    @State private var effects: [Effect] = []

    /// Name of the currently running effect (if any).
    @State private var currentEffect: String?

    /// Error message for display.
    @State private var errorMessage: String?

    /// Whether effects are being loaded.
    @State private var isLoading: Bool = true

    var body: some View {
        List(effects) { effect in
            NavigationLink {
                EffectConfigView(device: device, effect: effect)
            } label: {
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(effect.name)
                            .font(.headline)
                        if !effect.description.isEmpty {
                            Text(effect.description)
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                        }
                        // Show parameter count as a hint.
                        let paramCount = effect.params.count
                        if paramCount > 0 {
                            Text(
                                "\(paramCount) parameter\(paramCount == 1 ? "" : "s")"
                            )
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                        }
                    }
                    Spacer()
                    // Indicate the currently running effect.
                    if effect.name == currentEffect {
                        Image(systemName: "speaker.wave.2.fill")
                            .foregroundStyle(.green)
                            .font(.subheadline)
                    }
                }
                .padding(.vertical, 2)
            }
        }
        .navigationTitle("Effects")
        .overlay {
            if isLoading {
                ProgressView("Loading effects...")
            } else if effects.isEmpty {
                ContentUnavailableView(
                    "No Effects",
                    systemImage: "sparkles",
                    description: Text(
                        errorMessage ?? "Could not load effects."
                    )
                )
            }
        }
        .task {
            await loadEffects()
        }
    }

    /// Fetch the effect list and current device status from the server.
    private func loadEffects() async {
        isLoading = true
        do {
            effects = try await apiClient.fetchEffects()
            // Fetch device status to highlight the running effect.
            let status = try await apiClient.fetchStatus(ip: device.ip)
            if status.running {
                currentEffect = status.effect
            }
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }
}
