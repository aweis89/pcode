import AppKit
import SwiftUI

/// Native 3D gyroscope boot overlay shown over a pcode pane until pcode
/// finishes starting up and paints its first ready frame.
///
/// Mirrors pcode's `gyro` smooth spinner (`kitty_spinner._render_gyro`) at
/// full Retina vector resolution so startup has a crisp 120fps centerpiece
/// with zero artificial delay: the overlay fades out the instant `isReady`
/// flips to true, and stops its `TimelineView` once dismissed.
struct PCodeStartupOverlay: View {
    @EnvironmentObject var ghostty: Ghostty.App
    @Environment(\.colorScheme) private var colorScheme

    let isReady: Bool
    let label: String?
    let resuming: Bool
    /// For a resumed session, the editor card's height: the overlay is then only
    /// a card-shaped placeholder where that card will be, so the saved transcript
    /// above it shows at once and the real card fades in under the same outline.
    let compactCardHeight: CGFloat?

    @State private var appeared = false
    @State private var hiding = false
    @State private var dismissed: Bool

    private static let fadeInDuration: Double = 0.16
    private static let fadeOutDuration: Double = 0.20

    init(isReady: Bool, label: String?, resuming: Bool, compactCardHeight: CGFloat? = nil) {
        self.isReady = isReady
        self.label = label
        self.resuming = resuming
        self.compactCardHeight = compactCardHeight
        // Start already dismissed for panes that are live when mounted (e.g.
        // pane switches and Live Peek) so there is zero 1-frame flash.
        _dismissed = State(initialValue: isReady)
    }

    var body: some View {
        if !dismissed {
            content
            .opacity(hiding ? 0 : 1)
            .allowsHitTesting(false)
            .onAppear {
                if isReady {
                    dismissed = true
                    return
                }
                withAnimation(.easeOut(duration: Self.fadeInDuration)) {
                    appeared = true
                }
            }
            .onChange(of: isReady) { ready in
                guard ready, !hiding else { return }
                withAnimation(.easeOut(duration: Self.fadeOutDuration)) {
                    hiding = true
                }
                DispatchQueue.main.asyncAfter(deadline: .now() + Self.fadeOutDuration + 0.04) {
                    if hiding { dismissed = true }
                }
            }
        }
    }

    @ViewBuilder private var content: some View {
        if let compactCardHeight {
            compactCard(height: compactCardHeight)
        } else {
            fullCover
        }
    }

