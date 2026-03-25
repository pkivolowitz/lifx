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

    /// Power state derived from the device's server-reported field.
    @State private var isPoweredOn: Bool = true

    /// Error message for display.
    @State private var errorMessage: String?

    /// Whether a request is in progress.
    @State private var isLoading: Bool = false

    var body: some View {
        List {
            // Live color strip section — only shown when an effect is running.
            if status?.running == true {
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
            }

            // Schedule override banner — shown when manual control has
            // paused the schedule on this device.
            if status?.overridden == true {
                Section {
                    VStack(alignment: .leading, spacing: 8) {
                        Label(
                            "Schedule paused on this device",
                            systemImage: "calendar.badge.exclamationmark"
                        )
                        .font(.subheadline)
                        .foregroundStyle(.orange)

                        Text(
                            "Playing or stopping an effect pauses the schedule. "
                            + "Tap Resume to hand control back to the scheduler."
                        )
                        .font(.caption)
                        .foregroundStyle(.secondary)

                        Button {
                            Task { await resumeSchedule() }
                        } label: {
                            Label("Resume Schedule", systemImage: "calendar.badge.clock")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.borderedProminent)
                        .tint(.orange)
                    }
                    .padding(.vertical, 4)
                }
            }

            // Device info section.
            Section {
                LabeledContent("Product", value: device.product ?? "Unknown")
                if device.isVirtualGroup {
                    LabeledContent("Type", value: "Virtual Group")
                    if let members = device.memberIps {
                        ForEach(
                            Array(members.enumerated()),
                            id: \.offset
                        ) { index, memberIp in
                            LabeledContent(
                                "Device \(index + 1)",
                                value: memberIp
                            )
                        }
                    }
                } else {
                    LabeledContent("IP", value: device.ip)
                }
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
                .disabled(status?.effect == nil)

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
                    Label(
                        isPoweredOn ? "Power Off" : "Power On",
                        systemImage: isPoweredOn
                            ? "lightbulb.fill" : "lightbulb.slash"
                    )
                }

                // Deep reset — clears stale zone colors from device firmware.
                Button(role: .destructive) {
                    Task { await resetDevice() }
                } label: {
                    Label("Reset Lights", systemImage: "arrow.counterclockwise.circle")
                }
            } header: {
                Text("Controls")
            }
        }
        .navigationTitle(device.displayName)
        .navigationBarTitleDisplayMode(.inline)
        .task {
            // Initialise power state from the device snapshot.
            isPoweredOn = device.power ?? true
            // Fetch status first, then start SSE only if an effect is running.
            await refreshStatus()
            if status?.running == true {
                colorStream.connect(apiClient: apiClient, deviceId: device.deviceId)
            }
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
            status = try await apiClient.fetchStatus(deviceId: device.deviceId)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    /// Stop the current effect.
    private func stopEffect() async {
        isLoading = true
        do {
            status = try await apiClient.stop(deviceId: device.deviceId)
            colorStream.disconnect()
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
                deviceId: device.deviceId,
                effectName: effectName,
                params: params
            )
            if status?.running == true {
                colorStream.connect(apiClient: apiClient, deviceId: device.deviceId)
            }
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }

    /// Pulse the device's brightness to visually locate it.
    private func identifyDevice() async {
        do {
            try await apiClient.identify(deviceId: device.deviceId)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    /// Clear the phone override so the scheduler resumes control.
    private func resumeSchedule() async {
        isLoading = true
        do {
            status = try await apiClient.resume(deviceId: device.deviceId)
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }

    /// Toggle device power on or off.
    private func togglePower() async {
        let newState = !isPoweredOn
        do {
            try await apiClient.setPower(deviceId: device.deviceId, on: newState)
            isPoweredOn = newState
            await refreshStatus()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    /// Deep-reset device: stop effects, clear firmware state, blank zones.
    private func resetDevice() async {
        isLoading = true
        do {
            try await apiClient.reset(deviceId: device.deviceId)
            colorStream.disconnect()
            await refreshStatus()
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }
}
