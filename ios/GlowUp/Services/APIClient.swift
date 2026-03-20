// APIClient.swift
// GlowUp — LIFX Remote Control
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import Foundation

/// Percent-encode a device identifier for use in a URL path segment.
///
/// Labels may contain spaces and other characters that are not
/// valid in URL paths.  This encodes everything except unreserved
/// characters (RFC 3986 §2.3).
func urlEncodeDeviceId(_ identifier: String) -> String {
    // .urlPathAllowed keeps slashes — we need pure segment encoding.
    let allowed = CharacterSet.urlPathAllowed.subtracting(CharacterSet(charactersIn: "/"))
    return identifier.addingPercentEncoding(withAllowedCharacters: allowed) ?? identifier
}

/// HTTP client for the GlowUp REST API.
///
/// Manages the server URL and bearer token, persisting both in the
/// iOS Keychain.  All methods are async and throw on network or
/// server errors.
///
/// Published as an ``ObservableObject`` so SwiftUI views can react
/// to configuration changes.
@MainActor
class APIClient: ObservableObject {
    /// The base URL for the GlowUp server (e.g., ``https://lights.example.com``).
    @Published var serverURL: String {
        didSet { KeychainHelper.saveServerURL(serverURL) }
    }

    /// The bearer token for API authentication.
    @Published var token: String {
        didSet { KeychainHelper.saveToken(token) }
    }

    /// Whether the client has been configured with a URL and token.
    var isConfigured: Bool {
        !serverURL.isEmpty && !token.isEmpty
    }

    /// Whether the user has authenticated this session.
    ///
    /// Starts ``false`` on every app launch so the login screen is
    /// always presented first.  Set to ``true`` after a successful
    /// connection test.
    @Published var isAuthenticated: Bool = false

    /// Shared URL session for all API requests.
    private let session: URLSession

    /// Initialize the client, loading persisted credentials from Keychain.
    init() {
        self.serverURL = KeychainHelper.loadServerURL() ?? ""
        self.token = KeychainHelper.loadToken() ?? ""
        self.session = URLSession(configuration: .ephemeral)
    }

    // MARK: - Device endpoints

    /// Fetch all configured devices.
    ///
    /// - Returns: An array of ``Device`` from the server.
    /// - Throws: ``APIError`` on failure.
    func fetchDevices() async throws -> [Device] {
        let response: DeviceListResponse = try await get("/api/devices")
        return response.devices
    }

    /// Fetch the current status of a device's effect engine.
    ///
    /// - Parameter deviceId: Device identifier (label, MAC, or IP).
    /// - Returns: The ``DeviceStatus`` for the device.
    /// - Throws: ``APIError`` on failure.
    func fetchStatus(deviceId: String) async throws -> DeviceStatus {
        let encoded = urlEncodeDeviceId(deviceId)
        return try await get("/api/devices/\(encoded)/status")
    }

    /// Fetch a snapshot of the current zone colors.
    ///
    /// - Parameter deviceId: Device identifier (label, MAC, or IP).
    /// - Returns: An array of ``ZoneColor``.
    /// - Throws: ``APIError`` on failure.
    func fetchColors(deviceId: String) async throws -> [ZoneColor] {
        let encoded = urlEncodeDeviceId(deviceId)
        let response: ZoneColorResponse = try await get(
            "/api/devices/\(encoded)/colors"
        )
        return response.zones
    }

    // MARK: - Effect endpoints

    /// Fetch all available effects with parameter metadata.
    ///
    /// - Returns: An array of ``Effect`` sorted by name.
    /// - Throws: ``APIError`` on failure.
    func fetchEffects() async throws -> [Effect] {
        let response: EffectListResponse = try await get("/api/effects")
        return response.toEffectArray()
    }

    /// Save tuned parameter values as the server-side defaults for an effect.
    ///
    /// These defaults are used by the scheduler and reported by
    /// ``GET /api/effects`` on subsequent queries.
    ///
    /// - Parameters:
    ///   - effectName: Registered effect name.
    ///   - params: Parameter values to save as defaults.
    /// - Throws: ``APIError`` on failure.
    func saveEffectDefaults(
        effectName: String,
        params: [String: Any]
    ) async throws {
        let body: [String: Any] = ["params": params]
        let _: GenericOKResponse = try await postRaw(
            "/api/effects/\(effectName)/defaults", body: body
        )
    }

