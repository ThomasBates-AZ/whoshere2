import SwiftUI

extension Color {
    init(hex: UInt32) {
        self.init(red: Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255)
    }
}

/// Who's Here colors, brightened for the watch's black background.
enum Theme {
    static let tint = Color(hex: 0xF07082)       // the board's maroon, lightened to read on black
    static let red = Color(hex: 0xFF6961)
    static let redBackground = Color(hex: 0x3A1210)
    static let green = Color(hex: 0x30D158)
    static let greenBackground = Color(hex: 0x0E2E18)
    static let amber = Color(hex: 0xFF9F0A)
    static let amberBackground = Color(hex: 0x33240A)
    static let card = Color(hex: 0x1C1C1E)
    static let secondary = Color(hex: 0xA1A1A6)

    /// Complications refresh about every 15 minutes (and right away when a notification arrives),
    /// so they allow an older answer than the app before switching to "Can't confirm".
    static let complicationMaxAge: TimeInterval = 30 * 60

    static func color(for kind: DisplayState.Kind) -> Color {
        switch kind {
        case .clear: return green
        case .occupied: return red
        case .cantConfirm: return amber
        case .notSetUp, .loading: return tint
        }
    }

    static func background(for kind: DisplayState.Kind) -> Color {
        switch kind {
        case .clear: return greenBackground
        case .occupied: return redBackground
        case .cantConfirm: return amberBackground
        case .notSetUp, .loading: return card
        }
    }

    static func symbol(for kind: DisplayState.Kind) -> String {
        switch kind {
        case .clear: return "checkmark"
        case .occupied: return "person.2.fill"
        case .cantConfirm: return "exclamationmark.triangle.fill"
        case .notSetUp: return "gearshape"
        case .loading: return "ellipsis"
        }
    }

    private static let locationPalette: [Color] = [
        Color(hex: 0x64D2FF), Color(hex: 0xFFD60A), Color(hex: 0xBF8CFF), Color(hex: 0x66D4CF),
        Color(hex: 0xFF9F0A), Color(hex: 0xFF8AA8), Color(hex: 0x7DE38F), Color(hex: 0x8FB4FF),
    ]

    /// Each hotspot always gets the same color (a stable hash of its name, not Swift's per-launch one).
    static func locationColor(_ name: String?) -> Color {
        guard let name, !name.isEmpty else { return secondary }
        var hash: UInt32 = 5381
        for byte in name.lowercased().utf8 {
            hash = (hash &* 33) &+ UInt32(byte)
        }
        return locationPalette[Int(hash % UInt32(locationPalette.count))]
    }
}
