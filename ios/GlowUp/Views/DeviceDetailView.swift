// DeviceDetailView.swift
// GlowUp — LIFX Remote Control
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import SwiftUI

/// Detail view for a single LIFX device.
///
/// Shows live zone colors via SSE streaming, current effect status,
/// power toggle, and controls for changing or stopping effects.
struct DeviceDetailView: View {
    @EnvironmentObject var apiClient: APIClient

    /// The device being viewed.
    let device: Device

    /// Live color stream from SSE.
    @StateObject private var colorStream = SSEColorStream()

    /// Current effect engine status.
    @State private var status: DeviceStatus?

    /// Error message for display.
    @State private var errorMessage: String?

    /// Whether a request is in progress.
    @State private var isLoading: Bool = false

    var body: some View {
        List {
            // Live color strip section.
            Section {
                VStack(alignment: .leading, spacing: 8) {
                    ColorStripView(zones: colorStream.zones)
                    HStack {
                        Circle()
                            .fill(colorStream.isConnected ? .green : .gray)
                            .frame(width: 8, height: 8)
                        Text(
                            colorStream.isConnected
                            ? "Live · 4 Hz"
                            : "Connecting..."
                        )
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    }
                }
            } header: {
                Text("Live Colors")
            }

            // Device info section.
            Section {
                LabeledContent("Product", value: device.product ?? "Unknown")
                LabeledContent("IP", value: device.ip)
                if let label = device.label, !label.isEmpty {
                    LabeledContent("Label", value: label)
                }
                if let nickname = device.nickname, !nickname.isEmpty {
                    LabeledContent("Nickname", value: nickname)
                }
                if let group = device.group, !group.isEmpty {
                    LabeledContent("Group", value: group)
                }
                if let zones = device.zones {
                    LabeledContent("Zones", value: "\(zones)")
                }
            } header: {
                Text("Device Info")
            }

            // Current effect section.
            Section {
                if let status = status {
                    if let effect = status.effect {
                        LabeledContent("Effect", value: effect)
                        LabeledContent(
                            "Status",
                            value: status.running ? "Running" : "Stopped"
                        )
                        // Show current parameter values.
                        if !status.params.isEmpty {
                            ForEach(
                                Array(status.params.keys.sorted()),
                                id: \.self
                            ) { key in
                                LabeledContent(
                                    key,
                                    value: status.params[key]?.description ?? "—"
                                )
                            }
                        }
                    } else {
                        Text("No effect running")
                            .foregroundStyle(.secondary)
                    }
                } else {
                    ProgressView("Loading status...")
                }
            } header: {
                Text("Effect")
            }

            // Controls section.
            Section {
                // Change effect button.
                NavigationLink {
                    EffectPickerView(device: device)
                } label: {
                    Label("Change Effect", systemImage: "wand.and.stars")
                }

                // Restart button — replay the current/last effect.
                Button {
                    Task { await restartEffect() }
                } label: {
                    Label("Restart Effect", systemImage: "play.circle")
                }
                .disabled(status?.effect == nil || (status?.running ?? false))

                // Stop button.
                Button(role: .destructive) {
                    Task { await stopEffect() }
                } label: {
                    Label("Stop Effect", systemImage: "stop.circle")
                }
                .disabled(status?.effect == nil || !(status?.running ?? false))

                // Identify — pulse brightness to locate the device.
                Button {
                    Task { await identifyDevice() }
                } label: {
                    Label("Identify", systemImage: "lightbulb.max")
                }

                // Power toggle.
                Button {
                    Task { await togglePower() }
                } label: {
                    Label("Power Off", systemImage: "power")
                }
            } header: {
                Text("Controls")
            }
        }
        .navigationTitle(device.displayName)
        .navigationBarTitleDisplayMode(.inline)
        .task {
            // Start SSE stream and fetch status on appearance.
            colorStream.connect(apiClient: apiClient, ip: device.ip)
            await refreshStatus()
        }
        .onDisappear {
            // Clean up the SSE connection when leaving.
            colorStream.disconnect()
        }
        .refreshable {
            await refreshStatus()
        }
        .alert(
            "Error",
            isPresented: Binding(
                get: { errorMessage != nil },
                set: { if !$0 { errorMessage = nil } }
            )
        ) {
            Button("OK") { errorMessage = nil }
        } message: {
            Text(errorMessage ?? "")
        }
    }

    /// Fetch current device status from the server.
    private func refreshStatus() async {
        do {
            status = try await apiClient.fetchStatus(ip: device.ip)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    /// Stop the current effect.
    private func stopEffect() async {
        isLoading = true
        do {
            status = try await apiClient.stop(ip: device.ip)
            // Clear the color strip — no effect means no live colors.
            colorStream.zones = []
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }

    /// Replay the last effect that was running.
    private func restartEffect() async {
        guard let effectName = status?.effect else { return }
        isLoading = true
        do {
            // Rebuild params dict from status.
            var params: [String: Any] = [:]
            for (key, value) in status?.params ?? [:] {
                switch value {
                case .int(let v): params[key] = v
                case .double(let v): params[key] = v
                case .string(let v): params[key] = v
                case .null: break
                }
            }
            status = try await apiClient.play(
                ip: device.ip,
                effectName: effectName,
                params: params
            )
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }

    /// Pulse the device's brightness to visually locate it.
    private func identifyDevice() async {
        do {
            try await apiClient.identify(ip: device.ip)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    /// Toggle device power off.
    private func togglePower() async {
        do {
            try await apiClient.setPower(ip: device.ip, on: false)
            // Refresh status after power change.
            await refreshStatus()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}
