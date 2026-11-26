import AppKit
import Combine
import Foundation
import SwiftUI

// MARK: - Commands Store

/// Per-session live store tracking shell tool calls, background jobs, and `!command` shell mode
/// executions along with their streaming `commandOutput`.
@MainActor
final class PCodeInspectorCommandsStore: ObservableObject {
    @Published private(set) var commands: [PCodeInspectorCommandItem] = []

    /// Number of currently running commands/jobs, used for the Inspector tab badge.
    var runningCount: Int {
        commands.reduce(0) { $0 + ($1.isRunning ? 1 : 0) }
    }

    private struct CommandRecord {
        let id: String
        var command: String
        var detail: String
        var output: String
        var status: PCodeInspectorCommandItem.Status
        var isBackgroundJob: Bool
        var exitCode: Int?
        var startedAt: Date?
        var elapsedSeconds: Double?
        var lastActivityAt: Date
        var sequence: Int

        func makeItem() -> PCodeInspectorCommandItem {
            PCodeInspectorCommandItem(
                id: id,
                command: command,
                detail: detail,
                output: output,
                status: status,
                isBackgroundJob: isBackgroundJob,
                exitCode: exitCode,
                startedAt: startedAt,
                elapsedSeconds: elapsedSeconds
            )
        }
    }

    private var recordsByID: [String: CommandRecord] = [:]
    private var activeShellModeCallID: String?
    private var syntheticCounter: Int = 0
    private var sequence: Int = 0

    init() {}

    // MARK: - Queries

    func command(for id: String) -> PCodeInspectorCommandItem? {
        recordsByID[id]?.makeItem()
    }

    func reset() {
        resetInternal()
        commands = []
    }

    // MARK: - Event Ingestion

    func apply(_ event: PCodeHostEvent, now: Date = Date()) {
        applyInternal(event, now: now, replayingHistory: false)
    }