    /// Start an effect on a device.
    ///
    /// - Parameters:
    ///   - deviceId: Device identifier (label, MAC, or IP).
    ///   - effectName: Registered effect name.
    ///   - params: Parameter overrides (name → value).
    /// - Returns: The updated ``DeviceStatus``.
    /// - Throws: ``APIError`` on failure.
    func play(
        deviceId: String,
        effectName: String,
        params: [String: Any]
    ) async throws -> DeviceStatus {
        let encoded = urlEncodeDeviceId(deviceId)
        let body: [String: Any] = [
            "effect": effectName,
            "params": params,
        ]
        return try await postRaw("/api/devices/\(encoded)/play", body: body)
    }

    /// Stop the current effect on a device.
    ///
    /// - Parameter deviceId: Device identifier (label, MAC, or IP).
    /// - Returns: The updated ``DeviceStatus``.
    /// - Throws: ``APIError`` on failure.
    func stop(deviceId: String) async throws -> DeviceStatus {
        let encoded = urlEncodeDeviceId(deviceId)
        return try await post("/api/devices/\(encoded)/stop", body: EmptyBody())
    }

    /// Resume the schedule on a device by clearing its phone override.
    ///
    /// The scheduler will resume control on its next poll cycle.
    ///
    /// - Parameter deviceId: Device identifier (label, MAC, or IP).
    /// - Returns: The updated ``DeviceStatus``.
    /// - Throws: ``APIError`` on failure.
    func resume(deviceId: String) async throws -> DeviceStatus {
        let encoded = urlEncodeDeviceId(deviceId)
        return try await post("/api/devices/\(encoded)/resume", body: EmptyBody())
    }

    /// Turn a device on or off.
    ///
    /// - Parameters:
    ///   - deviceId: Device identifier (label, MAC, or IP).
    ///   - on: ``true`` to power on, ``false`` to power off.
    /// - Throws: ``APIError`` on failure.
    func setPower(deviceId: String, on: Bool) async throws {
        struct PowerBody: Codable { let on: Bool }
        let encoded = urlEncodeDeviceId(deviceId)
        let _: [String: AnyCodableValue] = try await post(
            "/api/devices/\(encoded)/power",
            body: PowerBody(on: on)
        )
    }

    /// Pulse a device's brightness to visually locate it.
    ///
    /// The server pulses the device for ~10 seconds in the background.
    /// This method returns immediately after the server acknowledges.
    ///
    /// - Parameter deviceId: Device identifier (label, MAC, or IP).
    /// - Throws: ``APIError`` on failure.
    func identify(deviceId: String) async throws {
        let encoded = urlEncodeDeviceId(deviceId)
        let _: [String: AnyCodableValue] = try await post(
            "/api/devices/\(encoded)/identify",
            body: EmptyBody()
        )
    }

    /// Deep-reset device hardware: stop effects, clear firmware state,
    /// blank all zones, and power off.
    ///
    /// - Parameter deviceId: Device identifier (label, MAC, or IP).
    /// - Throws: ``APIError`` on failure.
    func reset(deviceId: String) async throws {
        let encoded = urlEncodeDeviceId(deviceId)
        let _: [String: AnyCodableValue] = try await post(
            "/api/devices/\(encoded)/reset",
            body: EmptyBody()
        )
    }

    /// Set or clear a custom display name for a device.
    ///
    /// - Parameters:
    ///   - deviceId: Device identifier (label, MAC, or IP).
    ///   - nickname: The custom name, or empty string to clear.
    /// - Throws: ``APIError`` on failure.
    func setNickname(deviceId: String, nickname: String) async throws {
        struct NicknameBody: Codable { let nickname: String }
        let encoded = urlEncodeDeviceId(deviceId)
        let _: [String: AnyCodableValue] = try await post(
            "/api/devices/\(encoded)/nickname",
            body: NicknameBody(nickname: nickname)
        )
    }

    // MARK: - Schedule endpoints

    /// Fetch the schedule with resolved times for today.
    ///
    /// - Returns: An array of ``ScheduleEntry``.
    /// - Throws: ``APIError`` on failure.
    func fetchSchedule() async throws -> [ScheduleEntry] {
        let response: ScheduleResponse = try await get("/api/schedule")
        return response.entries
    }

