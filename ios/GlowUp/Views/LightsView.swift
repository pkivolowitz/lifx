// LightsView.swift
// GlowUp — LIFX Remote Control
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import SwiftUI

/// Home-like landing page for quick group power and brightness control.
///
/// Shows one card per device group with a large power toggle and a
/// brightness slider.  An "Advanced" button at the bottom navigates
/// to the full HubView for effect selection, scheduling, and device
/// management.
struct LightsView: View {
    @EnvironmentObject var apiClient: APIClient

    /// Groups fetched from the server, keyed by name.
    @State private var groups: [GroupInfo] = []

    /// All devices from the server (used to derive group power state).
    @State private var devices: [Device] = []

    /// Whether the initial load is in progress.
    @State private var isLoading: Bool = true

    /// Error message for display.
    @State private var errorMessage: String?

    /// Navigate to the advanced HubView.
    @State private var showAdvanced: Bool = false

    var body: some View {
        NavigationStack {
            ScrollView {
                if isLoading {
                    ProgressView("Loading...")
                        .padding(.top, 80)
                } else if groups.isEmpty {
                    ContentUnavailableView(
                        "No Groups",
                        systemImage: "lightbulb.slash",
                        description: Text(
                            errorMessage ?? "No device groups configured."
                        )
                    )
                } else {
                    LazyVStack(spacing: 16) {
                        ForEach($groups) { $group in
                            GroupCard(group: $group, apiClient: apiClient)
                        }
                    }
                    .padding()
                }
            }
            .navigationTitle("Lights")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Task { await loadData() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .disabled(isLoading)
                }
            }
            .safeAreaInset(edge: .bottom) {
                Button {
                    showAdvanced = true
                } label: {
                    Label("Advanced", systemImage: "slider.horizontal.3")
                        .font(.headline)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 12)
                }
                .buttonStyle(.borderedProminent)
                .tint(.secondary)
                .padding(.horizontal)
                .padding(.bottom, 8)
            }
            .fullScreenCover(isPresented: $showAdvanced) {
                NavigationStack {
                    HubView(apiClient: apiClient)
                        .toolbar {
                            ToolbarItem(placement: .topBarLeading) {
                                Button("Lights") {
                                    showAdvanced = false
                                }
                            }
                        }
                }
                .environmentObject(apiClient)
            }
            .task {
                await loadData()
            }
        }
    }

    /// Fetch groups and devices from the server.
    private func loadData() async {
        isLoading = true
        errorMessage = nil
        do {
            async let fetchedDevices = apiClient.fetchDevices()
            async let fetchedGroups: GroupsResponse = apiClient.get(
                "/api/groups"
            )

            let devs = try await fetchedDevices
            let grps = try await fetchedGroups.groups

            devices = devs

            // Build group info from server data.
            var infos: [GroupInfo] = []
            for (name, _) in grps.sorted(by: { $0.key < $1.key }) {
                // Find the virtual group device or derive from members.
                let groupId = "group:\(name)"
                let groupDev = devs.first { $0.ip == groupId }
                let memberDevs = devs.filter {
                    !$0.isVirtualGroup && ($0.group == name ||
                        (groupDev?.memberIps?.contains($0.ip) == true))
                }
                // Power: group device if present, else any member on.
                let power = groupDev?.power
                    ?? memberDevs.contains(where: { $0.power == true })
                let zones = groupDev?.zones
                    ?? memberDevs.reduce(0) { $0 + ($1.zones ?? 1) }
                let memberCount = groupDev?.memberIps?.count
                    ?? memberDevs.count

                infos.append(GroupInfo(
                    name: name,
                    groupId: groupId,
                    isPoweredOn: power,
                    brightness: power ? 100.0 : 0.0,
                    zones: zones,
                    memberCount: memberCount,
                    currentEffect: groupDev?.currentEffect
                ))
            }
            groups = infos
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }
}