    private func applyInternal(_ event: PCodeHostEvent, now: Date, replayingHistory: Bool) {
        switch event {
        case .toolStarted(
            let callID,
            _,
            let name,
            let detail,
            let command,
            _
        ):
            guard !callID.isEmpty,
                  !PCodeToolGrouper.isDelegateTool(name),
                  Self.isCommandTool(name: name, command: command)
            else { return }

            if callID.hasPrefix("shell_mode_") {
                activeShellModeCallID = callID
            }

            sequence += 1
            let existing = recordsByID[callID]
            let resolvedCommand = !command.isEmpty ? command : (existing?.command.isEmpty == false ? existing!.command : detail)
            let resolvedDetail = !detail.isEmpty ? detail : (existing?.detail ?? "")
            let isBackground = (existing?.isBackgroundJob ?? false)
                || Self.looksLikeBackgroundJob(name: name, callID: callID)

            recordsByID[callID] = CommandRecord(
                id: callID,
                command: resolvedCommand,
                detail: resolvedDetail,
                output: existing?.output ?? "",
                status: .running,
                isBackgroundJob: isBackground,
                exitCode: nil,
                startedAt: existing?.startedAt ?? (replayingHistory ? nil : now),
                elapsedSeconds: nil,
                lastActivityAt: now,
                sequence: sequence
            )

            if !replayingHistory {
                rebuildCommands()
            }

        case .commandOutput(let callID, let command, let output):
            guard !callID.isEmpty else { return }
            if callID.hasPrefix("shell_mode_") {
                activeShellModeCallID = callID
            }

            sequence += 1
            let existing = recordsByID[callID]
            let resolvedCommand = !command.isEmpty ? command : (existing?.command ?? "")
            let resolvedDetail = (existing?.detail.isEmpty == false) ? existing!.detail : resolvedCommand
            let status = existing?.status ?? .running
            let startedAt: Date?
            if status == .running {
                startedAt = existing?.startedAt ?? (replayingHistory ? nil : now)
            } else {
                startedAt = existing?.startedAt
            }

            recordsByID[callID] = CommandRecord(
                id: callID,
                command: resolvedCommand,
                detail: resolvedDetail,
                output: output,
                status: status,
                isBackgroundJob: (existing?.isBackgroundJob ?? false) || Self.looksLikeBackgroundJob(name: "", callID: callID),
                exitCode: existing?.exitCode,
                startedAt: startedAt,
                elapsedSeconds: existing?.elapsedSeconds,
                lastActivityAt: now,
                sequence: sequence
            )

            if !replayingHistory {
                rebuildCommands()
            }

        case .toolSummary(
            let callID,
            _,
            let name,
            let detail,
            let command,
            let failed,
            let elapsedSeconds,
            let error,
            let result,
            let isBackgroundJob,
            let exitCode
        ):
            let isTracked = !callID.isEmpty && recordsByID[callID] != nil
            guard isTracked || (!PCodeToolGrouper.isDelegateTool(name) && (Self.isCommandTool(name: name, command: command) || isBackgroundJob)) else {
                return
            }

            let key: String
            if !callID.isEmpty {
                key = callID
            } else {
                syntheticCounter += 1
                key = "cmd-\(syntheticCounter)"
            }

            if key == activeShellModeCallID {
                activeShellModeCallID = nil
            }

            sequence += 1
            let existing = recordsByID[key]
            let resolvedCommand: String
            if !command.isEmpty {
                resolvedCommand = command
            } else if let existingCommand = existing?.command, !existingCommand.isEmpty {
                resolvedCommand = existingCommand
            } else {
                resolvedCommand = detail
            }

            let resolvedDetail: String
            if !detail.isEmpty {
                resolvedDetail = detail
            } else if !error.isEmpty {
                resolvedDetail = error
            } else {
                resolvedDetail = existing?.detail ?? ""
            }

            let resolvedOutput: String
            if let result, !result.isEmpty {
                resolvedOutput = result
            } else if let existingOutput = existing?.output, !existingOutput.isEmpty {
                resolvedOutput = existingOutput
            } else if !error.isEmpty {
                resolvedOutput = error
            } else {
                resolvedOutput = ""
            }

            let resolvedExitCode = exitCode ?? existing?.exitCode
            let resolvedStatus = Self.settledStatus(failed: failed, exitCode: resolvedExitCode)
            let resolvedElapsed: Double?
            if let elapsedSeconds {
                resolvedElapsed = elapsedSeconds
            } else if let startedAt = existing?.startedAt, !replayingHistory {
                resolvedElapsed = max(0, now.timeIntervalSince(startedAt))
            } else {
                resolvedElapsed = existing?.elapsedSeconds
            }

            recordsByID[key] = CommandRecord(
                id: key,
                command: resolvedCommand,
                detail: resolvedDetail,
                output: resolvedOutput,
                status: resolvedStatus,
                isBackgroundJob: isBackgroundJob || (existing?.isBackgroundJob ?? false),
                exitCode: resolvedExitCode,
                startedAt: existing?.startedAt,
                elapsedSeconds: resolvedElapsed,
                lastActivityAt: now,
                sequence: sequence
            )

            if !replayingHistory {
                rebuildCommands()
            }

        case .shellResult(let command, let output, let failed, let elapsedSeconds):
            let key: String
            if let active = activeShellModeCallID {
                key = active
                activeShellModeCallID = nil
            } else {
                syntheticCounter += 1
                key = "shell_mode_\(syntheticCounter)"
            }

            sequence += 1
            let existing = recordsByID[key]
            let resolvedCommand = !command.isEmpty ? command : (existing?.command ?? "")
            let resolvedOutput = !output.isEmpty ? output : (existing?.output ?? "")
            let resolvedElapsed = elapsedSeconds ?? existing?.startedAt.flatMap { replayingHistory ? nil : max(0, now.timeIntervalSince($0)) }

            recordsByID[key] = CommandRecord(
                id: key,
                command: resolvedCommand,
                detail: resolvedCommand.isEmpty ? (existing?.detail ?? "") : resolvedCommand,
                output: resolvedOutput,
                status: failed ? .failed : .succeeded,
                isBackgroundJob: false,
                exitCode: existing?.exitCode,
                startedAt: existing?.startedAt,
                elapsedSeconds: resolvedElapsed,
                lastActivityAt: now,
                sequence: sequence
            )

            if !replayingHistory {
                rebuildCommands()
            }

        case .dropOutput(let callID):
            guard var record = recordsByID[callID], record.status == .running else { return }
            record.status = .succeeded
            if record.elapsedSeconds == nil, let startedAt = record.startedAt, !replayingHistory {
                record.elapsedSeconds = max(0, now.timeIntervalSince(startedAt))
            }
            sequence += 1
            record.lastActivityAt = now
            record.sequence = sequence
            recordsByID[callID] = record
            if !replayingHistory {
                rebuildCommands()
            }

        case .turnEnded(let status):
            activeShellModeCallID = nil
            let settled = settleRunningCommands(
                failed: status != .complete,
                includeBackgroundJobs: false,
                now: now,
                replayingHistory: replayingHistory
            )
            if settled && !replayingHistory {
                rebuildCommands()
            }

        case .history(let events):
            resetInternal()
            for historical in events {
                applyInternal(historical, now: now, replayingHistory: true)
            }
            _ = settleRunningCommands(
                failed: false,
                includeBackgroundJobs: true,
                now: now,
                replayingHistory: true
            )
            rebuildCommands()

        case .conversationReset:
            resetInternal()
            commands = []

        default:
            break
        }
    }

