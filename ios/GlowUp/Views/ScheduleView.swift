// ScheduleView.swift
// GlowUp — LIFX Remote Control
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import SwiftUI

/// Read-only schedule viewer with enable/disable toggles.
///
/// Shows each schedule entry as a card with name, effect, group,
/// time window, and day-of-week information.  The currently active
/// entry is highlighted.  Entries can be enabled or disabled via
/// a toggle — the change is persisted on the server.
struct ScheduleView: View {
    @EnvironmentObject var apiClient: APIClient
    @Environment(\.dismiss) var dismiss

    /// Schedule entries from the server.
    @State private var entries: [ScheduleEntry] = []

    /// Error message for display.
    @State private var errorMessage: String?

    /// Whether a request is in progress.
    @State private var isLoading: Bool = false

    var body: some View {
        NavigationStack {
            Group {
                if entries.isEmpty && !isLoading {
                    ContentUnavailableView(
                        "No Schedule",
                        systemImage: "calendar.badge.exclamationmark",
                        description: Text(
                            errorMessage ?? "No schedule entries configured on the server."
                        )
                    )
                } else {
                    List {
                        ForEach(entries) { entry in
                            ScheduleEntryRow(
                                entry: entry,
                                onToggle: { enabled in
                                    Task {
                                        await toggleEntry(entry, enabled: enabled)
                                    }
                                }
                            )
                        }
                    }
                }
            }
            .navigationTitle("Schedule")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
            .refreshable {
                await refreshSchedule()
            }
            .task {
                await refreshSchedule()
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
    }

    /// Fetch the schedule from the server.
    private func refreshSchedule() async {
        isLoading = true
        do {
            entries = try await apiClient.fetchSchedule()
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }

    /// Toggle an entry's enabled state on the server.
    private func toggleEntry(_ entry: ScheduleEntry, enabled: Bool) async {
        do {
            try await apiClient.setScheduleEnabled(
                index: entry.index, enabled: enabled
            )
            await refreshSchedule()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

/// A single schedule entry displayed as a rich card.
struct ScheduleEntryRow: View {
    /// The schedule entry to display.
    let entry: ScheduleEntry

    /// Callback when the toggle changes.
    let onToggle: (Bool) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            // Row 1: Name and active badge.
            HStack {
                Text(entry.name)
                    .font(.headline)
                    .foregroundStyle(entry.enabled ? .primary : .secondary)
                Spacer()
                if entry.active {
                    Text("ACTIVE")
                        .font(.caption2)
                        .fontWeight(.bold)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3)
                        .background(Color.green)
                        .foregroundStyle(.white)
                        .cornerRadius(4)
                }
            }

            // Row 2: Effect and group.
            HStack(spacing: 12) {
                Label(entry.effect, systemImage: "wand.and.stars")
                    .font(.subheadline)
                    .foregroundStyle(entry.enabled ? .primary : .secondary)
                Label(entry.group, systemImage: "square.stack.3d.up")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }

            // Row 3: Time window and days.
            HStack(spacing: 12) {
                if let start = entry.startResolved,
                   let stop = entry.stopResolved {
                    Label(
                        "\(start) \u{2192} \(stop)",
                        systemImage: "clock"
                    )
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                } else {
                    Label(
                        "\(entry.start) \u{2192} \(entry.stop)",
                        systemImage: "clock"
                    )
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                }
                Spacer()
                Text(entry.daysDisplay)
                    .font(.caption)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(Color.blue.opacity(0.15))
                    .foregroundStyle(.blue)
                    .cornerRadius(4)
            }

            // Row 4: Enable/disable toggle.
            Toggle("Enabled", isOn: Binding(
                get: { entry.enabled },
                set: { onToggle($0) }
            ))
            .font(.subheadline)
        }
        .padding(.vertical, 4)
        .opacity(entry.enabled ? 1.0 : 0.6)
    }
}