    /// Enable or disable a schedule entry.
    ///
    /// - Parameters:
    ///   - index: The zero-based schedule entry index.
    ///   - enabled: ``true`` to enable, ``false`` to disable.
    /// - Throws: ``APIError`` on failure.
    func setScheduleEnabled(index: Int, enabled: Bool) async throws {
        struct EnabledBody: Codable { let enabled: Bool }
        let _: [String: AnyCodableValue] = try await post(
            "/api/schedule/\(index)/enabled",
            body: EnabledBody(enabled: enabled)
        )
    }

    // MARK: - SSE streaming

    /// Build a URL request for the SSE color stream endpoint.
    ///
    /// The caller is responsible for creating a ``URLSession`` data
    /// task with a streaming delegate to consume the response.
    ///
    /// - Parameter deviceId: Device identifier (label, MAC, or IP).
    /// - Returns: A configured ``URLRequest`` for SSE streaming.
    func sseRequest(deviceId: String) -> URLRequest? {
        let encoded = urlEncodeDeviceId(deviceId)
        guard let url = buildURL("/api/devices/\(encoded)/colors/stream") else {
            return nil
        }
        var request = URLRequest(url: url)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        // Long timeout for streaming connections.
        request.timeoutInterval = 300
        return request
    }

    // MARK: - Internal helpers

    /// Build a full URL from a path.
    private func buildURL(_ path: String) -> URL? {
        // Strip trailing slash from base URL to avoid double slashes.
        let base = serverURL.hasSuffix("/")
            ? String(serverURL.dropLast())
            : serverURL
        return URL(string: base + path)
    }

    /// Perform a GET request and decode the JSON response.
    func get<T: Decodable>(_ path: String) async throws -> T {
        guard let url = buildURL(path) else {
            throw APIError.invalidURL
        }
        var request = URLRequest(url: url)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")

        let (data, response) = try await session.data(for: request)
        try validateResponse(response, data: data)
        return try JSONDecoder().decode(T.self, from: data)
    }

    /// Perform a POST request with a Codable body and decode the response.
    func post<B: Encodable, T: Decodable>(
        _ path: String,
        body: B
    ) async throws -> T {
        guard let url = buildURL(path) else {
            throw APIError.invalidURL
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(body)

        let (data, response) = try await session.data(for: request)
        try validateResponse(response, data: data)
        return try JSONDecoder().decode(T.self, from: data)
    }

    /// Perform a POST request with a raw ``[String: Any]`` body.
    ///
    /// Used for the ``play`` endpoint where params contain mixed types
    /// that don't conform to a single ``Codable`` struct.
    private func postRaw<T: Decodable>(
        _ path: String,
        body: [String: Any]
    ) async throws -> T {
        guard let url = buildURL(path) else {
            throw APIError.invalidURL
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(
            withJSONObject: body, options: []
        )

        let (data, response) = try await session.data(for: request)
        try validateResponse(response, data: data)
        return try JSONDecoder().decode(T.self, from: data)
    }

    /// Validate an HTTP response, throwing on error status codes.
    private func validateResponse(
        _ response: URLResponse,
        data: Data
    ) throws {
        guard let httpResponse = response as? HTTPURLResponse else {
            throw APIError.invalidResponse
        }
        guard (200...299).contains(httpResponse.statusCode) else {
            // Try to extract the error message from the JSON body.
            let message: String
            if let errorBody = try? JSONDecoder().decode(
                [String: String].self, from: data
            ),
               let errorMessage = errorBody["error"] {
                message = errorMessage
            } else {
                message = "HTTP \(httpResponse.statusCode)"
            }
            throw APIError.serverError(
                statusCode: httpResponse.statusCode,
                message: message
            )
        }
    }
}

/// Empty JSON body (``{}``) for POST endpoints that need no payload.
struct EmptyBody: Encodable {}

/// Generic ``{"ok": true}`` response from endpoints that return no data.
struct GenericOKResponse: Codable {
    let ok: Bool
}

/// Errors that can occur during API communication.
enum APIError: LocalizedError {
    /// The server URL is malformed.
    case invalidURL

    /// The server returned a non-HTTP response.
    case invalidResponse

    /// The server returned an error status code.
    case serverError(statusCode: Int, message: String)

    var errorDescription: String? {
        switch self {
        case .invalidURL:
            return "Invalid server URL"
        case .invalidResponse:
            return "Invalid response from server"
        case .serverError(let code, let message):
            return "Server error (\(code)): \(message)"
        }
    }
}
