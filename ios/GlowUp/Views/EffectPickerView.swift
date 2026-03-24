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

    /// Whether to show hidden (diagnostic) effects whose names start with ``_``.
    @State private var showHidden: Bool = false

    /// Whether to show effects that don't match the device's form factor.
    @State private var showOtherEffects: Bool = false

    /// Effects that match the target device's form factor and hidden toggle.
    private var matchingEffects: [Effect] {
        let base = showHidden ? effects : effects.filter { !$0.hidden }
        return base.filter { $0.supportsDeviceType(device.deviceType) }
    }

    /// Effects that don't match the device but may still be useful.
    private var otherEffects: [Effect] {
        let base = showHidden ? effects : effects.filter { !$0.hidden }
        return base.filter { !$0.supportsDeviceType(device.deviceType) }
    }

    var body: some View {
        List {
            ForEach(matchingEffects) { effect in
                effectRow(effect)
            }

            // Collapsed section for effects that don't match the device type.
            if !otherEffects.isEmpty {
                Section {
                    DisclosureGroup(
                        "Other Effects (\(otherEffects.count))",
                        isExpanded: $showOtherEffects
                    ) {
                        ForEach(otherEffects) { effect in
                            effectRow(effect)
                        }
                    }
                }
            }

            // Toggle for revealing hidden diagnostic/test effects.
            Section {
                Toggle("Show Hidden Effects", isOn: $showHidden)
            }
        }
        .navigationTitle("Effects")
        .overlay {
            if isLoading {
                ProgressView("Loading effects...")
            } else if visibleEffects.isEmpty {
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

    /// A single effect row: name, description, param count, running indicator.
    @ViewBuilder
    private func effectRow(_ effect: Effect) -> some View {
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

    /// Fetch the effect list and current device status from the server.
    private func loadEffects() async {
        isLoading = true
        do {
            effects = try await apiClient.fetchEffects()
            // Fetch device status to highlight the running effect.
            let status = try await apiClient.fetchStatus(deviceId: device.deviceId)
            if status.running {
                currentEffect = status.effect
            }
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }
}
