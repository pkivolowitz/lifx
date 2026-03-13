// AudioStreamService.swift
// GlowUp — LIFX Remote Control
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import AVFoundation
import Accelerate
import Foundation

/// Captures microphone audio, computes FFT-based frequency bands and
/// signal features, and streams them to the GlowUp server via the
/// ``/api/media/signals/ingest`` endpoint.
///
/// This turns the iPhone into a low-latency audio sensor node in the
/// GlowUp media pipeline.  The server writes the ingested signals to
/// its ``SignalBus``, making them available to any effect — no RTSP,
/// no ffmpeg, no camera mic lag.
///
/// Architecture: "Any Source, Any Effect, Any Surface"
///   - Source: iPhone microphone (this service)
///   - Effect: Any ``MediaEffect`` reading from ``source: "iphone"``
///   - Surface: Any LIFX device or group
@MainActor
class AudioStreamService: ObservableObject {

    // MARK: - Published state

    /// Whether the service is currently streaming.
    @Published var isStreaming: Bool = false

    /// Current RMS level for UI meter display.
    @Published var currentRMS: Float = 0.0

    /// Current frequency bands for UI visualization.
    @Published var currentBands: [Float] = Array(repeating: 0, count: 8)

    /// Error message if something goes wrong.
    @Published var errorMessage: String?

    // MARK: - Configuration

    /// Source name sent to the server (effects use this to read signals).
    let sourceName: String = "iphone"

    /// Reference to the API client for server URL and token.
    private weak var apiClient: APIClient?

    /// The audio processing engine (non-MainActor).
    private var processor: AudioProcessor?

    // MARK: - Lifecycle

    /// Initialize with a reference to the API client.
    init(apiClient: APIClient) {
        self.apiClient = apiClient
    }

    /// Start capturing audio and streaming signals.
    func start() {
        guard !isStreaming else { return }
        errorMessage = nil

        let serverURL = apiClient?.serverURL ?? ""
        let token = apiClient?.token ?? ""

        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.record, mode: .measurement)
            try session.setPreferredSampleRate(16000)
            try session.setActive(true)

            let proc = AudioProcessor(
                sourceName: sourceName,
                serverURL: serverURL,
                token: token
            ) { [weak self] rms, bands in
                Task { @MainActor [weak self] in
                    self?.currentRMS = rms
                    self?.currentBands = bands
                }
            }
            try proc.start()
            self.processor = proc
            isStreaming = true
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    /// Stop capturing and streaming.
    func stop() {
        processor?.stop()
        processor = nil
        isStreaming = false
        currentRMS = 0
        currentBands = Array(repeating: 0, count: 8)
    }
}

// MARK: - Audio Processor (non-MainActor)

/// Handles audio capture, FFT, and server posting off the main actor.
///
/// All audio processing runs on the audio thread callback.  Server
/// posts run on a dedicated background queue.  Only the UI update
/// callback touches the main actor.
private class AudioProcessor {
    /// Source name for the ingest payload.
    let sourceName: String

    /// Server URL for posting signals.
    let serverURL: String

    /// Auth token for the server.
    let token: String

    /// Callback to update UI (called from audio thread, must dispatch).
    let uiCallback: (Float, [Float]) -> Void

    // Audio engine.
    private var audioEngine: AVAudioEngine?

    // FFT config.
    private let fftSize: Int = 1024
    private let bandCount: Int = 8
    private let sampleRate: Float = 16000
    private let smoothing: Float = 0.3
    private let postInterval: TimeInterval = 1.0 / 15.0

    // FFT state.
    private let fftSetup: vDSP.FFT<DSPSplitComplex>?
    private let hannWindow: [Float]
    private var smoothBands: [Float]

    // Beat detection.
    private var energyHistory: [Float] = []
    private let beatHistorySize: Int = 43
    private let beatThreshold: Float = 1.5
    private var beatValue: Float = 0.0
    private var lastBeatTime: TimeInterval = 0

    // Timing.
    private var lastPostTime: TimeInterval = 0

    // Post queue.
    private let postQueue = DispatchQueue(
        label: "com.glowup.audio.post",
        qos: .userInteractive
    )