    // MARK: - Internal Helpers

    private func resetInternal() {
        recordsByID.removeAll()
        activeShellModeCallID = nil
        syntheticCounter = 0
        sequence = 0
    }

    private func settleRunningCommands(
        failed: Bool,
        includeBackgroundJobs: Bool,
        now: Date,
        replayingHistory: Bool
    ) -> Bool {
        var changed = false
        for (id, var record) in recordsByID where record.status == .running {
            if record.isBackgroundJob && !includeBackgroundJobs {
                continue
            }
            record.status = failed ? .failed : .succeeded
            if record.elapsedSeconds == nil, let startedAt = record.startedAt, !replayingHistory {
                record.elapsedSeconds = max(0, now.timeIntervalSince(startedAt))
            }
            recordsByID[id] = record
            changed = true
        }
        return changed
    }

    private func rebuildCommands() {
        let sorted = recordsByID.values.sorted { lhs, rhs in
            let lhsRunning = lhs.status == .running
            let rhsRunning = rhs.status == .running
            if lhsRunning != rhsRunning {
                return lhsRunning && !rhsRunning
            }
            if lhs.lastActivityAt != rhs.lastActivityAt {
                return lhs.lastActivityAt > rhs.lastActivityAt
            }
            return lhs.sequence > rhs.sequence
        }
        commands = sorted.map { $0.makeItem() }
    }

    static func isCommandTool(name: String, command: String) -> Bool {
        if !command.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return true
        }
        switch name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "shell", "run_command", "bash", "start_command", "wait_for_job":
            return true
        default:
            return false
        }
    }

    static func looksLikeBackgroundJob(name: String, callID: String) -> Bool {
        let lower = name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if lower == "start_command" {
            return true
        }
        return callID.hasPrefix("job_") || callID.hasPrefix("bg_")
    }

    static func settledStatus(failed: Bool, exitCode: Int?) -> PCodeInspectorCommandItem.Status {
        if failed {
            return .failed
        }
        if let exitCode, exitCode != 0 {
            return .exitedNonZero
        }
        return .succeeded
    }
}

// MARK: - Commands Inspector View

/// Right-hand Inspector "Commands" tab pairing a compact command list with a live monospace
/// output tailer and Kill / Copy / Reference (`⌘L`) controls.
struct PCodeInspectorCommandsView: View {
    @ObservedObject var state: PCodeInspectorState
    @ObservedObject var store: PCodeInspectorCommandsStore
    let actions: PCodeInspectorActions
    let codeFont: NSFont?

    @Environment(\.colorScheme) private var colorScheme
    @State private var selectedCommandID: String?
    @State private var isFollowingTail: Bool = true
    @State private var scrollToBottomToken: Int = 0
    @State private var copiedCommandID: String?

    init(
        state: PCodeInspectorState,
        store: PCodeInspectorCommandsStore,
        actions: PCodeInspectorActions = .noop,
        codeFont: NSFont? = nil
    ) {
        self.state = state
        self.store = store
        self.actions = actions
        self.codeFont = codeFont
    }

    private var isDark: Bool { colorScheme == .dark }

    private var resolvedCodeFont: NSFont {
        if let codeFont { return codeFont }
        if let jb = NSFont(name: "JetBrainsMono-Regular", size: 11.5) {
            return jb
        }
        return NSFont.monospacedSystemFont(ofSize: 11.5, weight: .regular)
    }

    private var selectedCommand: PCodeInspectorCommandItem? {
        if let selectedCommandID, let match = store.commands.first(where: { $0.id == selectedCommandID }) {
            return match
        }
        return store.commands.first
    }

