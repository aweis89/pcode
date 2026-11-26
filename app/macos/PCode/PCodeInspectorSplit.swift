import AppKit
import Darwin
import Foundation
import SwiftUI

/// Short-lived protocol-2 host RPC helper for control intents (e.g., `stop_jobs`)
/// without calling `attach`, so it never affects `attached`, `_latest`, or `unseen`.
enum PCodeHostControlRPC {
    private static let queue = DispatchQueue(
        label: "com.pcode.host.control-rpc",
        qos: .userInitiated,
        attributes: .concurrent
    )

    static func stopJobs(_ jobIDs: [String]?, sessionID: String?, hostID: String?) {
        queue.async {
            guard let socketPath = PCodeSessionInfo.hostSocketPath(
                forSessionID: sessionID,
                hostID: hostID
            ), !socketPath.isEmpty else { return }
            _ = sendRequest(
                socketPath: socketPath,
                method: "stop_jobs",
                args: [jobIDs as Any]
            )
        }
    }

    static func stopJobs(_ jobIDs: [String]?, socketPath: String) {
        guard !socketPath.isEmpty else { return }
        queue.async {
            _ = sendRequest(
                socketPath: socketPath,
                method: "stop_jobs",
                args: [jobIDs as Any]
            )
        }
    }

    @discardableResult
    static func sendRequest(
        socketPath: String,
        method: String,
        args: [Any],
        kwargs: [String: Any] = [:],
        timeoutSeconds: Double = 2.0
    ) -> Bool {
        let clampedTimeout = max(0.2, timeoutSeconds)
        let deadline = CFAbsoluteTimeGetCurrent() + clampedTimeout

        let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { Darwin.close(fd) }

        var noSigPipe: Int32 = 1
        _ = Darwin.setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &noSigPipe, socklen_t(MemoryLayout<Int32>.size))

        var tv = timeval(
            tv_sec: Int(clampedTimeout),
            tv_usec: Int32((clampedTimeout - floor(clampedTimeout)) * 1_000_000)
        )
        _ = Darwin.setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        _ = Darwin.setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let maxLen = MemoryLayout.size(ofValue: addr.sun_path)
        let bytes = Array(socketPath.utf8)
        guard bytes.count < maxLen else { return false }
        withUnsafeMutableBytes(of: &addr.sun_path) { buf in
            for (idx, byte) in bytes.enumerated() {
                buf[idx] = byte
            }
            buf[bytes.count] = 0
        }

