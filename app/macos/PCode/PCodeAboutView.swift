import AppKit
import SwiftUI

/// Presents the native macOS About Pcode window.
@MainActor
final class PCodeAboutController: NSWindowController, NSWindowDelegate {
    static let shared = PCodeAboutController()

    private init() {
        let hostingController = NSHostingController(rootView: PCodeAboutView())
        let window = NSWindow(contentViewController: hostingController)
        window.title = "About Pcode"
        window.styleMask = [.titled, .closable, .fullSizeContentView]
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = true
        window.isReleasedWhenClosed = false
        window.isExcludedFromWindowsMenu = true
        window.isRestorable = false
        window.standardWindowButton(.miniaturizeButton)?.isHidden = true
        window.standardWindowButton(.zoomButton)?.isHidden = true
        window.setContentSize(hostingController.view.fittingSize)
        window.center()

        super.init(window: window)
        window.delegate = self
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) is not supported")
    }

    func show() {
        guard let window else { return }
        if !window.isVisible {
            window.center()
        }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func hide() {
        window?.close()
    }

    // MARK: - First Responder

    @IBAction func close(_ sender: Any?) {
        window?.performClose(sender)
    }

    @IBAction func closeWindow(_ sender: Any?) {
        window?.performClose(sender)
    }

    override func cancelOperation(_ sender: Any?) {
        hide()
    }

    @objc func cancel(_ sender: Any?) {
        hide()
    }
}

struct PCodeAboutView: View {
    @Environment(\.openURL) private var openURL

    private static let docsURL = URL(string: "https://aweis89.github.io/pcode/")
    private static let githubURL = URL(string: "https://github.com/aweis89/pcode")

    private var version: String? {
        guard let value = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String,
              !value.isEmpty
        else { return nil }
        return value
    }

    private var build: String? {
        guard let value = Bundle.main.infoDictionary?["CFBundleVersion"] as? String,
              !value.isEmpty
        else { return nil }
        return value
    }

    var body: some View {
        VStack(alignment: .center, spacing: 20) {
            if let icon = NSApp?.applicationIconImage {
                Image(nsImage: icon)
                    .resizable()
                    .interpolation(.high)
                    .scaledToFit()
                    .frame(width: 96, height: 96)
            }

            VStack(alignment: .center, spacing: 8) {
                Text("Pcode")
                    .font(.title.bold())

                Text("A terminal-native coding agent built to run many agents in parallel.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .textSelection(.enabled)

            if version != nil || build != nil {
                VStack(spacing: 4) {
                    if let version {
                        PropertyRow(label: "Version", value: version)
                    }
                    if let build, build != version {
                        PropertyRow(label: "Build", value: build)
                    }
                }
                .frame(maxWidth: .infinity)
            }

            HStack(spacing: 8) {
                if let url = Self.docsURL {
                    Button("Docs") {
                        openURL(url)
                    }
                }
                if let url = Self.githubURL {
                    Button("GitHub") {
                        openURL(url)
                    }
                }
            }
        }
        .padding(.top, 12)
        .padding(.horizontal, 32)
        .padding(.bottom, 28)
        .frame(width: 300)
        .background(VisualEffectBackground(material: .underWindowBackground).ignoresSafeArea())
    }

    private struct PropertyRow: View {
        let label: String
        let value: String

        var body: some View {
            HStack(spacing: 8) {
                Text(label)
                    .frame(width: 84, alignment: .trailing)
                Text(value)
                    .frame(width: 120, alignment: .leading)
                    .foregroundStyle(.secondary)
                    .monospaced()
            }
            .font(.callout)
            .textSelection(.enabled)
        }
    }

    private struct VisualEffectBackground: NSViewRepresentable {
        let material: NSVisualEffectView.Material
        var blendingMode: NSVisualEffectView.BlendingMode = .behindWindow
        var isEmphasized: Bool = false

        func makeNSView(context: Context) -> NSVisualEffectView {
            let view = NSVisualEffectView()
            view.autoresizingMask = [.width, .height]
            view.material = material
            view.blendingMode = blendingMode
            view.isEmphasized = isEmphasized
            return view
        }

        func updateNSView(_ nsView: NSVisualEffectView, context: Context) {
            nsView.material = material
            nsView.blendingMode = blendingMode
            nsView.isEmphasized = isEmphasized
        }
    }
}
