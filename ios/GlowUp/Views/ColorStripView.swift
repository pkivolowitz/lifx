// ColorStripView.swift
// GlowUp — LIFX Remote Control
//
// Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import SwiftUI

/// Horizontal color strip visualizing LIFX zone colors.
///
/// Each zone is rendered as a vertical slice of the strip, using the
/// HSBK-to-SwiftUI-Color conversion.  The strip fills its available
/// width with a fixed height, giving a ribbon-like appearance similar
/// to the Python tkinter simulator.
struct ColorStripView: View {
    /// Zone colors to visualize.
    let zones: [ZoneColor]

    /// Height of the color strip in points.
    private let stripHeight: CGFloat = 50

    /// Corner radius for the strip container.
    private let cornerRadius: CGFloat = 10

    var body: some View {
        GeometryReader { geometry in
            if zones.isEmpty {
                // No data — show a placeholder.
                RoundedRectangle(cornerRadius: cornerRadius)
                    .fill(Color.gray.opacity(0.2))
                    .overlay(
                        Text("No color data")
                            .foregroundStyle(.secondary)
                            .font(.caption)
                    )
            } else {
                // Render each zone as a colored rectangle slice.
                HStack(spacing: 0) {
                    ForEach(
                        Array(zones.enumerated()),
                        id: \.offset
                    ) { _, zone in
                        Rectangle()
                            .fill(hsbkToColor(zone))
                    }
                }
                .clipShape(RoundedRectangle(cornerRadius: cornerRadius))
            }
        }
        .frame(height: stripHeight)
    }
}
