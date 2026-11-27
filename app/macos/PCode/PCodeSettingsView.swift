import AppKit
import SwiftUI

/// Presents the native macOS Pcode Settings window (⌘,).
@MainActor
final class PCodeSettingsController: NSWindowController, NSWindowDelegate {
    static let shared = PCodeSettingsController()

    private init() {
        let hostingController = NSHostingController(rootView: PCodeSettingsView())
        let window = NSWindow(contentViewController: hostingController)
        window.title = "Pcode Settings"
        window.styleMask = [.titled, .closable]
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

struct PCodeSettingsView: View {
    @ObservedObject var typography: PCodeTypography = .shared

    @State private var monoFamilies: [String] = []
    @State private var allFamilies: [String] = []

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            previewCard

            VStack(alignment: .leading, spacing: 12) {
                settingRow("Style") {
                    Picker("", selection: $typography.style) {
                        ForEach(PCodeTypography.Style.allCases) { option in
                            Text(option.label).tag(option)
                        }
                    }
                    .labelsHidden()
                    .pickerStyle(.segmented)
                    .fixedSize()
                }

                settingRow("Code Font") {
                    PCodeFontFamilyPicker(
                        selection: $typography.codeFamily,
                        defaultLabel: typography.isCodeFamilyAvailable
                            ? "JetBrains Mono (Bundled)"
                            : "System Monospaced",
                        primaryFamilies: monoFamilies,
                        allFamilies: allFamilies,
                        supportsMonospaceFilter: true
                    )
                }

                settingRow("Prose Font") {
                    VStack(alignment: .leading, spacing: 4) {
                        PCodeFontFamilyPicker(
                            selection: $typography.proseFamily,
                            defaultLabel: "System (SF Pro)",
                            primaryFamilies: allFamilies,
                            allFamilies: allFamilies,
                            supportsMonospaceFilter: false
                        )
                        .disabled(typography.style == .mono)
                        .opacity(typography.style == .mono ? 0.5 : 1)

                        if typography.style == .mono {
                            Text("Uses Code Font everywhere while in Mono style.")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }

                settingRow("Weight") {
                    Picker("", selection: $typography.weight) {
                        ForEach(PCodeTypography.Weight.allCases) { option in
                            Text(option.label).tag(option)
                        }
                    }
                    .labelsHidden()
                    .pickerStyle(.segmented)
                    .fixedSize()
                }

                settingRow("Size") {
                    HStack(spacing: 10) {
                        Slider(
                            value: $typography.size,
                            in: PCodeTypography.minSize...PCodeTypography.maxSize,
                            step: 1
                        )
                        .frame(maxWidth: 180)

                        Text("\(Int(typography.size.rounded())) pt")
                            .font(.callout.monospacedDigit())
                            .foregroundStyle(.secondary)
                            .frame(width: 42, alignment: .trailing)

                        Text("⌘+ / ⌘- / ⌘0")
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                    }
                }
            }

            Divider()

            HStack {
                Button("Reset to Defaults") {
                    typography.reset()
                }
                .disabled(isAtDefaults)

                Spacer()

                Button("Open Ghostty Config…") {
                    if let delegate = NSApp.delegate as? AppDelegate {
                        delegate.ghostty.openConfig()
                    }
                }
            }
        }
        .padding(20)
        .frame(width: 480)
        .onAppear {
            if allFamilies.isEmpty {
                allFamilies = PCodeTypography.availableSystemFontFamilies()
            }
            if monoFamilies.isEmpty {
                monoFamilies = PCodeTypography.availableMonospaceFontFamilies()
            }
        }
    }

    private var isAtDefaults: Bool {
        typography.style == .sansAndMono
            && typography.size == PCodeTypography.defaultSize
            && typography.weight == .medium
            && typography.proseFamily == nil
            && typography.codeFamily == nil
    }

    private var previewCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Text("Preview")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                Spacer()
                Text("Applies to scrollback, popups, and editor")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
            }

            VStack(alignment: .leading, spacing: 6) {
                (Text("Updated ")
                    .font(typography.proseFont())
                    + Text("PCodeTypography")
                    .font(typography.codeFont(scale: typography.inlineCodeScale))
                    + Text(" to keep scrollback and popups consistent.")
                    .font(typography.proseFont()))
                    .lineLimit(2)

                Text("let font = typography.codeNSFont(scale: 0.92)")
                    .font(typography.codeFont(scale: typography.codeBlockScale))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 5)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(
                        RoundedRectangle(cornerRadius: 6, style: .continuous)
                            .fill(Color.primary.opacity(0.05))
                    )
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .fill(Color(nsColor: .textBackgroundColor))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .strokeBorder(Color.primary.opacity(0.10), lineWidth: 1)
            )
        }
    }

    private func settingRow<Content: View>(
        _ label: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(label)
                .font(.callout)
                .foregroundStyle(.secondary)
                .frame(width: 82, alignment: .trailing)
            content()
            Spacer(minLength: 0)
        }
    }
}