    /// Drawn like `PCodeDockCard` (same margins, corners and shadow), so the swap is a cross-fade.
    private func compactCard(height: CGFloat) -> some View {
        VStack(spacing: 0) {
            Spacer(minLength: 0)
            HStack(spacing: 12) {
                PCodeGyroSpinner(
                    primary: primaryColor,
                    secondary: secondaryColor,
                    isLight: colorScheme == .light
                )
                .frame(width: 26, height: 26)
                if let caption {
                    Text(caption)
                        .font(.system(size: 12, weight: .regular, design: .monospaced))
                        .foregroundStyle(ghostty.config.foregroundColor.opacity(0.48))
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 14)
            .frame(maxWidth: PCodeDock.maxCardWidth)
            .frame(height: height)
            .opacity(appeared ? 1 : 0)
            .background(
                RoundedRectangle(cornerRadius: 12)
                    .fill(ghostty.config.backgroundColor)
                    .shadow(color: .black.opacity(0.28), radius: 18, x: 0, y: -2)
            )
            .padding(.horizontal, PCodeDock.bottomMargin)
            .padding(.top, PCodeDock.topMargin)
            .padding(.bottom, PCodeDock.bottomMargin)
        }
        .frame(maxWidth: .infinity)
    }

    private var fullCover: some View {
            ZStack {
                ghostty.config.backgroundColor

                VStack(spacing: 18) {
                    PCodeGyroSpinner(
                        primary: primaryColor,
                        secondary: secondaryColor,
                        isLight: colorScheme == .light
                    )
                    .frame(width: 92, height: 92)
                    .scaleEffect(hiding ? 0.94 : (appeared ? 1.0 : 0.90))

                    if let caption {
                        Text(caption)
                            .font(.system(size: 12, weight: .regular, design: .monospaced))
                            .foregroundStyle(ghostty.config.foregroundColor.opacity(0.48))
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                }
                .opacity(hiding ? 0 : (appeared ? 1 : 0))
            }
    }

    private var caption: String? {
        guard let label, !label.isEmpty else { return nil }
        return resuming ? "Resuming \(label)" : label
    }

    private var primaryColor: Color {
        if let palette = ghostty.config.palette([6, 4, 14, 12]),
           let c = palette[6] ?? palette[4] ?? palette[14] ?? palette[12] {
            return c
        }
        return .accentColor
    }

    private var secondaryColor: Color {
        if let palette = ghostty.config.palette([5, 13, 4, 12]),
           let c = palette[5] ?? palette[13] ?? palette[4] ?? palette[12] {
            return c
        }
        return ghostty.config.foregroundColor.opacity(0.8)
    }
}

/// 3D counter-rotating gyroscope rings with orbiting beads and a pulsing core,
/// Z-sorted per frame. Matches `kitty_spinner._render_gyro` in `pcode`.
private struct PCodeGyroSpinner: View {
    let primary: Color
    let secondary: Color
    let isLight: Bool
    @Environment(\.pcodeWindowOnScreen) private var onScreen

    private static let period: Double = 2.8
    private static let ringRadius: Double = 0.78
    private static let steps: Int = 44

    private struct RGB {
        let r: Double
        let g: Double
        let b: Double

        init(_ color: Color, isLight: Bool) {
            let appearance = NSAppearance(named: isLight ? .aqua : .darkAqua)
            var resolved: NSColor?
            if let appearance {
                appearance.performAsCurrentDrawingAppearance {
                    resolved = NSColor(color).usingColorSpace(.sRGB)
                }
            } else {
                resolved = NSColor(color).usingColorSpace(.sRGB)
            }
            if let srgb = resolved ?? NSColor.controlAccentColor.usingColorSpace(.sRGB) {
                self.r = Double(srgb.redComponent)
                self.g = Double(srgb.greenComponent)
                self.b = Double(srgb.blueComponent)
            } else {
                self.r = 0.35
                self.g = 0.65
                self.b = 0.95
            }
        }

        init(r: Double, g: Double, b: Double) {
            self.r = r
            self.g = g
            self.b = b
        }

        func mixed(with other: RGB, t: Double) -> RGB {
            RGB(
                r: r + (other.r - r) * t,
                g: g + (other.g - g) * t,
                b: b + (other.b - b) * t
            )
        }

        func lit(_ amount: Double, isLight: Bool) -> RGB {
            let target = isLight ? RGB(r: 0.08, g: 0.08, b: 0.12) : RGB(r: 1.0, g: 1.0, b: 1.0)
            let k = max(0.0, min(1.0, isLight ? amount * 0.45 : amount))
            return mixed(with: target, t: k)
        }

        func color(opacity: Double = 1.0) -> Color {
            Color(.sRGB, red: r, green: g, blue: b, opacity: opacity)
        }
    }

    private enum Primitive {
        case segment(ax: Double, ay: Double, bx: Double, by: Double, width: Double, color: Color)
        case disc(x: Double, y: Double, radius: Double, rgb: RGB, alpha: Double, specular: Double, glow: Double)
    }

    var body: some View {
        let c1 = RGB(primary, isLight: isLight)
        let c2 = RGB(secondary, isLight: isLight)

        // Paused while the window is hidden or covered: nobody sees it.
        TimelineView(.animation(minimumInterval: nil, paused: !onScreen)) { context in
            let t = context.date.timeIntervalSinceReferenceDate
            let ang = (t.truncatingRemainder(dividingBy: Self.period) / Self.period) * (2.0 * .pi)

            Canvas { gc, size in
                let scale = Double(min(size.width, size.height)) * 0.5
                let cx = Double(size.width) * 0.5
                let cy = Double(size.height) * 0.5

                var prims: [(z: Double, prim: Primitive)] = []
                prims.reserveCapacity(Self.steps * 2 + 3)

                // Pulsing center core.
                let pulse = 0.5 + 0.5 * sin(ang * 2.0)
                let coreRGB = c1.mixed(with: c2, t: 0.5).lit(0.28 + 0.22 * pulse, isLight: isLight)
                let coreR = (0.165 + 0.045 * pulse) * scale
                prims.append((
                    0.0,
                    .disc(x: cx, y: cy, radius: coreR, rgb: coreRGB, alpha: 0.88, specular: 0.45, glow: 0.30)
                ))

                let rings: [(RGB, Double, Double, Double, Double)] = [
                    (c1, ang, 0.92, 0.38, ang * 2.0),
                    (c2, -ang + 0.8, 1.02, -0.58, -ang * 2.0 + 1.5),
                ]

                let r = Self.ringRadius
                for (col, yaw, tiltX, roll, beadAng) in rings {
                    let cyw = cos(yaw), syw = sin(yaw)
                    let ctx = cos(tiltX), stx = sin(tiltX)
                    let crl = cos(roll), srl = sin(roll)

                    func project(_ theta: Double) -> (x: Double, y: Double, z: Double) {
                        let x0 = r * cos(theta)
                        let y0 = r * sin(theta)
                        let y1 = y0 * ctx
                        let z1 = y0 * stx
                        let x2 = x0 * cyw + z1 * syw
                        let z2 = -x0 * syw + z1 * cyw
                        let x3 = x2 * crl - y1 * srl
                        let y3 = x2 * srl + y1 * crl
                        return (cx + x3 * scale, cy + y3 * scale, z2 / r)
                    }

                    var prev = project(0)
                    for i in 1...Self.steps {
                        let next = project((Double(i) / Double(Self.steps)) * 2.0 * .pi)
                        let zm = 0.5 * (prev.z + next.z)
                        let depth = 0.5 + 0.5 * zm
                        let width = (0.040 + 0.042 * depth) * scale
                        let alpha = 0.20 + 0.76 * pow(depth, 1.25)
                        prims.append((
                            zm,
                            .segment(
                                ax: prev.x, ay: prev.y,
                                bx: next.x, by: next.y,
                                width: width,
                                color: col.color(opacity: alpha)
                            )
                        ))
                        prev = next
                    }

                    let bead = project(beadAng)
                    let bdepth = 0.5 + 0.5 * bead.z
                    let bcol = col.lit(0.35 * bdepth, isLight: isLight)
                    let brad = (0.082 + 0.064 * bdepth) * scale
                    prims.append((
                        bead.z + 0.05,
                        .disc(
                            x: bead.x, y: bead.y,
                            radius: brad,
                            rgb: bcol,
                            alpha: 0.38 + 0.62 * bdepth,
                            specular: 0.42,
                            glow: 0.26
                        )
                    ))
                }

                prims.sort { $0.z < $1.z }

                for item in prims {
                    switch item.prim {
                    case let .segment(ax, ay, bx, by, width, color):
                        var path = Path()
                        path.move(to: CGPoint(x: ax, y: ay))
                        path.addLine(to: CGPoint(x: bx, y: by))
                        gc.stroke(
                            path,
                            with: .color(color),
                            style: StrokeStyle(lineWidth: width, lineCap: .round)
                        )
                    case let .disc(x, y, radius, rgb, alpha, specular, glow):
                        if glow > 0 {
                            let glowR = radius * 2.2
                            let glowRect = CGRect(x: x - glowR, y: y - glowR, width: glowR * 2, height: glowR * 2)
                            let grad = Gradient(stops: [
                                .init(color: rgb.color(opacity: alpha * glow), location: 0),
                                .init(color: rgb.color(opacity: 0), location: 1),
                            ])
                            gc.fill(
                                Path(ellipseIn: glowRect),
                                with: .radialGradient(
                                    grad,
                                    center: CGPoint(x: x, y: y),
                                    startRadius: radius * 0.4,
                                    endRadius: glowR
                                )
                            )
                        }

                        let rect = CGRect(x: x - radius, y: y - radius, width: radius * 2, height: radius * 2)
                        gc.fill(Path(ellipseIn: rect), with: .color(rgb.color(opacity: alpha)))

                        if specular > 0 && !isLight {
                            let specR = radius * 0.42
                            let sx = x - radius * 0.28
                            let sy = y - radius * 0.28
                            let specRect = CGRect(x: sx - specR, y: sy - specR, width: specR * 2, height: specR * 2)
                            gc.fill(
                                Path(ellipseIn: specRect),
                                with: .color(Color.white.opacity(alpha * specular))
                            )
                        }
                    }
                }
            }
        }
    }
}
