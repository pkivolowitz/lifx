// DeviceListView.swift
// GlowUp — LIFX Remote Control
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import SwiftUI

/// Main screen: lists all discovered LIFX devices.
///
/// Each row shows the device name, product type, group, current
/// effect, and zone count.  Tapping a device navigates to its
/// detail view.  Pull-to-refresh re-fetches the device list.
struct DeviceListView: View {
    @EnvironmentObject var apiClient: APIClient

    /// Discovered devices from the server.
    @State private var devices: [Device] = []

    /// Error message to display, if any.
    @State private var errorMessage: String?

    /// Whether a request is in progress.
    @State private var isLoading: Bool = false

    /// Controls presentation of the settings sheet.
    @State private var showSettings: Bool = false

    var body: some View {
        NavigationStack {
            List(devices) { device in
                NavigationLink(value: device) {
                    DeviceRow(device: device)
                }
            }
            .navigationTitle("Devices")
            .navigationDestination(for: Device.self) { device in
                DeviceDetailView(device: device)
            }
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button {
                        showSettings = true
                    } label: {
                        Image(systemName: "gear")
                    }
                }
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
            .sheet(isPresented: $showSettings) {
                SettingsView()
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
}

/// A single row in the device list.
struct DeviceRow: View {
    /// The device to display.
    let device: Device

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            // Device name and current effect.
            HStack {
                Text(device.label ?? device.ip)
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
                Spacer()
                if let zones = device.zones, zones > 1 {
                    Text("\(zones) zones")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.vertical, 2)
    }
}