        let connectResult = withUnsafePointer(to: &addr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockPtr in
                Darwin.connect(fd, sockPtr, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard connectResult == 0 else { return false }

        // Verify the peer socket belongs to our effective user.
        var peerUID: uid_t = 0
        var peerGID: gid_t = 0
        guard Darwin.getpeereid(fd, &peerUID, &peerGID) == 0, peerUID == Darwin.geteuid() else {
            return false
        }

        let hello = "{\"type\":\"hello\",\"protocol\":2}\n"
        guard writeAll(fd: fd, data: Data(hello.utf8)) else { return false }

        var readBuffer = Data()
        guard let readyLine = readLine(fd: fd, buffer: &readBuffer, deadline: deadline),
              let readyObj = try? JSONSerialization.jsonObject(with: readyLine) as? [String: Any],
              readyObj["type"] as? String == "ready"
        else {
            return false
        }

        let sanitizedArgs: [Any] = args.map { value in
            if case Optional<Any>.none = value {
                return NSNull()
            }
            return value
        }
        let reqObj: [String: Any] = [
            "type": "request",
            "id": 1,
            "method": method,
            "args": sanitizedArgs,
            "kwargs": kwargs,
        ]
        guard JSONSerialization.isValidJSONObject(reqObj),
              var reqData = try? JSONSerialization.data(withJSONObject: reqObj)
        else { return false }
        reqData.append(0x0A)
        guard writeAll(fd: fd, data: reqData) else { return false }

        // Wait for reply and verify the host accepted the intent.
        guard let replyLine = readLine(fd: fd, buffer: &readBuffer, deadline: deadline),
              let replyObj = try? JSONSerialization.jsonObject(with: replyLine) as? [String: Any],
              replyObj["type"] as? String == "reply",
              (replyObj["id"] as? Int) == 1,
              replyObj["error"] == nil
        else {
            return false
        }
        return true
    }

    private static func writeAll(fd: Int32, data: Data) -> Bool {
        data.withUnsafeBytes { raw in
            guard let base = raw.baseAddress else { return true }
            var sent = 0
            while sent < raw.count {
                let n = Darwin.write(fd, base.advanced(by: sent), raw.count - sent)
                if n <= 0 {
                    if errno == EINTR { continue }
                    return false
                }
                sent += n
            }
            return true
        }
    }

    private static func readLine(
        fd: Int32,
        buffer: inout Data,
        deadline: CFAbsoluteTime,
        maxBytes: Int = 65_536
    ) -> Data? {
        var chunk = [UInt8](repeating: 0, count: 4096)
        while true {
            if let nlIndex = buffer.firstIndex(of: 0x0A) {
                let line = buffer.prefix(upTo: nlIndex)
                buffer.removeSubrange(...nlIndex)
                return Data(line)
            }
            if buffer.count >= maxBytes { return nil }

            let remaining = deadline - CFAbsoluteTimeGetCurrent()
            if remaining <= 0 { return nil }

            var pfd = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
            let timeoutMs = Int32(min(Double(Int32.max), ceil(remaining * 1000.0)))
            let ready = Darwin.poll(&pfd, 1, timeoutMs)
            if ready < 0 {
                if errno == EINTR { continue }
                return nil
            }
            if ready == 0 { return nil }

            let n = Darwin.read(fd, &chunk, chunk.count)
            if n > 0 {
                buffer.append(contentsOf: chunk.prefix(n))
            } else if n < 0 && errno == EINTR {
                continue
            } else {
                break
            }
        }
        return nil
    }
}

/// Full-height right-side Session Inspector split (or slide-over drawer on narrow windows)
/// wrapping a session pane's main content (transcript, docked composer, and optional bottom shell).
struct PCodeInspectorSplit<MainContent: View>: View {
    @EnvironmentObject var ghostty: Ghostty.App
    @ObservedObject var pane: PCodeSessionPane
    @ObservedObject var inspectorState: PCodeInspectorState
    @ObservedObject var changesStore: PCodeSessionChangesStore
    @ObservedObject var agentsStore: PCodeInspectorAgentsStore
    @ObservedObject var commandsStore: PCodeInspectorCommandsStore
    @ObservedObject private var typography = PCodeTypography.shared
    let mainContent: () -> MainContent

    @State private var dragStartFraction: CGFloat?
    @State private var dividerHovered = false

    init(
        pane: PCodeSessionPane,
        @ViewBuilder mainContent: @escaping () -> MainContent
    ) {
        self.pane = pane
        self.inspectorState = pane.inspectorState
        self.changesStore = pane.changesStore
        self.agentsStore = pane.agentsStore
        self.commandsStore = pane.commandsStore
        self.mainContent = mainContent
    }

    private var colorScheme: ColorScheme {
        NSColor(ghostty.config.backgroundColor).isLightColor ? .light : .dark
    }

    private var inspectorBackground: Color {
        Color(nsColor: NSColor(ghostty.config.backgroundColor).blended(
            withFraction: 0.03,
            of: colorScheme == .dark ? .white : .black
        ) ?? NSColor(ghostty.config.backgroundColor))
    }

    private func actions(isOverlay: Bool) -> PCodeInspectorActions {
        let surface = pane.surface
        let directory = changesStore.sessionDirectory ?? pane.directory
        let sessionID = pane.sessionID
        let hostID = surface.pcodeHost
            ?? pane.hostID
            ?? pane.hostStatus?.hostID
            ?? pane.lastHostID
            ?? pane.launchHostID
        let state = inspectorState
        return PCodeInspectorActions(
            insertIntoComposer: { text in
                guard !text.isEmpty else { return }
                if state.isExpanded {
                    state.isExpanded = false
                }
                if isOverlay {
                    state.close()
                }
                surface.surfaceModel?.sendText(text)
                Ghostty.moveFocus(to: surface)
            },
            openInEditor: { path, line in
                let resolved = Self.resolvePath(path, relativeTo: directory)
                PCodeExternalEditor.open(path: resolved, line: line)
            },
            copyToClipboard: { text in
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(text, forType: .string)
            },
            stopJob: { jobID in
                PCodeHostControlRPC.stopJobs([jobID], sessionID: sessionID, hostID: hostID)
            }
        )
    }

