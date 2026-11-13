import SwiftUI

/// The same tokens as src/loupe/web/theme.css, so the native screens match the web app.
enum Theme {
    static let paper = dynamic(light: 0xF3EFE6, dark: 0x12110F)
    static let surface = dynamic(light: 0xFBF9F4, dark: 0x1B1916)
    static let ink = dynamic(light: 0x1D1B17, dark: 0xEEE8DC)
    static let muted = dynamic(light: 0x6F685C, dark: 0xA0978A)
    static let line = dynamic(light: 0xDDD5C5, dark: 0x2B2823)
    static let accent = dynamic(light: 0xA8681A, dark: 0xD9A04A)
    static let accentSoft = dynamic(light: 0xF1E2C8, dark: 0x3A2C17)
    static let accentInk = dynamic(light: 0xFFFAF0, dark: 0x1A1206)
    static let warn = dynamic(light: 0x9A5B12, dark: 0xE0A45A)

    static let space1: CGFloat = 4
    static let space2: CGFloat = 8
    static let space3: CGFloat = 12
    static let space4: CGFloat = 16
    static let space5: CGFloat = 24
    static let space6: CGFloat = 32
    static let radiusM: CGFloat = 10
    static let formWidth: CGFloat = 420

    static func display(_ style: Font.TextStyle) -> Font { .system(style, design: .serif) }

    private static func dynamic(light: UInt32, dark: UInt32) -> Color {
        Color(UIColor { traits in rgb(traits.userInterfaceStyle == .dark ? dark : light) })
    }

    private static func rgb(_ value: UInt32) -> UIColor {
        UIColor(red: CGFloat((value >> 16) & 0xFF) / 255,
                green: CGFloat((value >> 8) & 0xFF) / 255,
                blue: CGFloat(value & 0xFF) / 255, alpha: 1)
    }
}

struct LoupeMark: View {
    var body: some View {
        HStack(spacing: Theme.space2) {
            Circle()
                .strokeBorder(Theme.accent, lineWidth: 2)
                .background(Circle().inset(by: 5).fill(Theme.accentSoft))
                .frame(width: 16, height: 16)
                .accessibilityHidden(true)
            Text("Loupe").font(Theme.display(.title3))
        }
        .foregroundStyle(Theme.ink)
    }
}

struct PrimaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.body.weight(.medium))
            .frame(maxWidth: .infinity, minHeight: 44)
            .foregroundStyle(Theme.accentInk)
            .background(Theme.accent.opacity(configuration.isPressed ? 0.8 : 1),
                        in: RoundedRectangle(cornerRadius: Theme.radiusM))
    }
}

struct SecondaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.body.weight(.medium))
            .frame(maxWidth: .infinity, minHeight: 44)
            .foregroundStyle(Theme.ink)
            .background(Theme.surface.opacity(configuration.isPressed ? 0.7 : 1),
                        in: RoundedRectangle(cornerRadius: Theme.radiusM))
            .overlay(RoundedRectangle(cornerRadius: Theme.radiusM).strokeBorder(Theme.line))
    }
}
