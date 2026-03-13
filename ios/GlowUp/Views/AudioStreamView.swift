// AudioStreamView.swift
// GlowUp — LIFX Remote Control
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import SwiftUI

/// Three-step wizard for audio-reactive lighting.
///
/// Flow: Sensor → Effect → Surface
///
/// 1. **Sensor**: None (non-reactive effects), iPhone mic, or server
///    camera sources.
/// 2. **Effect**: Filtered by sensor — None shows all effects, mic
///    shows only audio-reactive effects (soundlevel, waveform).
/// 3. **Surface**: Device or group to render on.
///
/// A single "Go" button starts everything: mic capture (if needed),
/// effect playback on the chosen surface.  "Stop" tears it all down.
struct AudioStreamView: View {
    /// API client for server communication.
    let apiClient: APIClient

    @StateObject private var audioService: AudioStreamService

    // MARK: - Wizard state

    /// Available sensors (built from server sources + local mic).
    @State private var sensors: [Sensor] = []

    /// Selected sensor.
    @State private var selectedSensor: Sensor?

    /// Available effects (filtered by sensor type).
    @State private var effects: [Effect] = []

    /// All effects from server (unfiltered).
    @State private var allEffects: [Effect] = []

    /// Selected effect.
    @State private var selectedEffect: Effect?

    /// Available devices from server.
    @State private var devices: [Device] = []

    /// Selected device.
    @State private var selectedDevice: Device?

    /// Whether the pipeline is running.
    @State private var isRunning: Bool = false

    /// Loading state.
    @State private var isLoading: Bool = true

    /// Error message.
    @State private var errorMessage: String?

    /// Audio-reactive effect names (effects that extend MediaEffect).
    private let audioEffectNames: Set<String> = [
        "soundlevel", "waveform",
    ]

    init(apiClient: APIClient) {
        self.apiClient = apiClient
        _audioService = StateObject(
            wrappedValue: AudioStreamService(apiClient: apiClient)
        )
    }

    var body: some View {
        List {
            if isLoading {
                Section {
                    HStack {
                        Spacer()
                        ProgressView("Loading...")
                        Spacer()
                    }
                }
            } else {
                // Step 1: Sensor
                sensorSection

                // Step 2: Effect (visible after sensor selected)
                if selectedSensor != nil {
                    effectSection
                }

                // Step 3: Surface (visible after effect selected)
                if selectedEffect != nil {
                    surfaceSection
                }

                // Live feedback (visible when running)
                if isRunning && selectedSensor?.type == .iphone {
                    liveFeedbackSection
                }

                // Action button (visible when all three selected)
                if selectedSensor != nil && selectedEffect != nil
                    && selectedDevice != nil {
                    actionSection
                }
            }

            // Error display.
            if let error = errorMessage {
                Section {
                    Label(error, systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.red)
                }
            }
        }
        .navigationTitle("Audio Reactive")
        .task { await loadData() }
        .onDisappear { stopEverything() }
    }

    // MARK: - Sections

    /// Step 1: Sensor picker.
    private var sensorSection: some View {
        Section("1. Sensor") {
            ForEach(sensors, id: \.id) { sensor in
                Button {
                    withAnimation {
                        selectSensor(sensor)
                    }
                } label: {
                    sensorRow(sensor)
                }
            }
        }
    }