    var body: some View {
        VStack(spacing: 0) {
            headerBar
            Divider()
            contentArea
        }
        .background(Color(nsColor: .textBackgroundColor))
        .background(keyboardShortcutHooks)
        .onAppear {
            syncSelection(preferredID: state.focusedCommandID)
        }
        .onChange(of: state.focusedCommandID) { targetID in
            syncSelection(preferredID: targetID)
        }
        .onChange(of: store.commands.map(\.id)) { _ in
            ensureValidSelection()
        }
    }

    // MARK: - Composer Reference Formatting

    /// Builds a clean summary + excerpt of a command and its output for `⌘L` composer injection.
    static func promptReference(
        for item: PCodeInspectorCommandItem,
        maxLines: Int = 30,
        maxCharacters: Int = 1500
    ) -> String {
        let title = item.command.isEmpty ? item.displayTitle : item.command
        var statusParts: [String] = []
        switch item.status {
        case .running:
            statusParts.append("running")
        case .succeeded:
            break
        case .failed:
            statusParts.append("failed")
        case .exitedNonZero:
            if let code = item.exitCode {
                statusParts.append("exit \(code)")
            } else {
                statusParts.append("non-zero exit")
            }
        }
        if item.status == .failed, let code = item.exitCode {
            statusParts.append("exit \(code)")
        }

        let headerSuffix = statusParts.isEmpty ? "" : " (\(statusParts.joined(separator: ", ")))"
        let header = "Command `\(title)`\(headerSuffix):"

        let trimmedOutput = item.output.trimmingCharacters(in: .newlines)
        guard !trimmedOutput.isEmpty else {
            return header
        }

        let lines = trimmedOutput.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        var wasTruncated = false
        var excerptLines = lines
        if lines.count > maxLines {
            excerptLines = Array(lines.suffix(maxLines))
            wasTruncated = true
        }
        var excerpt = excerptLines.joined(separator: "\n")
        if excerpt.count > maxCharacters {
            excerpt = String(excerpt.suffix(maxCharacters))
            wasTruncated = true
        }
        if wasTruncated {
            excerpt = "… (truncated)\n" + excerpt
        }

        return "\(header)\n```\n\(excerpt)\n```"
    }

    // MARK: - Header

    private var headerBar: some View {
        HStack(spacing: 8) {
            Image(systemName: "terminal")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.secondary)

            Text(store.commands.isEmpty ? "Commands" : "Commands · \(store.commands.count)")
                .font(.system(size: 11.5, weight: .semibold))

            if store.runningCount > 0 {
                HStack(spacing: 4) {
                    Circle()
                        .fill(Color.accentColor)
                        .frame(width: 6, height: 6)
                    Text("\(store.runningCount) running")
                        .font(.system(size: 10.5, weight: .medium))
                        .foregroundStyle(Color.accentColor)
                }
                .padding(.horizontal, 7)
                .padding(.vertical, 2)
                .background(
                    Capsule()
                        .fill(Color.accentColor.opacity(isDark ? 0.22 : 0.12))
                )
            }

            Spacer(minLength: 4)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .background(Color(nsColor: .windowBackgroundColor).opacity(0.65))
    }

    // MARK: - Content Layout

    @ViewBuilder
    private var contentArea: some View {
        if store.commands.isEmpty {
            emptyState
        } else {
            GeometryReader { geo in
                let listHeight = min(max(110, geo.size.height * 0.34), 240)
                VStack(spacing: 0) {
                    commandList
                        .frame(height: listHeight)

                    Divider()

                    if let selected = selectedCommand {
                        commandDetailPane(for: selected)
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                    }
                }
            }
        }
    }

