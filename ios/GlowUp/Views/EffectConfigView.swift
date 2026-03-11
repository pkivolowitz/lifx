// EffectConfigView.swift
// GlowUp — LIFX Remote Control
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import SwiftUI

/// Auto-generated parameter configuration screen for an effect.
///
/// Dynamically builds UI controls from the effect's ``Param`` metadata:
/// - Numeric params with min/max: ``Slider`` with value label.
/// - Params with choices: ``Picker`` (segmented style for few options).
/// - String params without choices: ``TextField``.
///
/// A "Play" button at the bottom sends the configured parameters
/// to the server and pops back to the device detail view.
struct EffectConfigView: View {
    @EnvironmentObject var apiClient: APIClient
    @Environment(\.dismiss) private var dismiss

    /// The target device.
    let device: Device

    /// The selected effect with its parameter metadata.
    let effect: Effect

    /// Current parameter values, initialized from defaults.
    @State private var paramValues: [String: ParamValue] = [:]

    /// Whether the play request is in progress.
    @State private var isPlaying: Bool = false

    /// Error message for display.
    @State private var errorMessage: String?

    var body: some View {
        Form {
            // Effect description section.
            if !effect.description.isEmpty {
                Section {
                    Text(effect.description)
                        .foregroundStyle(.secondary)
                }
            }

            // Parameter controls section.
            if !effect.params.isEmpty {
                Section {
                    ForEach(sortedParamNames, id: \.self) { name in
                        let param = effect.params[name]!
                        paramControl(name: name, param: param)
                    }
                } header: {
                    Text("Parameters")
                }
            }

            // Play button section.
            Section {
                Button {
                    Task { await playEffect() }
                } label: {
                    HStack {
                        Spacer()
                        if isPlaying {
                            ProgressView()
                                .padding(.trailing, 8)
                        }
                        Label("Play", systemImage: "play.fill")
                            .font(.headline)
                        Spacer()
                    }
                }
                .disabled(isPlaying)
            }
        }
        .navigationTitle(effect.name)
        .onAppear {
            initializeParams()
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

    /// Parameter names sorted alphabetically for stable display.
    private var sortedParamNames: [String] {
        effect.params.keys.sorted()
    }

    /// Build the appropriate control for a parameter.
    @ViewBuilder
    private func paramControl(name: String, param: EffectParam) -> some View {
        // Choice parameters: picker.
        if let choices = param.choices, !choices.isEmpty {
            choicePicker(name: name, choices: choices)
        }
        // Numeric parameters with min/max: slider.
        else if param.type == "float" || param.type == "int",
                let minVal = param.min?.doubleValue,
                let maxVal = param.max?.doubleValue {
            numericSlider(
                name: name,
                param: param,
                minVal: minVal,
                maxVal: maxVal
            )
        }
        // String parameters: text field.
        else if param.type == "str" {
            stringField(name: name, param: param)
        }
        // Fallback: display as text.
        else {
            LabeledContent(name, value: param.`default`.description)
        }
    }

    /// Maximum number of choices before switching from segmented to menu style.
    private let segmentedThreshold = 5

    /// A picker for choice-based parameters.
    ///
    /// Uses segmented style for small choice sets (e.g., direction: left/right)
    /// and menu (dropdown) style for larger ones (e.g., palette presets).
    @ViewBuilder
    private func choicePicker(
        name: String,
        choices: [String]
    ) -> some View {
        let binding = Binding<String>(
            get: { paramValues[name]?.stringValue ?? choices[0] },
            set: { paramValues[name] = .string($0) }
        )
        let picker = Picker(name, selection: binding) {
            ForEach(choices, id: \.self) { choice in
                Text(choice).tag(choice)
            }
        }
        if choices.count <= segmentedThreshold {
            picker.pickerStyle(.segmented)
        } else {
            picker.pickerStyle(.menu)
        }
    }

    /// A slider for numeric parameters with min/max bounds.
    private func numericSlider(
        name: String,
        param: EffectParam,
        minVal: Double,
        maxVal: Double
    ) -> some View {
        let isInt = param.type == "int"
        let binding = Binding<Double>(
            get: { paramValues[name]?.doubleValue ?? param.`default`.doubleValue ?? minVal },
            set: { paramValues[name] = isInt ? .int(Int($0)) : .double($0) }
        )
        return VStack(alignment: .leading, spacing: 4) {
            // Parameter name, description, and current value.
            HStack {
                Text(name)
                    .font(.subheadline)
                Spacer()
                Text(isInt
                     ? "\(Int(binding.wrappedValue))"
                     : String(format: "%.1f", binding.wrappedValue))
                .font(.subheadline)
                .foregroundStyle(.secondary)
            }
            if !param.description.isEmpty {
                Text(param.description)
                    .font(.caption)
                    .foregroundStyle(.tertiary)
            }
            Slider(
                value: binding,
                in: minVal...maxVal,
                step: isInt ? 1.0 : (maxVal - minVal) / 100.0
            )
        }
    }

    /// A text field for string parameters without choices.
    private func stringField(
        name: String,
        param: EffectParam
    ) -> some View {
        let binding = Binding<String>(
            get: { paramValues[name]?.stringValue ?? "" },
            set: { paramValues[name] = .string($0) }
        )
        return VStack(alignment: .leading, spacing: 4) {
            Text(name)
                .font(.subheadline)
            if !param.description.isEmpty {
                Text(param.description)
                    .font(.caption)
                    .foregroundStyle(.tertiary)
            }
            TextField(name, text: binding)
                .textFieldStyle(.roundedBorder)
        }
    }

    /// UserDefaults key for persisted parameter values, scoped per effect.
    private var savedParamsKey: String {
        "effect_params_\(effect.name)"
    }

    /// Initialize parameter values from saved state, falling back to
    /// server-reported defaults for any parameter not previously saved.
    private func initializeParams() {
        let saved = UserDefaults.standard.dictionary(forKey: savedParamsKey) ?? [:]

        for (name, param) in effect.params {
            // Restore saved value if present and type-compatible.
            if let savedVal = saved[name] {
                if let v = savedVal as? Int, param.type == "int" {
                    paramValues[name] = .int(v)
                    continue
                }
                if let v = savedVal as? Double {
                    paramValues[name] = param.type == "int"
                        ? .int(Int(v))
                        : .double(v)
                    continue
                }
                if let v = savedVal as? String {
                    paramValues[name] = .string(v)
                    continue
                }
            }

            // Fall back to server default.
            switch param.`default` {
            case .int(let v):
                paramValues[name] = .int(v)
            case .double(let v):
                paramValues[name] = .double(v)
            case .string(let v):
                paramValues[name] = .string(v)
            case .null:
                break
            }
        }
    }

    /// Persist current parameter values to UserDefaults.
    private func saveParams() {
        var dict: [String: Any] = [:]
        for (name, value) in paramValues {
            switch value {
            case .int(let v): dict[name] = v
            case .double(let v): dict[name] = v
            case .string(let v): dict[name] = v
            }
        }
        UserDefaults.standard.set(dict, forKey: savedParamsKey)
    }

    /// Send the play command to the server with configured parameters.
    private func playEffect() async {
        isPlaying = true
        errorMessage = nil

        // Persist the user's parameter choices for next time.
        saveParams()

        // Build the params dict for the API call.
        var apiParams: [String: Any] = [:]
        for (name, value) in paramValues {
            switch value {
            case .int(let v): apiParams[name] = v
            case .double(let v): apiParams[name] = v
            case .string(let v): apiParams[name] = v
            }
        }

        do {
            _ = try await apiClient.play(
                ip: device.ip,
                effectName: effect.name,
                params: apiParams
            )
            // Success — pop back to device detail.
            dismiss()
        } catch {
            errorMessage = error.localizedDescription
        }
        isPlaying = false
    }
}

/// Type-safe wrapper for parameter values of different types.
///
/// Used internally by ``EffectConfigView`` to track the user's
/// current slider/picker/text values before sending to the API.
enum ParamValue {
    case int(Int)
    case double(Double)
    case string(String)

    /// The underlying value as a ``Double``, if numeric.
    var doubleValue: Double? {
        switch self {
        case .int(let v): return Double(v)
        case .double(let v): return v
        default: return nil
        }
    }

    /// The underlying value as a ``String``, if string.
    var stringValue: String? {
        switch self {
        case .string(let v): return v
        default: return nil
        }
    }
}