/// Response wrapper for GET /api/groups.
struct GroupsResponse: Codable {
    let groups: [String: [String]]
}

/// State for a single group in the lights view.
struct GroupInfo: Identifiable {
    /// Group name.
    let name: String

    /// API identifier (``group:<name>``).
    let groupId: String

    /// Whether the group is powered on.
    var isPoweredOn: Bool

    /// Current brightness percentage (0–100).
    var brightness: Double

    /// Total zone count.
    let zones: Int

    /// Number of member devices.
    let memberCount: Int

    /// Currently running effect, if any.
    let currentEffect: String?

    /// Conform to ``Identifiable``.
    var id: String { name }
}

/// A single group card with power toggle and brightness slider.
struct GroupCard: View {
    /// Binding to the group state so slider/toggle update in place.
    @Binding var group: GroupInfo

    /// API client (not from environment — passed explicitly).
    let apiClient: APIClient

    /// Whether a power request is in flight.
    @State private var isTogglingPower: Bool = false

    /// Debounce timer for the brightness slider.
    @State private var brightnessTask: Task<Void, Never>?

    var body: some View {
        VStack(spacing: 12) {
            // Header: group name + power button.
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(group.name)
                        .font(.title2.bold())
                        .foregroundStyle(
                            group.isPoweredOn ? .primary : .secondary
                        )
                    HStack(spacing: 8) {
                        Text("\(group.memberCount) lights")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        if let effect = group.currentEffect {
                            Text(effect)
                                .font(.caption)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 1)
                                .background(Color.green.opacity(0.2))
                                .cornerRadius(4)
                        }
                    }
                }

                Spacer()

                // Large power button.
                Button {
                    Task { await togglePower() }
                } label: {
                    Image(systemName: group.isPoweredOn
                          ? "lightbulb.fill"
                          : "lightbulb.slash")
                        .font(.system(size: 32))
                        .foregroundStyle(
                            group.isPoweredOn ? .yellow : .secondary
                        )
                        .frame(width: 60, height: 60)
                        .background(
                            Circle()
                                .fill(group.isPoweredOn
                                      ? Color.yellow.opacity(0.15)
                                      : Color.secondary.opacity(0.1))
                        )
                }
                .buttonStyle(.plain)
                .disabled(isTogglingPower)
            }

            // Brightness slider — only interactive when powered on.
            if group.isPoweredOn {
                HStack {
                    Image(systemName: "sun.min")
                        .foregroundStyle(.secondary)
                    Slider(
                        value: $group.brightness,
                        in: 1...100,
                        step: 1
                    )
                    .onChange(of: group.brightness) { _, newValue in
                        debounceBrightness(Int(newValue))
                    }
                    Image(systemName: "sun.max.fill")
                        .foregroundStyle(.yellow)
                    Text("\(Int(group.brightness))%")
                        .font(.caption)
                        .frame(width: 40, alignment: .trailing)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding()
        .background(
            RoundedRectangle(cornerRadius: 16)
                .fill(Color(.secondarySystemGroupedBackground))
        )
    }

    /// Toggle group power on/off.
    private func togglePower() async {
        let newState = !group.isPoweredOn
        isTogglingPower = true
        do {
            try await apiClient.setPower(
                deviceId: group.groupId, on: newState
            )
            group.isPoweredOn = newState
            if newState {
                group.brightness = 100
            }
        } catch {
            // Revert on failure.
        }
        isTogglingPower = false
    }

    /// Debounce brightness slider to avoid flooding the server.
    private func debounceBrightness(_ value: Int) {
        brightnessTask?.cancel()
        brightnessTask = Task {
            // Wait 150ms before sending — slider generates many events.
            try? await Task.sleep(nanoseconds: 150_000_000)
            if Task.isCancelled { return }
            try? await apiClient.setBrightness(
                deviceId: group.groupId, brightness: value
            )
        }
    }
}