    private var emptyState: some View {
        VStack(spacing: 8) {
            Image(systemName: "terminal")
                .font(.system(size: 24, weight: .light))
                .foregroundStyle(.tertiary)
            Text("No commands yet")
                .font(.system(size: 12.5, weight: .semibold))
                .foregroundStyle(.secondary)
            Text("Shell commands and background jobs appear here with live output.")
                .font(.system(size: 11))
                .foregroundStyle(.tertiary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 250)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(24)
    }

    // MARK: - Command List

    private var commandList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: 2) {
                    ForEach(store.commands) { item in
                        commandRow(item, isSelected: item.id == selectedCommand?.id)
                            .id(item.id)
                    }
                }
                .padding(6)
            }
            .onChange(of: state.focusedCommandID) { targetID in
                guard let targetID, store.commands.contains(where: { $0.id == targetID }) else { return }
                withAnimation(.easeInOut(duration: 0.16)) {
                    proxy.scrollTo(targetID, anchor: .center)
                }
            }
        }
    }

    private func commandRow(_ item: PCodeInspectorCommandItem, isSelected: Bool) -> some View {
        Button {
            selectedCommandID = item.id
            isFollowingTail = item.isRunning
            if item.isRunning {
                scrollToBottomToken &+= 1
            }
        } label: {
            HStack(spacing: 7) {
                commandStatusIcon(for: item.status)

                Text(item.displayTitle)
                    .font(.system(size: 11.5, design: .monospaced))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                    .truncationMode(.tail)

                Spacer(minLength: 4)

                if item.isBackgroundJob {
                    Text("BG")
                        .font(.system(size: 9, weight: .bold, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 4)
                        .padding(.vertical, 1)
                        .background(
                            RoundedRectangle(cornerRadius: 3, style: .continuous)
                                .fill(Color.primary.opacity(isDark ? 0.12 : 0.08))
                        )
                }

                if let code = item.exitCode, code != 0 {
                    Text("exit \(code)")
                        .font(.system(size: 9.5, weight: .medium, design: .monospaced))
                        .foregroundStyle(item.status == .failed ? .red : .orange)
                }

                commandElapsedLabel(for: item)
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .background(
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .fill(
                        isSelected
                            ? Color.accentColor.opacity(isDark ? 0.22 : 0.14)
                            : Color.clear
                    )
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    @ViewBuilder
    private func commandStatusIcon(for status: PCodeInspectorCommandItem.Status) -> some View {
        switch status {
        case .running:
            ProgressView()
                .controlSize(.mini)
                .frame(width: 13, height: 13)
        case .succeeded:
            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 11.5))
                .foregroundStyle(.green)
                .frame(width: 13, height: 13)
        case .exitedNonZero:
            Image(systemName: "exclamationmark.circle.fill")
                .font(.system(size: 11.5))
                .foregroundStyle(.orange)
                .frame(width: 13, height: 13)
        case .failed:
            Image(systemName: "xmark.circle.fill")
                .font(.system(size: 11.5))
                .foregroundStyle(.red)
                .frame(width: 13, height: 13)
        }
    }

    @ViewBuilder
    private func commandElapsedLabel(for item: PCodeInspectorCommandItem) -> some View {
        if item.isRunning, let startedAt = item.startedAt {
            TimelineView(.periodic(from: .now, by: 1.0)) { context in
                let elapsed = max(0, context.date.timeIntervalSince(startedAt))
                Text(PCodePeekToolCall.formatDuration(elapsed))
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.tertiary)
            }
        } else if let elapsed = item.elapsedSeconds, elapsed >= 0.05 {
            Text(PCodePeekToolCall.formatDuration(elapsed))
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.tertiary)
        }
    }

    // MARK: - Detail & Live Output Tailer

    private func commandDetailPane(for item: PCodeInspectorCommandItem) -> some View {
        let isCopied = copiedCommandID == item.id

        return VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(item.command.isEmpty ? item.displayTitle : item.command)
                        .font(.system(size: 11.5, weight: .medium, design: .monospaced))
                        .foregroundStyle(.primary)
                        .lineLimit(2)
                        .textSelection(.enabled)
                        .help(item.command.isEmpty ? item.displayTitle : item.command)
                    Spacer(minLength: 4)
                }

                HStack(spacing: 6) {
                    statusPill(for: item)

                    Spacer(minLength: 4)

                    if item.isRunning && !isFollowingTail {
                        Button {
                            isFollowingTail = true
                            scrollToBottomToken &+= 1
                        } label: {
                            Label("Tail", systemImage: "arrow.down.to.line")
                                .font(.system(size: 10.5, weight: .medium))
                        }
                        .buttonStyle(.bordered)
                        .controlSize(.mini)
                        .help("Resume auto-scrolling output")
                    }

                    if item.isRunning && item.isBackgroundJob {
                        Button(role: .destructive) {
                            actions.stopJob(item.id)
                        } label: {
                            Label("Kill", systemImage: "stop.circle.fill")
                                .font(.system(size: 10.5, weight: .medium))
                        }
                        .buttonStyle(.bordered)
                        .tint(.red)
                        .controlSize(.mini)
                        .help("Stop running background job")
                    }

                    Button {
                        actions.copyToClipboard(item.output)
                        copiedCommandID = item.id
                        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                            if copiedCommandID == item.id {
                                copiedCommandID = nil
                            }
                        }
                    } label: {
                        Label(isCopied ? "Copied" : "Copy", systemImage: isCopied ? "checkmark" : "doc.on.doc")
                            .font(.system(size: 10.5, weight: .medium))
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.mini)
                    .disabled(item.output.isEmpty)
                    .help("Copy command output")

                    Button {
                        actions.insertIntoComposer(Self.promptReference(for: item))
                    } label: {
                        Label("Reference", systemImage: "text.insert")
                            .font(.system(size: 10.5, weight: .medium))
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.mini)
                    .help("Reference command and output in prompt (⌘L)")
                }
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 8)
            .background(Color(nsColor: .windowBackgroundColor).opacity(0.45))

            Divider()

            if item.output.isEmpty {
                VStack(spacing: 6) {
                    if item.isRunning {
                        ProgressView()
                            .controlSize(.small)
                        Text("Waiting for output…")
                            .font(.system(size: 11))
                            .foregroundStyle(.tertiary)
                    } else {
                        Text("No output")
                            .font(.system(size: 11))
                            .foregroundStyle(.tertiary)
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                PCodeCommandOutputTailView(
                    commandID: item.id,
                    text: item.output,
                    font: resolvedCodeFont,
                    isRunning: item.isRunning,
                    isFollowingTail: $isFollowingTail,
                    scrollToBottomToken: scrollToBottomToken
                )
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }

    private func statusPill(for item: PCodeInspectorCommandItem) -> some View {
        let label: String
        let color: Color
        switch item.status {
        case .running:
            label = item.isBackgroundJob ? "Background · Running" : "Running"
            color = .accentColor
        case .succeeded:
            label = item.isBackgroundJob ? "Background · Done" : "Succeeded"
            color = .green
        case .exitedNonZero:
            if let code = item.exitCode {
                label = "Exit \(code)"
            } else {
                label = "Exited non-zero"
            }
            color = .orange
        case .failed:
            if let code = item.exitCode {
                label = "Failed · Exit \(code)"
            } else {
                label = "Failed"
            }
            color = .red
        }

        return Text(label)
            .font(.system(size: 10, weight: .semibold))
            .foregroundStyle(color)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(
                Capsule()
                    .fill(color.opacity(isDark ? 0.2 : 0.12))
            )
    }

    // MARK: - Keyboard Shortcuts

    private var keyboardShortcutHooks: some View {
        Group {
            Button("") {
                guard let selected = selectedCommand else { return }
                actions.insertIntoComposer(Self.promptReference(for: selected))
            }
            .keyboardShortcut("l", modifiers: .command)
        }
        .frame(width: 0, height: 0)
        .opacity(0.001)
        .allowsHitTesting(false)
    }

    private func syncSelection(preferredID: String?) {
        if let preferredID, store.commands.contains(where: { $0.id == preferredID }) {
            selectedCommandID = preferredID
            isFollowingTail = store.command(for: preferredID)?.isRunning ?? true
            return
        }
        ensureValidSelection()
    }

    private func ensureValidSelection() {
        if let current = selectedCommandID, store.commands.contains(where: { $0.id == current }) {
            return
        }
        selectedCommandID = store.commands.first?.id
        isFollowingTail = selectedCommand?.isRunning ?? true
    }
}

// MARK: - AppKit Monospace Live Output Tailer

/// Read-only selectable monospace `NSTextView` inside an `NSScrollView` that auto-scrolls
/// to the bottom while a command is running unless the user manually scrolls up.
private struct PCodeCommandOutputTailView: NSViewRepresentable {
    let commandID: String
    let text: String
    let font: NSFont
    let isRunning: Bool
    @Binding var isFollowingTail: Bool
    let scrollToBottomToken: Int

    func makeCoordinator() -> Coordinator {
        Coordinator(isFollowingTail: $isFollowingTail)
    }

    func makeNSView(context: Context) -> NSScrollView {
        let scrollView = NSScrollView()
        scrollView.hasVerticalScroller = true
        scrollView.hasHorizontalScroller = true
        scrollView.autohidesScrollers = true
        scrollView.borderType = .noBorder
        scrollView.drawsBackground = true
        scrollView.backgroundColor = .textBackgroundColor

        let textView = NSTextView()
        textView.isEditable = false
        textView.isSelectable = true
        textView.isRichText = false
        textView.allowsUndo = false
        textView.drawsBackground = false
        textView.textContainerInset = NSSize(width: 10, height: 8)
        textView.font = font
        textView.textColor = .labelColor
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = true
        textView.autoresizingMask = [.width]
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        textView.textContainer?.widthTracksTextView = false
        textView.textContainer?.containerSize = NSSize(
            width: CGFloat.greatestFiniteMagnitude,
            height: CGFloat.greatestFiniteMagnitude
        )

        scrollView.documentView = textView
        scrollView.contentView.postsBoundsChangedNotifications = true

        context.coordinator.scrollView = scrollView
        context.coordinator.textView = textView
        context.coordinator.startObservingScroll(of: scrollView)

        updateContent(scrollView: scrollView, textView: textView, coordinator: context.coordinator, forceScrollToBottom: isRunning)
        return scrollView
    }

    func updateNSView(_ scrollView: NSScrollView, context: Context) {
        guard let textView = context.coordinator.textView else { return }
        context.coordinator.isFollowingTail = $isFollowingTail

        let switchedCommand = context.coordinator.lastCommandID != commandID
        let explicitScrollRequest = context.coordinator.lastScrollToken != scrollToBottomToken
        context.coordinator.lastScrollToken = scrollToBottomToken

        let shouldForceBottom = explicitScrollRequest || (switchedCommand && isRunning)
        updateContent(
            scrollView: scrollView,
            textView: textView,
            coordinator: context.coordinator,
            forceScrollToBottom: shouldForceBottom
        )
    }

    private func updateContent(
        scrollView: NSScrollView,
        textView: NSTextView,
        coordinator: Coordinator,
        forceScrollToBottom: Bool
    ) {
        let commandChanged = coordinator.lastCommandID != commandID
        let textChanged = coordinator.lastText != text
        let fontChanged = textView.font != font

        if fontChanged {
            textView.font = font
        }

        if commandChanged || textChanged {
            let previousOrigin = scrollView.contentView.bounds.origin
            let shouldTail = forceScrollToBottom || (isRunning && isFollowingTail)

            coordinator.suppressScrollObservation = true
            textView.string = text
            textView.textColor = .labelColor
            textView.font = font
            coordinator.lastCommandID = commandID
            coordinator.lastText = text

            if shouldTail {
                textView.layoutManager?.ensureLayout(for: textView.textContainer!)
                textView.scrollToEndOfDocument(nil)
            } else if !commandChanged {
                scrollView.contentView.scroll(to: previousOrigin)
                scrollView.reflectScrolledClipView(scrollView.contentView)
            } else {
                scrollView.contentView.scroll(to: .zero)
                scrollView.reflectScrolledClipView(scrollView.contentView)
            }
            coordinator.suppressScrollObservation = false
        } else if forceScrollToBottom {
            coordinator.suppressScrollObservation = true
            textView.scrollToEndOfDocument(nil)
            coordinator.suppressScrollObservation = false
        }
    }

    final class Coordinator: NSObject {
        weak var scrollView: NSScrollView?
        weak var textView: NSTextView?
        var isFollowingTail: Binding<Bool>
        var lastCommandID: String?
        var lastText: String = ""
        var lastScrollToken: Int = 0
        var suppressScrollObservation = false
        private var observer: NSObjectProtocol?

        init(isFollowingTail: Binding<Bool>) {
            self.isFollowingTail = isFollowingTail
        }

        deinit {
            if let observer {
                NotificationCenter.default.removeObserver(observer)
            }
        }

        func startObservingScroll(of scrollView: NSScrollView) {
            if let observer {
                NotificationCenter.default.removeObserver(observer)
            }
            observer = NotificationCenter.default.addObserver(
                forName: NSView.boundsDidChangeNotification,
                object: scrollView.contentView,
                queue: .main
            ) { [weak self] _ in
                self?.handleBoundsDidChange()
            }
        }

        private func handleBoundsDidChange() {
            guard !suppressScrollObservation,
                  let scrollView,
                  let documentView = scrollView.documentView
            else { return }

            let visibleMaxY = scrollView.contentView.bounds.maxY
            let documentHeight = documentView.bounds.height
            let atBottom = documentHeight <= scrollView.contentView.bounds.height
                || visibleMaxY >= documentHeight - 24

            if isFollowingTail.wrappedValue != atBottom {
                DispatchQueue.main.async { [weak self] in
                    self?.isFollowingTail.wrappedValue = atBottom
                }
            }
        }
    }
}
