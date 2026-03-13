// AudioStreamView.swift
// GlowUp — LIFX Remote Control
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import SwiftUI

/// Microphone streaming control view.
///
/// Lets the user start/stop the iPhone microphone as an audio source
/// for the GlowUp media pipeline.  Shows a live VU meter and
/// frequency band visualization while streaming.
///
/// When streaming, the phone POSTs audio signal values (bands, RMS,
/// beat, centroid) to the server at ~15 Hz.  Any effect using
/// ``source: "iphone"`` will respond in real time.
struct AudioStreamView: View {
    @EnvironmentObject var apiClient: APIClient
    @StateObject private var audioService: AudioStreamService

    /// Initialize with the shared API client.
    ///
    /// The ``AudioStreamService`` is created as a ``@StateObject``
    /// so it persists across view updates.
    init(apiClient: APIClient) {
        _audioService = StateObject(
            wrappedValue: AudioStreamService(apiClient: apiClient)
        )
    }

    var body: some View {
        List {
            // Status section.
            Section {
                HStack {
                    Image(systemName: audioService.isStreaming
                          ? "mic.fill" : "mic.slash")
                        .foregroundStyle(
                            audioService.isStreaming ? .red : .secondary
                        )
                        .font(.title2)
                    VStack(alignment: .leading) {
                        Text(audioService.isStreaming
                             ? "Streaming" : "Idle")
                            .font(.headline)
                        Text("Source: \(audioService.sourceName)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button {
                        if audioService.isStreaming {
                            audioService.stop()
                        } else {
                            audioService.start()
                        }
                    } label: {
                        Text(audioService.isStreaming ? "Stop" : "Start")
                            .font(.headline)
                            .padding(.horizontal, 16)
                            .padding(.vertical, 8)
                            .background(audioService.isStreaming
                                        ? Color.red : Color.accentColor)
                            .foregroundStyle(.white)
                            .clipShape(Capsule())
                    }
                }
                .padding(.vertical, 4)
            }

            // Live meter section (visible only while streaming).
            if audioService.isStreaming {
                Section("Level") {
                    VUMeterView(level: audioService.currentRMS)
                        .frame(height: 24)
                }

                Section("Frequency Bands") {
                    BandVisualizerView(bands: audioService.currentBands)
                        .frame(height: 80)
                }

                Section {
                    Text("Use **source: \"iphone\"** in any audio effect to respond to this microphone.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            // Error display.
            if let error = audioService.errorMessage {
                Section {
                    Label(error, systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.red)
                }
            }
        }
        .navigationTitle("Mic Stream")
        .onDisappear {
            // Stop streaming when navigating away.
            audioService.stop()
        }
    }
}

// MARK: - VU Meter

/// A horizontal VU meter bar.
struct VUMeterView: View {
    let level: Float

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                // Background track.
                RoundedRectangle(cornerRadius: 4)
                    .fill(Color.secondary.opacity(0.2))

                // Active level.
                RoundedRectangle(cornerRadius: 4)
                    .fill(levelColor)
                    .frame(
                        width: max(0, geo.size.width * CGFloat(level))
                    )
            }
        }
    }

    /// Color shifts from green to yellow to red with level.
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

    /// Labels for the 8 default bands.
    private let bandLabels = [
        "Sub", "Bass", "Low", "LMid",
        "Mid", "HMid", "Hi", "Air"
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

    /// Gradient from warm (bass) to cool (treble).
    private func bandColor(index: Int) -> Color {
        let fraction = Double(index) / Double(max(1, bands.count - 1))
        return Color(
            hue: fraction * 0.66,  // 0 (red) to 0.66 (blue)
            saturation: 0.8,
            brightness: 0.9
        )
    }
}
