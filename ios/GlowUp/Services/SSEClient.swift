// SSEClient.swift
// GlowUp — LIFX Remote Control
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import Foundation

/// Server-Sent Events client for live zone color streaming.
///
/// Connects to the ``GET /api/devices/{ip}/colors/stream`` endpoint
/// and parses the SSE ``data:`` lines into ``ZoneColor`` arrays.
/// Publishes updates at the server's fixed 4 Hz rate.
///
/// Usage::
///
///     let stream = SSEColorStream()
///     stream.connect(apiClient: client, ip: "192.0.2.10")
///     // stream.zones updates at 4 Hz
///     stream.disconnect()
@MainActor
class SSEColorStream: ObservableObject {
    /// Current zone colors, updated at 4 Hz from the SSE stream.
    @Published var zones: [ZoneColor] = []

    /// Whether the stream is actively connected.
    @Published var isConnected: Bool = false

    /// The active URLSession task (cancelled on disconnect).
    private var task: URLSessionDataTask?

    /// The URLSession configured for streaming (no caching).
    private var session: URLSession?

    /// Delegate that processes streaming bytes.
    private var delegate: SSEDelegate?

    /// Connect to the SSE stream for a device.
    ///
    /// If already connected, disconnects first.
    ///
    /// - Parameters:
    ///   - apiClient: The configured ``APIClient`` for building requests.
    ///   - deviceId: Device identifier (label, MAC, or IP).
    func connect(apiClient: APIClient, deviceId: String) {
        disconnect()

        guard let request = apiClient.sseRequest(deviceId: deviceId) else { return }

        // Create a delegate that calls back to update zones.
        let sseDelegate = SSEDelegate { [weak self] newZones in
            Task { @MainActor in
                self?.zones = newZones
                self?.isConnected = true
            }
        }
        self.delegate = sseDelegate

        // Use a dedicated session with the streaming delegate.
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 300
        config.timeoutIntervalForResource = 600
        let urlSession = URLSession(
            configuration: config,
            delegate: sseDelegate,
            delegateQueue: nil
        )
        self.session = urlSession

        let dataTask = urlSession.dataTask(with: request)
        self.task = dataTask
        dataTask.resume()
    }

    /// Disconnect from the SSE stream.
    func disconnect() {
        task?.cancel()
        task = nil
        session?.invalidateAndCancel()
        session = nil
        delegate = nil
        isConnected = false
    }
}

/// URLSession delegate that processes streaming SSE data.
///
/// Accumulates incoming bytes, splits on double-newline boundaries
/// (the SSE frame delimiter), and parses ``data:`` lines as JSON
/// containing zone color arrays.
private class SSEDelegate: NSObject, URLSessionDataDelegate {
    /// Callback invoked with parsed zone colors on each SSE event.
    private let onZones: ([ZoneColor]) -> Void

    /// Buffer for accumulating partial SSE data between delegate calls.
    private var buffer: String = ""

    /// JSON decoder reused across events.
    private let decoder = JSONDecoder()

    /// Initialize with a callback for zone color updates.
    ///
    /// - Parameter onZones: Called on each SSE event with the parsed colors.
    init(onZones: @escaping ([ZoneColor]) -> Void) {
        self.onZones = onZones
    }

    /// Process incoming data from the streaming response.
    func urlSession(
        _ session: URLSession,
        dataTask: URLSessionDataTask,
        didReceive data: Data
    ) {
        guard let text = String(data: data, encoding: .utf8) else { return }
        buffer += text

        // SSE events are delimited by double newlines.
        while let range = buffer.range(of: "\n\n") {
            let event = String(buffer[buffer.startIndex..<range.lowerBound])
            buffer = String(buffer[range.upperBound...])

            // Extract the data payload from "data: {json}" lines.
            for line in event.components(separatedBy: "\n") {
                if line.hasPrefix("data: ") {
                    let jsonStr = String(line.dropFirst(6))
                    guard let jsonData = jsonStr.data(using: .utf8) else {
                        continue
                    }
                    if let response = try? decoder.decode(
                        ZoneColorResponse.self, from: jsonData
                    ) {
                        onZones(response.zones)
                    }
                }
            }
        }
    }
}