/// Searchable system font family picker presented in a popover.
private struct PCodeFontFamilyPicker: View {
    @Binding var selection: String?
    let defaultLabel: String
    let primaryFamilies: [String]
    let allFamilies: [String]
    let supportsMonospaceFilter: Bool

    @State private var isPresented = false
    @State private var query = ""
    @State private var showAllFonts = false

    private var activeFamilies: [String] {
        let source = (supportsMonospaceFilter && !showAllFonts) ? primaryFamilies : allFamilies
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return source }
        return source.filter { $0.localizedCaseInsensitiveContains(trimmed) }
    }

    private var buttonTitle: String {
        selection ?? defaultLabel
    }

    var body: some View {
        Button {
            isPresented.toggle()
        } label: {
            HStack(spacing: 6) {
                Text(buttonTitle)
                    .lineLimit(1)
                    .truncationMode(.tail)
                Spacer(minLength: 4)
                Image(systemName: "chevron.up.chevron.down")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            .frame(width: 240, alignment: .leading)
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .background(
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .fill(Color(nsColor: .controlBackgroundColor))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .strokeBorder(Color.primary.opacity(0.14), lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
        .popover(isPresented: $isPresented, arrowEdge: .bottom) {
            VStack(spacing: 0) {
                HStack(spacing: 6) {
                    Image(systemName: "magnifyingglass")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    TextField("Search fonts…", text: $query)
                        .textFieldStyle(.plain)
                        .font(.callout)
                    if !query.isEmpty {
                        Button {
                            query = ""
                        } label: {
                            Image(systemName: "xmark.circle.fill")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 8)

                if supportsMonospaceFilter {
                    HStack {
                        Toggle("Show all system fonts", isOn: $showAllFonts)
                            .toggleStyle(.checkbox)
                            .font(.caption)
                        Spacer()
                    }
                    .padding(.horizontal, 10)
                    .padding(.bottom, 6)
                }

                Divider()

                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 2) {
                        if query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                            fontRow(label: defaultLabel, family: nil, isSelected: selection == nil)
                            Divider().padding(.vertical, 3)
                        }

                        if activeFamilies.isEmpty {
                            Text("No matching fonts")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .padding(.horizontal, 10)
                                .padding(.vertical, 12)
                        } else {
                            ForEach(activeFamilies, id: \.self) { family in
                                fontRow(
                                    label: family,
                                    family: family,
                                    isSelected: selection == family
                                )
                            }
                        }
                    }
                    .padding(6)
                }
                .frame(height: 260)
            }
            .frame(width: 280)
        }
    }

    private func fontRow(label: String, family: String?, isSelected: Bool) -> some View {
        Button {
            selection = family
            isPresented = false
        } label: {
            HStack(spacing: 8) {
                Image(systemName: "checkmark")
                    .font(.caption.weight(.semibold))
                    .opacity(isSelected ? 1 : 0)
                    .frame(width: 12)

                Text(label)
                    .font(previewFont(for: family))
                    .lineLimit(1)
                    .truncationMode(.tail)

                Spacer(minLength: 0)
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 5, style: .continuous)
                    .fill(isSelected ? Color.accentColor.opacity(0.16) : Color.clear)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func previewFont(for family: String?) -> Font {
        guard let family else { return .callout }
        if let nsFont = PCodeTypographySnapshot.resolveFamilyNSFont(
            family: family, size: 13, weight: .regular, italic: false)
        {
            return Font(nsFont as CTFont)
        }
        return .callout
    }
}