    /// Row view for a sensor item.
    @ViewBuilder
    private func sensorRow(_ sensor: Sensor) -> some View {
        HStack {
            Image(systemName: sensor.icon)
                .frame(width: 24)
                .foregroundStyle(sensor.color)
            VStack(alignment: .leading) {
                Text(sensor.displayName)
                    .foregroundStyle(.primary)
                Text(sensor.subtitle)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if selectedSensor?.id == sensor.id {
                Image(systemName: "checkmark")
                    .foregroundStyle(.tint)
            }
        }
    }

    /// Step 2: Effect picker (filtered by sensor).
    private var effectSection: some View {
        Section("2. Effect") {
            ForEach(effects, id: \.id) { effect in
                Button {
                    withAnimation {
                        selectedEffect = effect
                    }
                } label: {
                    effectRow(effect)
                }
            }
        }
    }

    /// Row view for an effect item.
    @ViewBuilder
    private func effectRow(_ effect: Effect) -> some View {
        HStack {
            VStack(alignment: .leading) {
                Text(effect.name)
                    .foregroundStyle(.primary)
                Text(effect.description)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if selectedEffect?.id == effect.id {
                Image(systemName: "checkmark")
                    .foregroundStyle(.tint)
            }
        }
    }

    /// Step 3: Surface picker (device or group).
    private var surfaceSection: some View {
        Section("3. Surface") {
            ForEach(devices, id: \.id) { device in
                Button {
                    withAnimation {
                        selectedDevice = device
                    }
                } label: {
                    deviceRow(device)
                }
            }
        }
    }

    /// Row view for a device item.
    @ViewBuilder
    private func deviceRow(_ device: Device) -> some View {
        HStack {
            VStack(alignment: .leading) {
                Text(device.displayName)
                    .foregroundStyle(.primary)
                Text(device.product ?? "Unknown")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if selectedDevice?.id == device.id {
                Image(systemName: "checkmark")
                    .foregroundStyle(.tint)
            }
        }
    }

    /// Live audio feedback when mic is active.
    private var liveFeedbackSection: some View {
        Section("Live") {
            VUMeterView(level: audioService.currentRMS)
                .frame(height: 20)
            BandVisualizerView(bands: audioService.currentBands)
                .frame(height: 60)
        }
    }

    /// Go / Stop button.
    private var actionSection: some View {
        Section {
            Button {
                if isRunning {
                    stopEverything()
                } else {
                    Task { await startEverything() }
                }
            } label: {
                HStack {
                    Spacer()
                    Label(
                        isRunning ? "Stop" : "Go",
                        systemImage: isRunning
                            ? "stop.fill" : "play.fill"
                    )
                    .font(.title2.bold())
                    .foregroundStyle(.white)
                    Spacer()
                }
                .padding(.vertical, 8)
                .background(isRunning ? Color.red : Color.green)
                .clipShape(RoundedRectangle(cornerRadius: 10))
            }
            .listRowBackground(Color.clear)
        }
    }

    // MARK: - Data loading

    /// Fetch devices, effects, and media sources from the server.
    private func loadData() async {
        isLoading = true
        do {
            async let fetchedDevices = apiClient.fetchDevices()
            async let fetchedEffects = apiClient.fetchEffects()

            devices = try await fetchedDevices
            allEffects = try await fetchedEffects

            // Build sensor list.
            var sensorList: [Sensor] = [
                Sensor(
                    id: "none",
                    type: .none,
                    displayName: "None",
                    subtitle: "Non-reactive effects",
                    icon: "slash.circle",
                    color: .secondary
                ),
                Sensor(
                    id: "iphone",
                    type: .iphone,
                    displayName: "iPhone Mic",
                    subtitle: "Low-latency audio from this device",
                    icon: "mic.fill",
                    color: .red
                ),
            ]

            // Add server-configured camera sources.
            do {
                let sources: MediaSourcesResponse = try await apiClient.get(
                    "/api/media/sources"
                )
                if let sourceList = sources.sources.sources {
                    for src in sourceList {
                        sensorList.append(Sensor(
                            id: src.name,
                            type: .server,
                            displayName: src.name.capitalized,
                            subtitle: "Camera audio (\(src.type))",
                            icon: "video.fill",
                            color: .blue
                        ))
                    }
                }
            } catch {
                // No media sources configured — that's fine.
            }

            sensors = sensorList
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }

    // MARK: - Selection logic

    /// Handle sensor selection — filter effects accordingly.
    private func selectSensor(_ sensor: Sensor) {
        selectedSensor = sensor
        selectedEffect = nil

        switch sensor.type {
        case .none:
            // Show all non-hidden, non-audio-reactive effects.
            effects = allEffects.filter {
                !$0.hidden && !audioEffectNames.contains($0.name)
            }
        case .iphone, .server:
            // Show only audio-reactive effects.
            effects = allEffects.filter {
                audioEffectNames.contains($0.name)
            }
        }
    }

    // MARK: - Start / Stop

    /// Start the full pipeline: sensor + effect on surface.
    private func startEverything() async {
        guard let sensor = selectedSensor,
              let effect = selectedEffect,
              let device = selectedDevice else { return }

        errorMessage = nil

        // Start iPhone mic if needed.
        if sensor.type == .iphone {
            audioService.start()
            // Brief delay for first signals to reach server.
            try? await Task.sleep(nanoseconds: 500_000_000)
        }

        // Start server camera source if needed.
        if sensor.type == .server {
            do {
                let _: GenericOKResponse = try await apiClient.post(
                    "/api/media/sources/\(sensor.id)/start",
                    body: EmptyBody()
                )
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            } catch {
                errorMessage = "Failed to start \(sensor.displayName): \(error.localizedDescription)"
                return
            }
        }

        // Build params — set source name for audio effects.
        var params: [String: Any] = [:]
        if sensor.type == .iphone {
            params["source"] = "iphone"
        } else if sensor.type == .server {
            params["source"] = sensor.id
        }

        // Play the effect on the device.
        do {
            _ = try await apiClient.play(
                ip: device.ip,
                effectName: effect.name,
                params: params
            )
            withAnimation { isRunning = true }
        } catch {
            errorMessage = error.localizedDescription
            audioService.stop()
        }
    }

    /// Stop everything.
    private func stopEverything() {
        audioService.stop()

        if let device = selectedDevice {
            Task {
                _ = try? await apiClient.stop(ip: device.ip)
            }
        }

        withAnimation { isRunning = false }
    }
}

// MARK: - Sensor model

/// Represents an audio source in the wizard.
struct Sensor: Identifiable {
    let id: String
    let type: SensorType
    let displayName: String
    let subtitle: String
    let icon: String
    let color: Color
}

/// The kind of sensor.
enum SensorType {
    /// No sensor — non-reactive effects.
    case none
    /// iPhone microphone (local capture + HTTP ingest).
    case iphone
    /// Server-managed source (RTSP camera via ffmpeg).
    case server
}

/// Response wrapper for GET /api/media/sources.
struct MediaSourcesResponse: Codable {
    let sources: MediaSourcesInner
}

struct MediaSourcesInner: Codable {
    let sources: [MediaSourceInfo]?
}

struct MediaSourceInfo: Codable {
    let name: String
    let type: String
}

// MARK: - VU Meter

/// A horizontal VU meter bar.
struct VUMeterView: View {
    let level: Float

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 4)
                    .fill(Color.secondary.opacity(0.2))
                RoundedRectangle(cornerRadius: 4)
                    .fill(levelColor)
                    .frame(
                        width: max(0, geo.size.width * CGFloat(level))
                    )
            }
        }
    }

    private var levelColor: Color {
        if level < 0.5 { return .green }
        if level < 0.8 { return .yellow }
        return .red
    }
}

// MARK: - Band Visualizer

/// Vertical bar chart of frequency bands.
struct BandVisualizerView: View {
    let bands: [Float]

    private let bandLabels = [
        "Sub", "Bass", "Low", "LMid",
        "Mid", "HMid", "Hi", "Air",
    ]

    var body: some View {
        GeometryReader { geo in
            HStack(alignment: .bottom, spacing: 4) {
                ForEach(0..<bands.count, id: \.self) { i in
                    VStack(spacing: 2) {
                        RoundedRectangle(cornerRadius: 3)
                            .fill(bandColor(index: i))
                            .frame(
                                height: max(
                                    2,
                                    geo.size.height * 0.85
                                        * CGFloat(bands[i])
                                )
                            )
                        if i < bandLabels.count {
                            Text(bandLabels[i])
                                .font(.system(size: 8))
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
    }

    private func bandColor(index: Int) -> Color {
        let fraction = Double(index) / Double(max(1, bands.count - 1))
        return Color(
            hue: fraction * 0.66,
            saturation: 0.8,
            brightness: 0.9
        )
    }
}