    static func overlayDrawerWidth(in totalWidth: CGFloat, fraction: CGFloat) -> CGFloat {
        let minWidth = min(totalWidth, PCodeInspectorState.minSplitWidth)
        let maxWidth = max(minWidth, totalWidth * 0.85)
        return min(maxWidth, max(minWidth, totalWidth * fraction))
    }

    private static func resolvePath(_ path: String, relativeTo directory: String) -> String {
        guard !path.isEmpty else { return path }
        if (path as NSString).isAbsolutePath {
            return path
        }
        return URL(fileURLWithPath: directory, isDirectory: true)
            .appendingPathComponent(path)
            .standardizedFileURL
            .path
    }

    var body: some View {
        GeometryReader { geometry in
            let totalWidth = max(1, geometry.size.width)
            let useOverlay = PCodeInspectorState.shouldUseOverlay(containerWidth: totalWidth)
            let isSplit = inspectorState.isOpen && !inspectorState.isExpanded && !useOverlay
            let isOverlay = inspectorState.isOpen && !inspectorState.isExpanded && useOverlay
            let isExpanded = inspectorState.isOpen && inspectorState.isExpanded
            let inspectorWidth = isSplit
                ? PCodeInspectorState.clampedWidth(in: totalWidth, fraction: inspectorState.splitFraction)
                : 0
            let drawerWidth = Self.overlayDrawerWidth(in: totalWidth, fraction: inspectorState.splitFraction)

            // Keep `mainContent()` at a single invariant structural position across all states
            // so opening/closing/expanding or crossing the overlay width threshold never tears
            // down the live transcript controller or Ghostty terminal surfaces.
            ZStack(alignment: .trailing) {
                HStack(spacing: 0) {
                    mainContent()
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .allowsHitTesting(!isExpanded)
                        .accessibilityHidden(isExpanded)

                    if isSplit {
                        Rectangle()
                            .fill(
                                dividerHovered || dragStartFraction != nil
                                    ? Color.accentColor.opacity(0.6)
                                    : ghostty.config.splitDividerColor
                            )
                            .frame(width: 1)

                        inspectorPanel(totalWidth: inspectorWidth, isOverlay: false)
                            .frame(width: inspectorWidth)
                            .frame(maxHeight: .infinity)
                    }
                }

                if isSplit {
                    resizeHandle(
                        totalWidth: totalWidth,
                        displayedWidth: inspectorWidth,
                        isOverlay: false
                    )
                    .frame(width: 11)
                    .offset(x: -(inspectorWidth - 5))
                    .zIndex(5)
                }

                if isOverlay {
                    Color.black.opacity(colorScheme == .dark ? 0.34 : 0.18)
                        .ignoresSafeArea()
                        .onTapGesture {
                            inspectorState.close()
                            Ghostty.moveFocus(to: pane.keyboardSurface)
                        }

                    ZStack(alignment: .leading) {
                        inspectorPanel(totalWidth: drawerWidth, isOverlay: true)
                            .frame(width: drawerWidth)
                            .frame(maxHeight: .infinity)
                            .shadow(color: Color.black.opacity(0.28), radius: 14, x: -4, y: 0)

                        resizeHandle(
                            totalWidth: totalWidth,
                            displayedWidth: drawerWidth,
                            isOverlay: true
                        )
                        .frame(width: 11)
                        .offset(x: -5)
                    }
                    .frame(width: drawerWidth)
                    .frame(maxHeight: .infinity)
                }

                if isExpanded {
                    inspectorPanel(totalWidth: totalWidth, isOverlay: false)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
            .background(
                PCodeInspectorEscapeMonitor(enabled: isOverlay || isExpanded) {
                    if inspectorState.isExpanded {
                        inspectorState.isExpanded = false
                    } else {
                        inspectorState.close()
                    }
                    Ghostty.moveFocus(to: pane.keyboardSurface)
                }
            )
        }
        .onAppear {
            syncChangesStoreGitPolling()
        }
        .onChange(of: inspectorState.diffScope) { _ in
            syncChangesStoreGitPolling()
        }
        .onChange(of: inspectorState.isOpen) { _ in
            syncChangesStoreGitPolling()
        }
        .onChange(of: inspectorState.activeTab) { _ in
            syncChangesStoreGitPolling()
        }
    }

    private func syncChangesStoreGitPolling() {
        let shouldPollGit = inspectorState.isOpen
            && inspectorState.activeTab == .changes
            && (inspectorState.diffScope == .workingTree || inspectorState.diffScope == .branch)
        if shouldPollGit {
            changesStore.setActiveScope(inspectorState.diffScope, triggerRefresh: true)
        } else {
            changesStore.setActiveScope(.session, triggerRefresh: false)
            changesStore.cancelPendingGitRefresh()
        }
    }

    private func resizeHandle(totalWidth: CGFloat, displayedWidth: CGFloat, isOverlay: Bool) -> some View {
        PCodeResizeHandle(
            orientation: .horizontal,
            onHover: { dividerHovered = $0 },
            onDoubleClick: { inspectorState.toggleExpanded() },
            onDragStart: {
                dragStartFraction = displayedWidth / max(1, totalWidth)
            },
            onDragChanged: { delta in
                let base = dragStartFraction ?? (displayedWidth / max(1, totalWidth))
                let nextFraction = base - (delta / max(1, totalWidth))
                let maxFrac: CGFloat = isOverlay ? 0.85 : PCodeInspectorState.maxSplitFraction
                inspectorState.splitFraction = min(maxFrac, max(PCodeInspectorState.minSplitFraction, nextFraction))
            },
            onDragEnded: {
                dragStartFraction = nil
            }
        )
    }

    private func inspectorPanel(totalWidth: CGFloat, isOverlay: Bool) -> some View {
        VStack(spacing: 0) {
            inspectorHeader(isOverlay: isOverlay)

            Rectangle()
                .fill(ghostty.config.splitDividerColor.opacity(0.75))
                .frame(height: 1)

            inspectorBody(isOverlay: isOverlay)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(inspectorBackground)
        .environment(\.colorScheme, colorScheme)
    }

    private func inspectorHeader(isOverlay: Bool) -> some View {
        HStack(spacing: 6) {
            HStack(spacing: 2) {
                tabButton(.changes, badge: changesBadgeCount, pulsing: changesHasStreaming)
                tabButton(.agents, badge: agentsBadgeCount, pulsing: agentsStore.activeCount > 0)
                tabButton(.commands, badge: commandsBadgeCount, pulsing: commandsStore.runningCount > 0)
            }
            .padding(2)
            .background(
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .fill(Color.primary.opacity(colorScheme == .dark ? 0.08 : 0.06))
            )

            Spacer(minLength: 4)

            Button {
                inspectorState.toggleExpanded()
            } label: {
                Image(systemName: inspectorState.isExpanded
                    ? "arrow.down.right.and.arrow.up.left"
                    : "arrow.up.left.and.arrow.down.right")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                    .frame(width: 22, height: 22)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(inspectorState.isExpanded ? "Restore split (⌘⇧F)" : "Expand full width (⌘⇧F)")

            Button {
                inspectorState.close()
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                    .frame(width: 22, height: 22)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(isOverlay ? "Close inspector (Esc / ⌘I)" : "Close inspector (⌘I)")
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
    }

    private var changesBadgeCount: Int {
        let count = changesStore.files(for: inspectorState.diffScope).count
        return count > 0 ? count : changesStore.sessionFiles.count
    }

    private var changesHasStreaming: Bool {
        changesStore.sessionFiles.contains(where: \.isStreaming)
    }

    private var agentsBadgeCount: Int {
        agentsStore.activeCount > 0 ? agentsStore.activeCount : agentsStore.agents.count
    }

    private var commandsBadgeCount: Int {
        commandsStore.runningCount > 0 ? commandsStore.runningCount : commandsStore.commands.count
    }

    private func tabButton(_ tab: PCodeInspectorTab, badge: Int, pulsing: Bool) -> some View {
        let selected = inspectorState.activeTab == tab
        return Button {
            inspectorState.activeTab = tab
        } label: {
            HStack(spacing: 4) {
                Text(tab.title)
                    .font(.system(size: 11.5, weight: selected ? .semibold : .medium))
                if pulsing {
                    Circle()
                        .fill(Color.accentColor)
                        .frame(width: 5, height: 5)
                }
                if badge > 0 {
                    Text("\(badge)")
                        .font(.system(size: 10, weight: .medium, design: .monospaced))
                        .foregroundStyle(selected ? .primary : .secondary)
                        .padding(.horizontal, 4)
                        .padding(.vertical, 1)
                        .background(
                            Capsule()
                                .fill(Color.primary.opacity(selected ? 0.14 : 0.08))
                        )
                }
            }
            .foregroundStyle(selected ? .primary : .secondary)
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(
                RoundedRectangle(cornerRadius: 4.5, style: .continuous)
                    .fill(selected ? Color.primary.opacity(colorScheme == .dark ? 0.14 : 0.10) : .clear)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    @ViewBuilder
    private func inspectorBody(isOverlay: Bool) -> some View {
        let resolvedActions = actions(isOverlay: isOverlay)
        switch inspectorState.activeTab {
        case .changes:
            let diffs = changesStore.files(for: inspectorState.diffScope)
            let singleEditLabel: String? = {
                if case .singleEdit(let callID) = inspectorState.diffScope,
                   let single = changesStore.singleEdit(for: callID) {
                    return single.basename
                }
                return nil
            }()
            PCodeChangesInspectorView(
                state: inspectorState,
                diffs: diffs,
                actions: resolvedActions,
                singleEditLabel: singleEditLabel,
                isLoading: changesStore.isLoadingGit && (inspectorState.diffScope == .workingTree || inspectorState.diffScope == .branch),
                codeFont: typography.snapshot.codeNSFont(scale: typography.snapshot.codeBlockScale),
                onScopeChange: { scope in
                    changesStore.setActiveScope(scope, triggerRefresh: true)
                },
                onRefresh: {
                    let scope = inspectorState.diffScope
                    if scope == .workingTree || scope == .branch {
                        Task { await changesStore.refreshGit(for: scope) }
                    }
                }
            )
        case .agents:
            PCodeInspectorAgentsView(
                state: inspectorState,
                store: agentsStore,
                actions: resolvedActions
            )
        case .commands:
            PCodeInspectorCommandsView(
                state: inspectorState,
                store: commandsStore,
                actions: resolvedActions
            )
        }
    }
}

/// Window-scoped local key monitor that dismisses narrow-overlay or expanded Inspector on `Esc`
/// even when focus stays in the Ghostty composer or an AppKit transcript/text view.
private struct PCodeInspectorEscapeMonitor: NSViewRepresentable {
    let enabled: Bool
    let onEscape: () -> Void

    func makeNSView(context: Context) -> MonitorView {
        let view = MonitorView()
        view.enabled = enabled
        view.onEscape = onEscape
        return view
    }

    func updateNSView(_ view: MonitorView, context: Context) {
        view.enabled = enabled
        view.onEscape = onEscape
    }

    static func dismantleNSView(_ view: MonitorView, coordinator: ()) {
        view.teardown()
    }

    final class MonitorView: NSView {
        var enabled = false {
            didSet { syncMonitor() }
        }
        var onEscape: (() -> Void)?
        private var monitor: Any?

        deinit {
            teardown()
        }

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            syncMonitor()
        }

        func teardown() {
            if let monitor {
                NSEvent.removeMonitor(monitor)
                self.monitor = nil
            }
        }

        private func syncMonitor() {
            guard enabled, window != nil else {
                teardown()
                return
            }
            guard monitor == nil else { return }
            monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
                guard let self,
                      self.enabled,
                      let window = self.window,
                      event.window === window,
                      event.keyCode == 53, // Escape
                      event.modifierFlags.intersection([.command, .option, .control, .shift]).isEmpty
                else {
                    return event
                }
                // Let active text-input fields (e.g. agent message draft or search bar) handle Escape first.
                if let responder = window.firstResponder as? NSTextView, responder.isEditable {
                    return event
                }
                self.onEscape?()
                return nil
            }
        }
    }
}

/// Compact `+N -M` stat pill shown on an open session's sidebar row when it has edits.
struct PCodeSidebarSessionChangeStats: View {
    @ObservedObject var changesStore: PCodeSessionChangesStore

    var body: some View {
        let stats = changesStore.sessionStats
        if !changesStore.sessionFiles.isEmpty && (stats.added > 0 || stats.removed > 0) {
            HStack(spacing: 3) {
                if stats.added > 0 {
                    Text("+\(stats.added)")
                        .foregroundStyle(Color.green.opacity(0.85))
                }
                if stats.removed > 0 {
                    Text("-\(stats.removed)")
                        .foregroundStyle(Color.red.opacity(0.85))
                }
            }
            .font(.system(size: 10, weight: .medium, design: .monospaced))
            .fixedSize()
        }
    }
}

/// Compact nested list of files edited by an open session, shown under its sidebar row.
struct PCodeSidebarFileChangesList: View {
    @ObservedObject var changesStore: PCodeSessionChangesStore
    @ObservedObject var inspectorState: PCodeInspectorState
    let isSelectedPane: Bool
    let onSelectFile: (String) -> Void
    let onSelectOverflow: () -> Void

    static let maxVisibleFiles = 5

    private var activeFilePath: String? {
        guard isSelectedPane, inspectorState.isOpen, inspectorState.activeTab == .changes else {
            return nil
        }
        return inspectorState.focusedFilePath
    }

    var body: some View {
        let slice = changesStore.sidebarFiles(limit: Self.maxVisibleFiles)
        if !slice.visible.isEmpty {
            let duplicateBasenames = Self.duplicateBasenames(in: slice.visible)
            VStack(alignment: .leading, spacing: 1) {
                ForEach(slice.visible) { file in
                    fileRow(file, showParent: duplicateBasenames.contains(file.basename))
                }
                if slice.overflowCount > 0 {
                    Button(action: onSelectOverflow) {
                        HStack(spacing: 4) {
                            Text("+ \(slice.overflowCount) more…")
                                .font(.system(size: 10.5))
                                .foregroundStyle(.secondary)
                            Spacer(minLength: 4)
                        }
                        .padding(.leading, 28)
                        .padding(.trailing, 8)
                        .padding(.vertical, 2)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.bottom, 3)
        }
    }

    private func fileRow(_ file: PCodeFileDiff, showParent: Bool) -> some View {
        let isFocused = activeFilePath == file.path
        return Button {
            onSelectFile(file.path)
        } label: {
            HStack(spacing: 5) {
                Text(file.kind.badge)
                    .font(.system(size: 9.5, weight: .bold, design: .monospaced))
                    .foregroundStyle(badgeColor(for: file.kind))
                    .frame(width: 10, alignment: .center)

                if file.isStreaming {
                    Circle()
                        .fill(Color.accentColor)
                        .frame(width: 4.5, height: 4.5)
                }

                Text(file.basename)
                    .font(.system(size: 11, weight: isFocused ? .medium : .regular))
                    .foregroundStyle(isFocused ? .primary : .secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)

                if showParent && !file.parentDirectory.isEmpty {
                    Text(file.parentDirectory)
                        .font(.system(size: 10))
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                        .truncationMode(.head)
                }

                Spacer(minLength: 4)

                HStack(spacing: 3) {
                    if file.added > 0 {
                        Text("+\(file.added)")
                            .foregroundStyle(Color.green.opacity(0.85))
                    }
                    if file.removed > 0 {
                        Text("-\(file.removed)")
                            .foregroundStyle(Color.red.opacity(0.85))
                    }
                }
                .font(.system(size: 10, weight: .medium, design: .monospaced))
            }
            .padding(.leading, 26)
            .padding(.trailing, 8)
            .padding(.vertical, 2.5)
            .background(
                RoundedRectangle(cornerRadius: 4, style: .continuous)
                    .fill(isFocused ? Color.accentColor.opacity(0.14) : .clear)
                    .padding(.leading, 20)
                    .padding(.trailing, 4)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(file.path)
    }

    private func badgeColor(for kind: PCodeFileChangeKind) -> Color {
        switch kind {
        case .added: return .green
        case .modified: return .orange
        case .deleted: return .red
        case .renamed: return .blue
        }
    }

    private static func duplicateBasenames(in files: [PCodeFileDiff]) -> Set<String> {
        var seen: Set<String> = []
        var dupes: Set<String> = []
        for file in files {
            if !seen.insert(file.basename).inserted {
                dupes.insert(file.basename)
            }
        }
        return dupes
    }
}
