// DeviceListView.swift
// GlowUp — LIFX Remote Control
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import SwiftUI

/// Main screen: lists all configured LIFX devices.
///
/// Each row shows the device name, product type, group, current
/// effect, and zone count.  Tapping a device navigates to its
/// detail view.  Pull-to-refresh re-fetches the device list.
///
/// Swipe actions:
/// - Leading swipe: identify (pulse brightness to locate device)
/// - Trailing swipe: rename (set a custom display name)
struct DeviceListView: View {
    @EnvironmentObject var apiClient: APIClient

    /// Discovered devices from the server.
    @State private var devices: [Device] = []

    /// Error message to display, if any.
    @State private var errorMessage: String?

    /// Whether a request is in progress.
    @State private var isLoading: Bool = false

    /// Device being renamed (drives the rename alert).
    @State private var renamingDevice: Device?

    /// Text field content for the rename alert.
    @State private var renameText: String = ""

    var body: some View {
        NavigationStack {
            List(devices) { device in
                NavigationLink(value: device) {
                    DeviceRow(device: device)
                }
                .swipeActions(edge: .leading) {
                    Button {
                        Task { await identifyDevice(device) }
                    } label: {
                        Label("Identify", systemImage: "lightbulb.max")
                    }
                    .tint(.yellow)
                }
                .swipeActions(edge: .trailing) {
                    Button {
                        renameText = device.nickname ?? device.label ?? ""
                        renamingDevice = device
                    } label: {
                        Label("Rename", systemImage: "pencil")
                    }
                    .tint(.blue)
                }
            }
            .navigationTitle("Devices")
            .navigationDestination(for: Device.self) { device in
                DeviceDetailView(device: device)
            }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Task { await refreshDevices() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .disabled(isLoading)
                }
            }
            .refreshable {
                await refreshDevices()
            }
            .overlay {
                if devices.isEmpty && !isLoading {
                    ContentUnavailableView(
                        "No Devices",
                        systemImage: "lightbulb.slash",
                        description: Text(
                            errorMessage ?? "Pull to refresh or check server connection."
                        )
                    )
                }
            }
            .alert(
                "Rename Device",
                isPresented: Binding(
                    get: { renamingDevice != nil },
                    set: { if !$0 { renamingDevice = nil } }
                )
            ) {
                TextField("Display name", text: $renameText)
                Button("Save") {
                    if let device = renamingDevice {
                        Task { await saveNickname(device) }
                    }
                }
                Button("Clear Name", role: .destructive) {
                    if let device = renamingDevice {
                        renameText = ""
                        Task { await saveNickname(device) }
                    }
                }
                Button("Cancel", role: .cancel) {
                    renamingDevice = nil
                }
            } message: {
                Text("Enter a custom name for this device.")
            }
            .task {
                // Fetch devices on first appearance.
                await refreshDevices()
            }
        }
    }

    /// Fetch the device list from the server.
    private func refreshDevices() async {
        isLoading = true
        errorMessage = nil
        do {
            devices = try await apiClient.fetchDevices()
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }

    /// Pulse a device's brightness to visually locate it.
    private func identifyDevice(_ device: Device) async {
        do {
            try await apiClient.identify(deviceId: device.deviceId)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    /// Save the current rename text as the device's nickname.
    private func saveNickname(_ device: Device) async {
        let name = renameText.trimmingCharacters(in: .whitespacesAndNewlines)
        renamingDevice = nil
        do {
            try await apiClient.setNickname(deviceId: device.deviceId, nickname: name)
            // Refresh to show the updated name.
            await refreshDevices()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

/// A single row in the device list.
struct DeviceRow: View {
    /// The device to display.
    let device: Device

    /// API client for power toggle.
    @EnvironmentObject var apiClient: APIClient

    /// Whether power is on (initialized from server, updated locally
    /// for immediate UI feedback on toggle).
    @State private var isPoweredOn: Bool = true

    /// Initialize power state from the server's response.
    init(device: Device) {
        self.device = device
        _isPoweredOn = State(initialValue: device.power ?? true)
    }

    /// Whether a power request is in flight.
    @State private var powerLoading: Bool = false

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                // Device name and current effect.
                HStack {
                    Text(device.displayName)
                        .font(.headline)
                    Spacer()
                    if let effect = device.currentEffect {
                        Text(effect)
                            .font(.caption)
                            .padding(.horizontal, 8)
                            .padding(.vertical, 2)
                            .background(Color.green.opacity(0.2))
                            .cornerRadius(6)
                    }
                }

                // Product type, group, and zone count.
                HStack {
                    if device.isVirtualGroup {
                        // Virtual group: show member count.
                        let count = device.memberIps?.count ?? 0
                        Label(
                            "\(count) devices",
                            systemImage: "rectangle.3.group"
                        )
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                    } else {
                        if let product = device.product {
                            Text(product)
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                        }
                        if let group = device.group, !group.isEmpty {
                            Text("·")
                                .foregroundStyle(.secondary)
                            Text(group)
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                        }
                    }
                    Spacer()
                    if let zones = device.zones, zones > 1 {
                        Text("\(zones) zones")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }

            // Power toggle button.
            Button {
                Task { await togglePower() }
            } label: {
                Image(systemName: isPoweredOn
                      ? "lightbulb.fill"
                      : "lightbulb.slash")
                    .font(.title2)
                    .foregroundStyle(isPoweredOn ? .yellow : .secondary)
            }
            .buttonStyle(.plain)
            .disabled(powerLoading)
        }
        .padding(.vertical, 2)
    }

    /// Toggle power on/off for this device or group.
    private func togglePower() async {
        let newState = !isPoweredOn
        powerLoading = true
        do {
            try await apiClient.setPower(
                deviceId: device.deviceId, on: newState
            )
            isPoweredOn = newState
        } catch {
            // Revert on failure — state didn't change.
        }
        powerLoading = false
    }
}