    init(sourceName: String, serverURL: String, token: String,
         uiCallback: @escaping (Float, [Float]) -> Void) {
        self.sourceName = sourceName
        self.serverURL = serverURL
        self.token = token
        self.uiCallback = uiCallback
        self.smoothBands = Array(repeating: 0, count: bandCount)
        self.hannWindow = vDSP.window(
            ofType: Float.self,
            usingSequence: .hanningNormalized,
            count: fftSize,
            isHalfWindow: false
        )
        let log2n = vDSP_Length(log2(Float(fftSize)))
        self.fftSetup = vDSP.FFT(
            log2n: log2n,
            radix: .radix2,
            ofType: DSPSplitComplex.self
        )
    }

    func start() throws {
        let engine = AVAudioEngine()
        let inputNode = engine.inputNode

        let bufferSize = AVAudioFrameCount(fftSize)
        inputNode.installTap(
            onBus: 0,
            bufferSize: bufferSize,
            format: nil
        ) { [weak self] buffer, _ in
            self?.processBuffer(buffer)
        }

        engine.prepare()
        try engine.start()
        self.audioEngine = engine
    }

    func stop() {
        audioEngine?.stop()
        audioEngine?.inputNode.removeTap(onBus: 0)
        audioEngine = nil
    }

    // MARK: - Audio processing

    private func processBuffer(_ buffer: AVAudioPCMBuffer) {
        guard let channelData = buffer.floatChannelData?[0] else { return }
        let frameCount = Int(buffer.frameLength)
        guard frameCount >= fftSize else { return }

        let samples = Array(UnsafeBufferPointer(
            start: channelData, count: fftSize
        ))

        // --- RMS ---
        var rms: Float = 0
        vDSP_rmsqv(samples, 1, &rms, vDSP_Length(fftSize))

        // --- Windowed FFT ---
        var windowed = [Float](repeating: 0, count: fftSize)
        vDSP_vmul(samples, 1, hannWindow, 1, &windowed, 1,
                  vDSP_Length(fftSize))

        let halfN = fftSize / 2
        var realPart = [Float](repeating: 0, count: halfN)
        var imagPart = [Float](repeating: 0, count: halfN)

        var magnitudes = [Float](repeating: 0, count: halfN)

        realPart.withUnsafeMutableBufferPointer { realBuf in
            imagPart.withUnsafeMutableBufferPointer { imagBuf in
                var split = DSPSplitComplex(
                    realp: realBuf.baseAddress!,
                    imagp: imagBuf.baseAddress!
                )

                // Pack interleaved real samples into split complex.
                windowed.withUnsafeBufferPointer { ptr in
                    ptr.baseAddress!.withMemoryRebound(
                        to: DSPComplex.self, capacity: halfN
                    ) { complexPtr in
                        vDSP_ctoz(complexPtr, 2, &split, 1,
                                  vDSP_Length(halfN))
                    }
                }

                // Forward FFT (in-place).
                fftSetup?.transform(
                    input: split,
                    output: &split,
                    direction: .forward
                )

                // Magnitudes.
                vDSP_zvabs(&split, 1, &magnitudes, 1,
                           vDSP_Length(halfN))
            }
        }

        var scale: Float = 2.0 / Float(fftSize)
        vDSP_vsmul(magnitudes, 1, &scale, &magnitudes, 1,
                   vDSP_Length(halfN))

        // --- Log band binning ---
        let bands = binToBands(magnitudes)

        // --- Smoothing ---
        for i in 0..<bandCount {
            smoothBands[i] = smoothing * smoothBands[i]
                + (1.0 - smoothing) * bands[i]
        }

        // Global peak normalization.
        let globalPeak = max(smoothBands.max() ?? 1e-6, 1e-6)
        let normBands = smoothBands.map { min(1.0, $0 / globalPeak) }

        // --- Derived signals ---
        let bass = (normBands[0] + normBands[1]) / 2.0
        let treble = (normBands[6] + normBands[7]) / 2.0
        let mid = (normBands[2] + normBands[3]
                   + normBands[4] + normBands[5]) / 4.0
        let energy = smoothBands.reduce(0, +)

        // --- Spectral centroid ---
        let nyquist = sampleRate / 2.0
        var centroid: Float = 0.5
        let magSum = magnitudes.reduce(0, +)
        if magSum > 1e-6 {
            var weightedSum: Float = 0
            for i in 0..<halfN {
                weightedSum += Float(i) * nyquist
                    / Float(halfN) * magnitudes[i]
            }
            centroid = min(1.0, max(0.0,
                                    (weightedSum / magSum) / nyquist))
        }

        // --- Beat detection ---
        let now = ProcessInfo.processInfo.systemUptime
        energyHistory.append(energy)
        if energyHistory.count > beatHistorySize {
            energyHistory.removeFirst(
                energyHistory.count - beatHistorySize
            )
        }
        let avgEnergy = energyHistory.reduce(0, +)
            / Float(energyHistory.count)
        if energy > avgEnergy * beatThreshold && avgEnergy > 1e-6 {
            if now - lastBeatTime > 0.15 {
                beatValue = 1.0
                lastBeatTime = now
            }
        }
        if now > lastBeatTime {
            beatValue = max(0, 1.0 - Float((now - lastBeatTime) / 0.2))
        }

        // --- Throttle ---
        guard now - lastPostTime >= postInterval else { return }
        lastPostTime = now

        let postRMS = min(1.0, rms * 5.0)
        let postBands = normBands
        let postBeat = beatValue

        // UI update.
        uiCallback(postRMS, postBands)

        // Server post.
        postQueue.async { [weak self] in
            self?.postSignals(
                bands: postBands, rms: postRMS, beat: postBeat,
                bass: bass, mid: mid, treble: treble,
                energy: min(1.0, energy), centroid: centroid
            )
        }
    }

    /// Map FFT magnitudes into logarithmically-spaced frequency bands.
    private func binToBands(_ magnitudes: [Float]) -> [Float] {
        let nBins = magnitudes.count
        guard nBins > 0 else {
            return Array(repeating: 0, count: bandCount)
        }
        let nyquist = sampleRate / 2.0
        let binHz = sampleRate / Float(nBins * 2)
        let minFreq: Float = 20.0
        let logMin = log(minFreq)
        let logMax = log(nyquist)

        var bands = [Float](repeating: 0, count: bandCount)
        for b in 0..<bandCount {
            let loFreq = exp(logMin + (logMax - logMin)
                             * Float(b) / Float(bandCount))
            let hiFreq = exp(logMin + (logMax - logMin)
                             * Float(b + 1) / Float(bandCount))
            let loBin = max(0, Int(loFreq / binHz))
            let hiBin = min(nBins - 1, Int(hiFreq / binHz))
            if hiBin >= loBin {
                var sum: Float = 0
                for i in loBin...hiBin { sum += magnitudes[i] }
                bands[b] = sum / Float(hiBin - loBin + 1)
            }
        }
        return bands
    }

    // MARK: - Server communication

    private func postSignals(
        bands: [Float], rms: Float, beat: Float,
        bass: Float, mid: Float, treble: Float,
        energy: Float, centroid: Float
    ) {
        guard !serverURL.isEmpty, !token.isEmpty else { return }

        let base = serverURL.hasSuffix("/")
            ? String(serverURL.dropLast()) : serverURL
        guard let url = URL(
            string: base + "/api/media/signals/ingest"
        ) else { return }

        let body: [String: Any] = [
            "source": sourceName,
            "signals": [
                "bands": bands.map { Double($0) },
                "rms": Double(rms),
                "beat": Double(beat),
                "bass": Double(bass),
                "mid": Double(mid),
                "treble": Double(treble),
                "energy": Double(energy),
                "centroid": Double(centroid),
            ] as [String: Any],
        ]

        guard let jsonData = try? JSONSerialization.data(
            withJSONObject: body
        ) else { return }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(token)",
                        forHTTPHeaderField: "Authorization")
        request.setValue("application/json",
                        forHTTPHeaderField: "Content-Type")
        request.httpBody = jsonData
        request.timeoutInterval = 0.5

        URLSession.shared.dataTask(with: request).resume()
    }
}

/// Errors specific to the audio streaming service.
enum AudioStreamError: LocalizedError {
    /// Could not create the desired audio format.
    case formatError

    var errorDescription: String? {
        switch self {
        case .formatError:
            return "Could not create audio format"
        }
    }
}
