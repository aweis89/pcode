import AppKit
import Combine
import Foundation
import SwiftUI

// MARK: - Agents Store

/// Per-session live store aggregating delegated sub-agents and their nested tool calls
/// from `PCodeHostEvent` streams.
@MainActor
final class PCodeInspectorAgentsStore: ObservableObject {
    @Published private(set) var agents: [PCodeInspectorAgentItem] = []

    /// Number of active (`.running` or `.waiting`) sub-agents, used for the Inspector tab badge.
    var activeCount: Int {
        agents.reduce(0) { $0 + ($1.isActive ? 1 : 0) }
    }

    private struct NestedCallRecord {
        let callID: String
        var call: PCodePeekToolCall
    }

    private struct AgentRecord {
        let id: String
        var workerID: String?
        var role: String
        var detail: String
        var status: PCodeInspectorAgentItem.Status
        var startedAt: Date?
        var elapsedSeconds: Double?
        var nestedCalls: [NestedCallRecord]
        var reportMarkdown: String
        var pendingQuestion: String?
        var error: String?
        var lastActivityAt: Date
        var sequence: Int

        func makeItem() -> PCodeInspectorAgentItem {
            PCodeInspectorAgentItem(
                id: id,
                workerID: workerID,
                role: role,
                detail: detail,
                status: status,
                startedAt: startedAt,
                elapsedSeconds: elapsedSeconds,
                nestedCalls: nestedCalls.map(\.call),
                reportMarkdown: reportMarkdown,
                pendingQuestion: pendingQuestion,
                error: error
            )
        }
    }

    private var recordsByID: [String: AgentRecord] = [:]
    private var pendingChildrenByParent: [String: [NestedCallRecord]] = [:]
    private var childParentByCallID: [String: String] = [:]
    private var nestedIntIDByCallID: [String: Int] = [:]
    private var nextNestedIntID: Int = 1
    private var syntheticAgentCounter: Int = 0
    private var sequence: Int = 0

    init() {}

    // MARK: - Queries

    func agent(for id: String) -> PCodeInspectorAgentItem? {
        recordsByID[id]?.makeItem()
    }

    func reset() {
        resetInternal()
        agents = []
    }

    // MARK: - Event Ingestion

    func apply(_ event: PCodeHostEvent, now: Date = Date()) {
        applyInternal(event, now: now, replayingHistory: false)
    }

    /// Optional hook for future `WorkerStatus` notifications (waiting question, worker ID, status).
    func updateWorkerState(
        callID: String,
        workerID: String? = nil,
        status: PCodeInspectorAgentItem.Status? = nil,
        pendingQuestion: String? = nil,
        clearPendingQuestion: Bool = false,
        now: Date = Date()
    ) {
        guard var record = recordsByID[callID] else { return }
        if let workerID, !workerID.isEmpty {
            record.workerID = workerID
        }
        if let status {
            record.status = status
            if status != .running && status != .waiting {
                record.pendingQuestion = nil
            }
        }
        if clearPendingQuestion {
            record.pendingQuestion = nil
            if record.status == .waiting {
                record.status = .running
            }
        } else if let pendingQuestion {
            let trimmed = pendingQuestion.trimmingCharacters(in: .whitespacesAndNewlines)
            if trimmed.isEmpty {
                record.pendingQuestion = nil
                if record.status == .waiting {
                    record.status = .running
                }
            } else {
                record.pendingQuestion = trimmed
                if record.status == .running {
                    record.status = .waiting
                }
            }
        }
        sequence += 1
        record.lastActivityAt = now
        record.sequence = sequence
        recordsByID[callID] = record
        rebuildAgents()
    }

    private func applyInternal(_ event: PCodeHostEvent, now: Date, replayingHistory: Bool) {
        switch event {
        case .toolStarted(
            let callID,
            let parentCallID,
            let name,
            let detail,
            let command,
            let purpose,
            _
        ):
            guard !callID.isEmpty else { return }
            var changed = false

            if Self.isDelegateTool(name) {
                upsertAgentStarted(
                    callID: callID,
                    detail: detail,
                    purpose: purpose,
                    now: now,
                    replayingHistory: replayingHistory
                )
                changed = true
            }

            if !parentCallID.isEmpty {
                let child = makeStartedChildCall(
                    callID: callID,
                    name: name,
                    detail: (detail.isEmpty && name != "wait_for_job") ? purpose : detail,
                    command: command,
                    purpose: purpose,
                    now: now,
                    replayingHistory: replayingHistory
                )
                attachChild(child, toParent: parentCallID, now: now)
                changed = true
            }

            if changed && !replayingHistory {
                rebuildAgents()
            }

        case .toolSummary(
            let callID,
            let parentCallID,
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
            var changed = false
            let isTrackedAgent = !callID.isEmpty && recordsByID[callID] != nil

            if Self.isDelegateTool(name) || isTrackedAgent {
                let resolvedID: String
                if !callID.isEmpty {
                    resolvedID = callID
                } else {
                    syntheticAgentCounter += 1
                    resolvedID = "agent-\(syntheticAgentCounter)"
                }
                upsertAgentSummary(
                    callID: resolvedID,
                    detail: detail,
                    failed: failed,
                    elapsedSeconds: elapsedSeconds,
                    error: error,
                    result: result,
                    now: now,
                    replayingHistory: replayingHistory
                )
                changed = true
            }

            let resolvedParent = !parentCallID.isEmpty
                ? parentCallID
                : (callID.isEmpty ? "" : (childParentByCallID[callID] ?? ""))

            if !resolvedParent.isEmpty {
                let childKey: String
                if !callID.isEmpty {
                    childKey = callID
                } else {
                    syntheticAgentCounter += 1
                    childKey = "nested-\(syntheticAgentCounter)"
                }
                let child = makeSettledChildCall(
                    callID: childKey,
                    parentCallID: resolvedParent,
                    name: name,
                    detail: detail,
                    command: command,
                    failed: failed,
                    elapsedSeconds: elapsedSeconds,
                    error: error,
                    result: result,
                    isBackgroundJob: isBackgroundJob,
                    exitCode: exitCode
                )
                attachChild(child, toParent: resolvedParent, now: now)
                changed = true
            }

            if changed && !replayingHistory {
                rebuildAgents()
            }

        case .commandOutput(let callID, let command, let output):
            guard !callID.isEmpty, let parentID = childParentByCallID[callID] else { return }
            if updateChildOutput(callID: callID, parentID: parentID, command: command, output: output) {
                if !replayingHistory {
                    rebuildAgents()
                }
            }

        case .dropOutput(let callID):
            guard !callID.isEmpty, let parentID = childParentByCallID[callID] else { return }
            if settleRunningChild(callID: callID, parentID: parentID, failed: false) {
                if !replayingHistory {
                    rebuildAgents()
                }
            }

        case .turnEnded(let status):
            let settled = settleActiveAgents(for: status, now: now, replayingHistory: replayingHistory)
            if settled && !replayingHistory {
                rebuildAgents()
            }

        case .history(let events):
            resetInternal()
            for historical in events {
                applyInternal(historical, now: now, replayingHistory: true)
            }
            _ = settleActiveAgents(for: .complete, now: now, replayingHistory: true)
            rebuildAgents()

        case .conversationReset:
            resetInternal()
            agents = []

        default:
            break
        }
    }

    // MARK: - Internal Helpers

    private func resetInternal() {
        recordsByID.removeAll()
        pendingChildrenByParent.removeAll()
        childParentByCallID.removeAll()
        nestedIntIDByCallID.removeAll()
        nextNestedIntID = 1
        syntheticAgentCounter = 0
        sequence = 0
    }

    private func upsertAgentStarted(
        callID: String,
        detail: String,
        purpose: String,
        now: Date,
        replayingHistory: Bool
    ) {
        sequence += 1
        let trimmedPurpose = purpose.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedDetail = detail.trimmingCharacters(in: .whitespacesAndNewlines)
        let existing = recordsByID[callID]

        let resolvedRole: String
        if !trimmedPurpose.isEmpty {
            resolvedRole = trimmedPurpose
        } else if !trimmedDetail.isEmpty {
            resolvedRole = trimmedDetail
        } else if let existingRole = existing?.role, !existingRole.isEmpty {
            resolvedRole = existingRole
        } else {
            resolvedRole = "Sub-agent"
        }

        let resolvedDetail: String
        if !trimmedDetail.isEmpty {
            resolvedDetail = trimmedDetail
        } else {
            resolvedDetail = existing?.detail ?? ""
        }

        let workerID = existing?.workerID
            ?? Self.extractWorkerID(from: trimmedDetail)
            ?? Self.extractWorkerID(from: trimmedPurpose)

        var record = existing ?? AgentRecord(
            id: callID,
            workerID: workerID,
            role: resolvedRole,
            detail: resolvedDetail,
            status: .running,
            startedAt: replayingHistory ? nil : now,
            elapsedSeconds: nil,
            nestedCalls: [],
            reportMarkdown: "",
            pendingQuestion: nil,
            error: nil,
            lastActivityAt: now,
            sequence: sequence
        )

        record.workerID = workerID ?? record.workerID
        record.role = resolvedRole
        record.detail = resolvedDetail
        record.status = .running
        if record.startedAt == nil && !replayingHistory {
            record.startedAt = now
        }
        record.elapsedSeconds = nil
        record.error = nil
        record.lastActivityAt = now
        record.sequence = sequence

        if let buffered = pendingChildrenByParent.removeValue(forKey: callID) {
            for child in buffered {
                Self.upsertNestedCall(child, into: &record.nestedCalls)
            }
        }

        recordsByID[callID] = record
    }

    private func upsertAgentSummary(
        callID: String,
        detail: String,
        failed: Bool,
        elapsedSeconds: Double?,
        error: String,
        result: String?,
        now: Date,
        replayingHistory: Bool
    ) {
        sequence += 1
        let trimmedDetail = detail.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedError = error.trimmingCharacters(in: .whitespacesAndNewlines)
        let existing = recordsByID[callID]

        let resolvedRole: String
        if let existingRole = existing?.role, !existingRole.isEmpty, existingRole != "Sub-agent" {
            resolvedRole = existingRole
        } else if !trimmedDetail.isEmpty {
            resolvedRole = trimmedDetail
        } else if let existingRole = existing?.role, !existingRole.isEmpty {
            resolvedRole = existingRole
        } else {
            resolvedRole = "Sub-agent"
        }

        let resolvedDetail: String
        if !trimmedDetail.isEmpty {
            resolvedDetail = trimmedDetail
        } else if !trimmedError.isEmpty && (existing?.detail.isEmpty ?? true) {
            resolvedDetail = trimmedError
        } else {
            resolvedDetail = existing?.detail ?? ""
        }

        let resolvedReport: String
        if let result, !result.isEmpty {
            resolvedReport = result
        } else {
            resolvedReport = existing?.reportMarkdown ?? ""
        }

        let resolvedError: String?
        if !trimmedError.isEmpty {
            resolvedError = trimmedError
        } else if failed {
            resolvedError = existing?.error
        } else {
            resolvedError = nil
        }

        let resolvedElapsed: Double?
        if let elapsedSeconds {
            resolvedElapsed = elapsedSeconds
        } else if let startedAt = existing?.startedAt, !replayingHistory {
            resolvedElapsed = max(0, now.timeIntervalSince(startedAt))
        } else {
            resolvedElapsed = existing?.elapsedSeconds
        }

        let workerID = existing?.workerID
            ?? Self.extractWorkerID(from: trimmedDetail)
            ?? result.flatMap(Self.extractWorkerID(from:))

        var nestedCalls = existing?.nestedCalls ?? []
        if let buffered = pendingChildrenByParent.removeValue(forKey: callID) {
            for child in buffered {
                Self.upsertNestedCall(child, into: &nestedCalls)
            }
        }
        // Any nested child still marked running when its parent agent finishes is settled too.
        for idx in nestedCalls.indices where nestedCalls[idx].call.isRunning {
            nestedCalls[idx].call = Self.settling(nestedCalls[idx].call, failed: failed)
        }

        let status: PCodeInspectorAgentItem.Status = failed ? .failed : .completed

        recordsByID[callID] = AgentRecord(
            id: callID,
            workerID: workerID,
            role: resolvedRole,
            detail: resolvedDetail,
            status: status,
            startedAt: existing?.startedAt,
            elapsedSeconds: resolvedElapsed,
            nestedCalls: nestedCalls,
            reportMarkdown: resolvedReport,
            pendingQuestion: nil,
            error: resolvedError,
            lastActivityAt: now,
            sequence: sequence
        )
    }

    private func intID(for callID: String) -> Int {
        if let existing = nestedIntIDByCallID[callID] {
            return existing
        }
        let assigned = nextNestedIntID
        nextNestedIntID += 1
        nestedIntIDByCallID[callID] = assigned
        return assigned
    }

    private func existingChildCall(callID: String, parentID: String) -> PCodePeekToolCall? {
        if let record = recordsByID[parentID],
           let found = record.nestedCalls.first(where: { $0.callID == callID })
        {
            return found.call
        }
        if let buffered = pendingChildrenByParent[parentID],
           let found = buffered.first(where: { $0.callID == callID })
        {
            return found.call
        }
        return nil
    }

    private func makeStartedChildCall(
        callID: String,
        name: String,
        detail: String,
        command: String,
        purpose: String,
        now: Date,
        replayingHistory: Bool
    ) -> NestedCallRecord {
        let parentID = childParentByCallID[callID]
        let existing = parentID.flatMap { existingChildCall(callID: callID, parentID: $0) }
        let call = PCodePeekToolCall(
            id: existing?.id ?? intID(for: callID),
            name: name.isEmpty ? (existing?.name ?? "tool") : name,
            detail: detail.isEmpty ? (existing?.detail ?? "") : detail,
            failed: false,
            elapsedSeconds: nil,
            command: command.isEmpty ? (existing?.command ?? "") : command,
            output: existing?.output ?? "",
            isRunning: true,
            startedAt: existing?.startedAt ?? (replayingHistory ? nil : now),
            isBackgroundJob: existing?.isBackgroundJob ?? false,
            exitCode: existing?.exitCode,
            purpose: purpose.isEmpty ? (existing?.purpose ?? "") : purpose
        )
        return NestedCallRecord(callID: callID, call: call)
    }

    private func makeSettledChildCall(
        callID: String,
        parentCallID: String,
        name: String,
        detail: String,
        command: String,
        failed: Bool,
        elapsedSeconds: Double?,
        error: String,
        result: String?,
        isBackgroundJob: Bool,
        exitCode: Int?
    ) -> NestedCallRecord {
        let existing = existingChildCall(callID: callID, parentID: parentCallID)
        let mergedDetail: String
        if !detail.isEmpty {
            mergedDetail = detail
        } else if !error.isEmpty {
            mergedDetail = error
        } else {
            mergedDetail = existing?.detail ?? ""
        }
        let call = PCodePeekToolCall(
            id: existing?.id ?? intID(for: callID),
            name: name.isEmpty ? (existing?.name ?? "tool") : name,
            detail: mergedDetail,
            failed: failed,
            elapsedSeconds: elapsedSeconds ?? existing?.elapsedSeconds,
            command: command.isEmpty ? (existing?.command ?? "") : command,
            output: result ?? existing?.output ?? "",
            isRunning: false,
            startedAt: nil,
            isBackgroundJob: isBackgroundJob || (existing?.isBackgroundJob ?? false),
            exitCode: exitCode ?? existing?.exitCode,
            purpose: existing?.purpose ?? ""
        )
        return NestedCallRecord(callID: callID, call: call)
    }

    private func attachChild(_ child: NestedCallRecord, toParent parentCallID: String, now: Date) {
        childParentByCallID[child.callID] = parentCallID
        if var parent = recordsByID[parentCallID] {
            Self.upsertNestedCall(child, into: &parent.nestedCalls)
            sequence += 1
            parent.lastActivityAt = now
            parent.sequence = sequence
            recordsByID[parentCallID] = parent
        } else {
            var list = pendingChildrenByParent[parentCallID] ?? []
            Self.upsertNestedCall(child, into: &list)
            pendingChildrenByParent[parentCallID] = list
        }
    }

    private func updateChildOutput(
        callID: String,
        parentID: String,
        command: String,
        output: String
    ) -> Bool {
        func applyUpdate(in list: inout [NestedCallRecord]) -> Bool {
            guard let idx = list.firstIndex(where: { $0.callID == callID }) else { return false }
            let existing = list[idx].call
            list[idx].call = PCodePeekToolCall(
                id: existing.id,
                name: existing.name,
                detail: existing.detail.isEmpty ? command : existing.detail,
                failed: existing.failed,
                elapsedSeconds: existing.elapsedSeconds,
                command: command.isEmpty ? existing.command : command,
                output: output,
                isRunning: existing.isRunning,
                startedAt: existing.startedAt,
                isBackgroundJob: existing.isBackgroundJob,
                exitCode: existing.exitCode,
                purpose: existing.purpose
            )
            return true
        }

        if var parent = recordsByID[parentID] {
            if applyUpdate(in: &parent.nestedCalls) {
                recordsByID[parentID] = parent
                return true
            }
        } else if var buffered = pendingChildrenByParent[parentID] {
            if applyUpdate(in: &buffered) {
                pendingChildrenByParent[parentID] = buffered
                return true
            }
        }
        return false
    }

    private func settleRunningChild(callID: String, parentID: String, failed: Bool) -> Bool {
        if var parent = recordsByID[parentID],
           let idx = parent.nestedCalls.firstIndex(where: { $0.callID == callID }),
           parent.nestedCalls[idx].call.isRunning
        {
            parent.nestedCalls[idx].call = Self.settling(parent.nestedCalls[idx].call, failed: failed)
            recordsByID[parentID] = parent
            return true
        }
        return false
    }

    private func settleActiveAgents(
        for turnStatus: PCodePeekTurn.Status,
        now: Date,
        replayingHistory: Bool
    ) -> Bool {
        let targetStatus: PCodeInspectorAgentItem.Status
        let childFailed: Bool
        switch turnStatus {
        case .cancelled:
            targetStatus = .cancelled
            childFailed = false
        case .failed:
            targetStatus = .failed
            childFailed = true
        case .complete, .running:
            targetStatus = .completed
            childFailed = false
        }

        var changed = false
        for (id, var record) in recordsByID {
            var recordChanged = false
            if record.status == .running || record.status == .waiting {
                record.status = targetStatus
                record.pendingQuestion = nil
                if record.elapsedSeconds == nil, let startedAt = record.startedAt, !replayingHistory {
                    record.elapsedSeconds = max(0, now.timeIntervalSince(startedAt))
                }
                recordChanged = true
            }
            for idx in record.nestedCalls.indices where record.nestedCalls[idx].call.isRunning {
                record.nestedCalls[idx].call = Self.settling(record.nestedCalls[idx].call, failed: childFailed)
                recordChanged = true
            }
            if recordChanged {
                recordsByID[id] = record
                changed = true
            }
        }
        return changed
    }

    private static func upsertNestedCall(_ record: NestedCallRecord, into list: inout [NestedCallRecord]) {
        if let idx = list.firstIndex(where: { $0.callID == record.callID }) {
            list[idx] = record
        } else {
            list.append(record)
        }
    }

    private static func settling(_ call: PCodePeekToolCall, failed: Bool) -> PCodePeekToolCall {
        PCodePeekToolCall(
            id: call.id,
            name: call.name,
            detail: call.detail,
            failed: call.failed || failed,
            elapsedSeconds: call.elapsedSeconds,
            command: call.command,
            output: call.output,
            isRunning: false,
            startedAt: nil,
            isBackgroundJob: call.isBackgroundJob,
            exitCode: call.exitCode,
            purpose: call.purpose
        )
    }

    private func rebuildAgents() {
        let sorted = recordsByID.values.sorted { lhs, rhs in
            let lhsActive = lhs.status == .running || lhs.status == .waiting
            let rhsActive = rhs.status == .running || rhs.status == .waiting
            if lhsActive != rhsActive {
                return lhsActive && !rhsActive
            }
            if lhs.lastActivityAt != rhs.lastActivityAt {
                return lhs.lastActivityAt > rhs.lastActivityAt
            }
            return lhs.sequence > rhs.sequence
        }
        agents = sorted.map { $0.makeItem() }
    }

    static func isDelegateTool(_ name: String) -> Bool {
        PCodeToolGrouper.isDelegateTool(name)
    }

    /// Extracts an explicit worker handle like `w1` or `w2.1` when present in metadata or report text.
    static func extractWorkerID(from text: String) -> String? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }

        let patterns = [
            #"^\[?(w\d+(?:\.\d+)?)\]?(?:\s*[·:\-\)]|\s|$)"#,
            #"(?i)\bworker(?:_id|\s+id)?\s*[:=#]?\s*(w\d+(?:\.\d+)?)\b"#,
        ]
        let ns = trimmed as NSString
        let range = NSRange(location: 0, length: ns.length)

        for pattern in patterns {
            guard let regex = try? NSRegularExpression(pattern: pattern) else { continue }
            if let match = regex.firstMatch(in: trimmed, options: [], range: range),
               match.numberOfRanges > 1
            {
                let groupRange = match.range(at: 1)
                if groupRange.location != NSNotFound {
                    return ns.substring(with: groupRange)
                }
            }
        }
        return nil
    }
}

// MARK: - Agents Inspector View

/// Right-hand Inspector "Agents" tab showing live and completed delegated sub-agents,
/// their nested tool calls, Markdown reports, and relay-through-parent actions.
struct PCodeInspectorAgentsView: View {
    @ObservedObject var state: PCodeInspectorState
    @ObservedObject var store: PCodeInspectorAgentsStore
    let actions: PCodeInspectorActions

    @Environment(\.colorScheme) private var colorScheme
    @State private var expandedAgentIDs: Set<String> = []
    @State private var expandedToolListAgentIDs: Set<String> = []
    @State private var expandedNestedCallKeys: Set<String> = []
    @State private var selectedAgentID: String?
    @State private var draftMessages: [String: String] = [:]
    @State private var copiedReportID: String?

    init(
        state: PCodeInspectorState,
        store: PCodeInspectorAgentsStore,
        actions: PCodeInspectorActions = .noop
    ) {
        self.state = state
        self.store = store
        self.actions = actions
    }

    private var isDark: Bool { colorScheme == .dark }

    var body: some View {
        VStack(spacing: 0) {
            headerBar
            Divider()
            contentArea
        }
        .background(Color.clear)
        .onAppear {
            reconcileSelection(for: store.agents.map(\.id))
        }
        .onChange(of: state.focusedAgentID) { targetID in
            syncFocusFromState(targetID)
        }
        .onChange(of: store.agents.map(\.id)) { ids in
            reconcileSelection(for: ids)
        }
    }

    // MARK: - Relay Prompt Formatters

    static func targetLabel(for agent: PCodeInspectorAgentItem) -> String {
        if let workerID = agent.workerID?.trimmingCharacters(in: .whitespacesAndNewlines),
           !workerID.isEmpty
        {
            return workerID
        }
        let role = agent.role.trimmingCharacters(in: .whitespacesAndNewlines)
        if !role.isEmpty {
            return role
        }
        return agent.id
    }

    static func stopActionPrompt(for agent: PCodeInspectorAgentItem) -> String {
        PCodeComposerInsertion.workerStopPrompt(
            id: agent.workerID ?? "",
            role: agent.role
        )
    }

    static func messageActionPrompt(for agent: PCodeInspectorAgentItem, body: String = "") -> String {
        PCodeComposerInsertion.workerMessagePrompt(
            id: agent.workerID ?? "",
            role: agent.role,
            message: body
        )
    }

    static func answerActionPrompt(for agent: PCodeInspectorAgentItem, body: String = "") -> String {
        PCodeComposerInsertion.workerAnswerPrompt(
            id: agent.workerID ?? "",
            role: agent.role,
            answer: body
        )
    }

    // MARK: - Header

    private var headerBar: some View {
        HStack(spacing: 8) {
            Image(systemName: "person.2")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.secondary)

            Text(store.agents.isEmpty ? "Agents" : "Agents · \(store.agents.count)")
                .font(.system(size: 11.5, weight: .semibold))

            if store.activeCount > 0 {
                HStack(spacing: 4) {
                    Circle()
                        .fill(Color.accentColor)
                        .frame(width: 6, height: 6)
                    Text("\(store.activeCount) active")
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

            if !store.agents.isEmpty {
                Button {
                    let allExpanded = store.agents.allSatisfy { expandedAgentIDs.contains($0.id) }
                    withAnimation(.easeInOut(duration: 0.15)) {
                        if allExpanded {
                            expandedAgentIDs.removeAll()
                        } else {
                            expandedAgentIDs = Set(store.agents.map(\.id))
                        }
                    }
                } label: {
                    Text(store.agents.allSatisfy { expandedAgentIDs.contains($0.id) } ? "Collapse All" : "Expand All")
                        .font(.system(size: 10.5, weight: .medium))
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.borderless)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .background(Color.primary.opacity(isDark ? 0.04 : 0.035))
    }

    // MARK: - Content

    @ViewBuilder
    private var contentArea: some View {
        if store.agents.isEmpty {
            emptyState
        } else {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 8) {
                        ForEach(store.agents) { agent in
                            agentCard(agent)
                                .id(agent.id)
                        }
                    }
                    .padding(10)
                }
                .onAppear {
                    scrollToAgent(state.focusedAgentID, with: proxy, animated: false)
                }
                .onChange(of: state.focusedAgentID) { targetID in
                    scrollToAgent(targetID, with: proxy, animated: true)
                }
            }
        }
    }

    private var emptyState: some View {
        VStack(spacing: 8) {
            Image(systemName: "person.2")
                .font(.system(size: 24, weight: .light))
                .foregroundStyle(.tertiary)
            Text("No delegated agents")
                .font(.system(size: 12.5, weight: .semibold))
                .foregroundStyle(.secondary)
            Text("Sub-agents appear here when the model delegates a task.")
                .font(.system(size: 11))
                .foregroundStyle(.tertiary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 240)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(24)
    }

    // MARK: - Agent Card

    private func agentCard(_ agent: PCodeInspectorAgentItem) -> some View {
        let isExpanded = expandedAgentIDs.contains(agent.id)
        let isSelected = selectedAgentID == agent.id

        return VStack(alignment: .leading, spacing: 0) {
            agentHeaderRow(agent, isExpanded: isExpanded)

            if isExpanded {
                Divider()
                    .opacity(0.6)

                VStack(alignment: .leading, spacing: 10) {
                    if let question = agent.pendingQuestion, !question.isEmpty {
                        pendingQuestionBanner(agent: agent, question: question)
                    }

                    if agent.isActive {
                        activeActionBar(for: agent)
                    }

                    if let error = agent.error, !error.isEmpty {
                        errorBanner(error)
                    }

                    if !agent.nestedCalls.isEmpty {
                        nestedCallsSection(agentID: agent.id, calls: agent.nestedCalls)
                    }

                    if !agent.reportMarkdown.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        reportSection(agent: agent)
                    } else if agent.isActive && agent.nestedCalls.isEmpty {
                        Text("Waiting for agent activity…")
                            .font(.system(size: 11))
                            .foregroundStyle(.tertiary)
                            .padding(.vertical, 2)
                    }
                }
                .padding(10)
            }
        }
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(Color.primary.opacity(isDark ? 0.05 : 0.035))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(
                    isSelected
                        ? Color.accentColor.opacity(isDark ? 0.55 : 0.45)
                        : Color.primary.opacity(isDark ? 0.12 : 0.09),
                    lineWidth: isSelected ? 1.25 : 1
                )
        )
    }

    private func agentHeaderRow(_ agent: PCodeInspectorAgentItem, isExpanded: Bool) -> some View {
        Button {
            withAnimation(.easeInOut(duration: 0.16)) {
                selectedAgentID = agent.id
                if state.focusedAgentID != agent.id {
                    state.focusedAgentID = agent.id
                }
                if expandedAgentIDs.contains(agent.id) {
                    expandedAgentIDs.remove(agent.id)
                } else {
                    expandedAgentIDs.insert(agent.id)
                }
            }
        } label: {
            HStack(alignment: .top, spacing: 8) {
                statusIndicator(for: agent.status)
                    .padding(.top, 2)

                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Text(agent.role)
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundStyle(.primary)
                            .lineLimit(isExpanded ? 3 : 1)
                            .truncationMode(.tail)

                        if let workerID = agent.workerID, !workerID.isEmpty {
                            Text(workerID)
                                .font(.system(size: 10, weight: .medium, design: .monospaced))
                                .foregroundStyle(.secondary)
                                .padding(.horizontal, 5)
                                .padding(.vertical, 1)
                                .background(
                                    RoundedRectangle(cornerRadius: 4, style: .continuous)
                                        .fill(Color.primary.opacity(isDark ? 0.1 : 0.06))
                                )
                        }
                    }

                    if !agent.detail.isEmpty && agent.detail != agent.role {
                        Text(agent.detail)
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                            .lineLimit(isExpanded ? 3 : 1)
                            .truncationMode(.tail)
                    }

                    HStack(spacing: 8) {
                        statusBadgeText(for: agent.status)

                        if !agent.nestedCalls.isEmpty {
                            Label("\(agent.nestedCalls.count)", systemImage: "wrench.and.screwdriver")
                                .font(.system(size: 10.5))
                                .foregroundStyle(.secondary)
                                .labelStyle(.titleAndIcon)
                        }

                        elapsedLabel(for: agent)
                    }
                }

                Spacer(minLength: 4)

                Image(systemName: isExpanded ? "chevron.down" : "chevron.right")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.tertiary)
                    .padding(.top, 4)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 8)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    @ViewBuilder
    private func statusIndicator(for status: PCodeInspectorAgentItem.Status) -> some View {
        switch status {
        case .running:
            ProgressView()
                .controlSize(.mini)
                .frame(width: 14, height: 14)
        case .waiting:
            Image(systemName: "questionmark.circle.fill")
                .font(.system(size: 12.5))
                .foregroundStyle(.orange)
                .frame(width: 14, height: 14)
        case .completed:
            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 12.5))
                .foregroundStyle(.green)
                .frame(width: 14, height: 14)
        case .failed:
            Image(systemName: "xmark.circle.fill")
                .font(.system(size: 12.5))
                .foregroundStyle(.red)
                .frame(width: 14, height: 14)
        case .cancelled:
            Image(systemName: "slash.circle")
                .font(.system(size: 12.5))
                .foregroundStyle(.secondary)
                .frame(width: 14, height: 14)
        }
    }

    private func statusBadgeText(for status: PCodeInspectorAgentItem.Status) -> some View {
        let label: String
        let color: Color
        switch status {
        case .running:
            label = "Running"
            color = .accentColor
        case .waiting:
            label = "Waiting"
            color = .orange
        case .completed:
            label = "Done"
            color = .green
        case .failed:
            label = "Failed"
            color = .red
        case .cancelled:
            label = "Cancelled"
            color = .secondary
        }
        return Text(label)
            .font(.system(size: 10, weight: .semibold))
            .foregroundStyle(color)
    }

    @ViewBuilder
    private func elapsedLabel(for agent: PCodeInspectorAgentItem) -> some View {
        if agent.isActive, let startedAt = agent.startedAt {
            TimelineView(.periodic(from: .now, by: 1.0)) { context in
                let elapsed = max(0, context.date.timeIntervalSince(startedAt))
                Text(PCodePeekToolCall.formatDuration(elapsed))
                    .font(.system(size: 10.5, design: .monospaced))
                    .foregroundStyle(.tertiary)
            }
        } else if let elapsed = agent.elapsedSeconds, elapsed >= 0.05 {
            Text(PCodePeekToolCall.formatDuration(elapsed))
                .font(.system(size: 10.5, design: .monospaced))
                .foregroundStyle(.tertiary)
        }
    }

    // MARK: - Pending Question & Active Controls

    private func pendingQuestionBanner(agent: PCodeInspectorAgentItem, question: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 5) {
                Image(systemName: "questionmark.bubble.fill")
                    .font(.system(size: 11))
                    .foregroundStyle(.orange)
                Text("Question from \(Self.targetLabel(for: agent))")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.orange)
                Spacer(minLength: 4)
                Button("Answer") {
                    let draft = draftMessages[agent.id] ?? ""
                    actions.prefillWorkerAction(Self.answerActionPrompt(for: agent, body: draft))
                    draftMessages[agent.id] = ""
                }
                .font(.system(size: 10.5, weight: .semibold))
                .buttonStyle(.borderedProminent)
                .controlSize(.mini)
            }

            Text(question)
                .font(.system(size: 11.5))
                .foregroundStyle(.primary)
                .textSelection(.enabled)
        }
        .padding(8)
        .background(
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .fill(Color.orange.opacity(isDark ? 0.16 : 0.1))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .stroke(Color.orange.opacity(0.35), lineWidth: 1)
        )
    }

    private func activeActionBar(for agent: PCodeInspectorAgentItem) -> some View {
        let isWaiting = agent.status == .waiting || (agent.pendingQuestion?.isEmpty == false)
        let draftBinding = Binding<String>(
            get: { draftMessages[agent.id] ?? "" },
            set: { draftMessages[agent.id] = $0 }
        )

        return VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                TextField(
                    isWaiting ? "Answer worker…" : "Message worker…",
                    text: draftBinding
                )
                .textFieldStyle(.roundedBorder)
                .font(.system(size: 11.5))
                .onSubmit {
                    submitWorkerPrompt(for: agent, isAnswer: isWaiting)
                }

                Button {
                    submitWorkerPrompt(for: agent, isAnswer: isWaiting)
                } label: {
                    Label(isWaiting ? "Answer" : "Message", systemImage: "paperplane")
                        .font(.system(size: 11, weight: .medium))
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .help(isWaiting ? "Prefill answer in composer" : "Prefill message in composer")

                Button(role: .destructive) {
                    actions.prefillWorkerAction(Self.stopActionPrompt(for: agent))
                } label: {
                    Label("Stop", systemImage: "stop.circle")
                        .font(.system(size: 11, weight: .medium))
                }
                .buttonStyle(.bordered)
                .tint(.red)
                .controlSize(.small)
                .help("Prefill stop command in composer")
            }
        }
    }

    private func submitWorkerPrompt(for agent: PCodeInspectorAgentItem, isAnswer: Bool) {
        let body = draftMessages[agent.id] ?? ""
        let prompt = isAnswer
            ? Self.answerActionPrompt(for: agent, body: body)
            : Self.messageActionPrompt(for: agent, body: body)
        actions.prefillWorkerAction(prompt)
        draftMessages[agent.id] = ""
    }

    private func errorBanner(_ error: String) -> some View {
        HStack(alignment: .top, spacing: 6) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 11))
                .foregroundStyle(.red)
                .padding(.top, 1)
            Text(error)
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(.red)
                .textSelection(.enabled)
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .fill(Color.red.opacity(isDark ? 0.16 : 0.08))
        )
    }

    // MARK: - Nested Tool Calls

    private func nestedCallsSection(agentID: String, calls: [PCodePeekToolCall]) -> some View {
        let settledCalls = calls.filter { !$0.isRunning }
        let runningCalls = calls.filter(\.isRunning)
        let shouldFoldSettled = settledCalls.count >= 2
        let isSettledExpanded = !shouldFoldSettled || expandedToolListAgentIDs.contains(agentID)
        let failedCount = settledCalls.filter(\.failed).count

        return VStack(alignment: .leading, spacing: 4) {
            if shouldFoldSettled {
                Button {
                    withAnimation(.easeInOut(duration: 0.14)) {
                        if expandedToolListAgentIDs.contains(agentID) {
                            expandedToolListAgentIDs.remove(agentID)
                        } else {
                            expandedToolListAgentIDs.insert(agentID)
                        }
                    }
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: isSettledExpanded ? "chevron.down" : "chevron.right")
                            .font(.system(size: 9, weight: .semibold))
                            .foregroundStyle(.secondary)
                            .frame(width: 10)

                        Text("\(settledCalls.count) tool calls")
                            .font(.system(size: 11, weight: .medium))
                            .foregroundStyle(.primary)

                        let breakdown = Self.toolCallBreakdown(settledCalls)
                        if !breakdown.isEmpty {
                            Text("· \(breakdown)")
                                .font(.system(size: 10.5))
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                                .truncationMode(.tail)
                        }

                        Spacer(minLength: 4)

                        if failedCount > 0 {
                            Text("\(failedCount) failed")
                                .font(.system(size: 10, weight: .medium))
                                .foregroundStyle(.red)
                        }
                    }
                    .padding(.horizontal, 8)
                    .padding(.vertical, 5)
                    .background(
                        RoundedRectangle(cornerRadius: 6, style: .continuous)
                            .fill(Color.primary.opacity(isDark ? 0.06 : 0.04))
                    )
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            } else {
                Text("Tool Calls (\(calls.count))")
                    .font(.system(size: 10.5, weight: .semibold))
                    .foregroundStyle(.secondary)
            }

            if isSettledExpanded && !settledCalls.isEmpty {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(settledCalls) { call in
                        nestedCallRow(agentID: agentID, call: call)
                    }
                }
                .padding(.leading, shouldFoldSettled ? 8 : 0)
            }

            if !runningCalls.isEmpty {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(runningCalls) { call in
                        nestedCallRow(agentID: agentID, call: call)
                    }
                }
            }
        }
    }

    private func nestedCallRow(agentID: String, call: PCodePeekToolCall) -> some View {
        let key = "\(agentID)::\(call.id)"
        let trimmedOutput = call.output.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedCommand = call.command.trimmingCharacters(in: .whitespacesAndNewlines)
        let hasDetails = !trimmedOutput.isEmpty || !trimmedCommand.isEmpty
        let isExpanded = expandedNestedCallKeys.contains(key)

        return VStack(alignment: .leading, spacing: 0) {
            Button {
                guard hasDetails else { return }
                withAnimation(.easeInOut(duration: 0.14)) {
                    if expandedNestedCallKeys.contains(key) {
                        expandedNestedCallKeys.remove(key)
                    } else {
                        expandedNestedCallKeys.insert(key)
                    }
                }
            } label: {
                HStack(spacing: 6) {
                    nestedCallIcon(for: call)

                    Text(call.displayName)
                        .font(.system(size: 11, weight: .medium, design: .monospaced))
                        .foregroundStyle(.primary)
                        .lineLimit(1)

                    if !call.summaryText.isEmpty {
                        Text(call.summaryText)
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }

                    Spacer(minLength: 4)

                    if let elapsed = call.formattedElapsed {
                        Text(elapsed)
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(.tertiary)
                    }

                    if hasDetails {
                        Image(systemName: isExpanded ? "chevron.down" : "chevron.right")
                            .font(.system(size: 8.5, weight: .semibold))
                            .foregroundStyle(.tertiary)
                    }
                }
                .padding(.horizontal, 7)
                .padding(.vertical, 4)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if isExpanded && hasDetails {
                VStack(alignment: .leading, spacing: 4) {
                    if !trimmedCommand.isEmpty {
                        Text("$ \(trimmedCommand)")
                            .font(.system(size: 10.5, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                            .lineLimit(4)
                    }
                    if !trimmedOutput.isEmpty {
                        Text(Self.truncatedCallOutput(trimmedOutput))
                            .font(.system(size: 10.5, design: .monospaced))
                            .foregroundStyle(.primary.opacity(0.85))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 6)
                .background(Color.primary.opacity(isDark ? 0.04 : 0.025))
            }
        }
        .background(
            RoundedRectangle(cornerRadius: 5, style: .continuous)
                .fill(Color.primary.opacity(isDark ? 0.05 : 0.035))
        )
        .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
    }

    private static func toolCallBreakdown(_ calls: [PCodePeekToolCall]) -> String {
        var counts: [String: Int] = [:]
        var order: [String] = []
        for call in calls {
            let label = call.displayName
            if counts[label] == nil {
                order.append(label)
            }
            counts[label, default: 0] += 1
        }
        let sorted = order.sorted { (counts[$0] ?? 0) > (counts[$1] ?? 0) }
        return sorted.prefix(3).map { "\($0) ×\(counts[$0] ?? 0)" }.joined(separator: " · ")
    }

    private static func truncatedCallOutput(_ text: String, maxLines: Int = 18, maxChars: Int = 1200) -> String {
        var result = text
        let lines = result.split(separator: "\n", omittingEmptySubsequences: false)
        if lines.count > maxLines {
            result = lines.prefix(maxLines).joined(separator: "\n") + "\n… (\(lines.count - maxLines) more lines)"
        }
        if result.count > maxChars {
            result = String(result.prefix(maxChars)) + "…"
        }
        return result
    }

    @ViewBuilder
    private func nestedCallIcon(for call: PCodePeekToolCall) -> some View {
        if call.isRunning {
            ProgressView()
                .controlSize(.mini)
                .frame(width: 12, height: 12)
        } else if call.failed {
            Image(systemName: "xmark.circle.fill")
                .font(.system(size: 10.5))
                .foregroundStyle(.red)
                .frame(width: 12, height: 12)
        } else if call.exitedNonZero {
            Image(systemName: "exclamationmark.circle.fill")
                .font(.system(size: 10.5))
                .foregroundStyle(.orange)
                .frame(width: 12, height: 12)
        } else {
            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 10.5))
                .foregroundStyle(.green.opacity(0.85))
                .frame(width: 12, height: 12)
        }
    }

    // MARK: - Markdown Report

    private func reportSection(agent: PCodeInspectorAgentItem) -> some View {
        let isCopied = copiedReportID == agent.id

        return VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Report")
                    .font(.system(size: 10.5, weight: .semibold))
                    .foregroundStyle(.secondary)
                Spacer()
                Button {
                    actions.copyToClipboard(agent.reportMarkdown)
                    copiedReportID = agent.id
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                        if copiedReportID == agent.id {
                            copiedReportID = nil
                        }
                    }
                } label: {
                    HStack(spacing: 4) {
                        Image(systemName: isCopied ? "checkmark" : "doc.on.doc")
                            .font(.system(size: 10))
                        Text(isCopied ? "Copied" : "Copy")
                            .font(.system(size: 10.5, weight: .medium))
                    }
                    .foregroundStyle(isCopied ? .green : .secondary)
                }
                .buttonStyle(.borderless)
                .help("Copy Markdown report")
            }

            PCodeInspectorMarkdownView(markdown: agent.reportMarkdown, isDark: isDark)
                .padding(9)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(
                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .fill(Color.primary.opacity(isDark ? 0.04 : 0.025))
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .stroke(Color.primary.opacity(isDark ? 0.1 : 0.08), lineWidth: 1)
                )
        }
    }

    private func syncFocusFromState(_ targetID: String?) {
        guard let targetID,
              store.agents.contains(where: { $0.id == targetID }),
              selectedAgentID != targetID
        else { return }
        selectedAgentID = targetID
        expandedAgentIDs.insert(targetID)
    }

    private func reconcileSelection(for ids: [String]) {
        let validIDs = Set(ids)
        expandedAgentIDs = expandedAgentIDs.intersection(validIDs)
        draftMessages = draftMessages.filter { validIDs.contains($0.key) }
        if let copied = copiedReportID, !validIDs.contains(copied) {
            copiedReportID = nil
        }

        if let current = selectedAgentID, !validIDs.contains(current) {
            selectedAgentID = nil
        }

        if let focused = state.focusedAgentID, validIDs.contains(focused) {
            if selectedAgentID != focused {
                selectedAgentID = focused
                expandedAgentIDs.insert(focused)
            }
        } else if selectedAgentID == nil, let first = ids.first {
            selectedAgentID = first
            expandedAgentIDs.insert(first)
        }
    }

    private func scrollToAgent(_ targetID: String?, with proxy: ScrollViewProxy, animated: Bool) {
        guard let targetID, store.agents.contains(where: { $0.id == targetID }) else { return }
        if animated {
            withAnimation(.easeInOut(duration: 0.2)) {
                proxy.scrollTo(targetID, anchor: .top)
            }
        } else {
            proxy.scrollTo(targetID, anchor: .top)
        }
    }
}

// MARK: - Lightweight Markdown Report Renderer

private struct PCodeInspectorMarkdownView: View {
    let markdown: String
    let isDark: Bool

    private enum Block: Identifiable {
        case heading(id: Int, level: Int, text: String)
        case code(id: Int, language: String?, text: String)
        case bullet(id: Int, text: String)
        case quote(id: Int, text: String)
        case paragraph(id: Int, text: String)

        var id: Int {
            switch self {
            case .heading(let id, _, _),
                 .code(let id, _, _),
                 .bullet(let id, _),
                 .quote(let id, _),
                 .paragraph(let id, _):
                return id
            }
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(parseBlocks(markdown)) { block in
                switch block {
                case .heading(_, let level, let text):
                    inlineMarkdownText(text)
                        .font(.system(size: level <= 2 ? 13 : 12, weight: .semibold))
                        .foregroundStyle(.primary)
                        .padding(.top, 2)
                case .code(_, let language, let code):
                    VStack(alignment: .leading, spacing: 3) {
                        if let language, !language.isEmpty {
                            Text(language)
                                .font(.system(size: 9.5, weight: .medium, design: .monospaced))
                                .foregroundStyle(.tertiary)
                        }
                        ScrollView(.horizontal, showsIndicators: false) {
                            Text(code)
                                .font(.system(size: 11, design: .monospaced))
                                .foregroundStyle(.primary)
                                .textSelection(.enabled)
                        }
                    }
                    .padding(7)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(
                        RoundedRectangle(cornerRadius: 5, style: .continuous)
                            .fill(Color.primary.opacity(isDark ? 0.08 : 0.045))
                    )
                case .bullet(_, let text):
                    HStack(alignment: .top, spacing: 6) {
                        Text("•")
                            .font(.system(size: 11.5, weight: .semibold))
                            .foregroundStyle(.secondary)
                        inlineMarkdownText(text)
                            .font(.system(size: 11.5))
                            .foregroundStyle(.primary)
                    }
                case .quote(_, let text):
                    HStack(alignment: .top, spacing: 8) {
                        RoundedRectangle(cornerRadius: 1)
                            .fill(Color.secondary.opacity(0.45))
                            .frame(width: 2)
                        inlineMarkdownText(text)
                            .font(.system(size: 11.5))
                            .foregroundStyle(.secondary)
                    }
                case .paragraph(_, let text):
                    inlineMarkdownText(text)
                        .font(.system(size: 11.5))
                        .foregroundStyle(.primary)
                }
            }
        }
        .textSelection(.enabled)
    }

    private func inlineMarkdownText(_ raw: String) -> Text {
        if let attributed = try? AttributedString(
            markdown: raw,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        ) {
            return Text(attributed)
        }
        return Text(raw)
    }

    private func parseBlocks(_ source: String) -> [Block] {
        let lines = source.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        var blocks: [Block] = []
        var nextID = 0
        var idx = 0
        var paragraphBuffer: [String] = []

        func flushParagraph() {
            guard !paragraphBuffer.isEmpty else { return }
            let joined = paragraphBuffer.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
            paragraphBuffer.removeAll()
            guard !joined.isEmpty else { return }
            blocks.append(.paragraph(id: nextID, text: joined))
            nextID += 1
        }

        while idx < lines.count {
            let line = lines[idx]
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            if trimmed.hasPrefix("```") {
                flushParagraph()
                let language = String(trimmed.dropFirst(3)).trimmingCharacters(in: .whitespaces)
                idx += 1
                var codeLines: [String] = []
                while idx < lines.count && !lines[idx].trimmingCharacters(in: .whitespaces).hasPrefix("```") {
                    codeLines.append(lines[idx])
                    idx += 1
                }
                if idx < lines.count { idx += 1 }
                blocks.append(.code(id: nextID, language: language.isEmpty ? nil : language, text: codeLines.joined(separator: "\n")))
                nextID += 1
                continue
            }

            if trimmed.isEmpty {
                flushParagraph()
                idx += 1
                continue
            }

            if trimmed.hasPrefix("#") {
                let hashes = trimmed.prefix { $0 == "#" }
                let rest = trimmed.dropFirst(hashes.count)
                if hashes.count <= 6 && rest.first == " " {
                    flushParagraph()
                    blocks.append(.heading(
                        id: nextID,
                        level: hashes.count,
                        text: rest.trimmingCharacters(in: .whitespaces)
                    ))
                    nextID += 1
                    idx += 1
                    continue
                }
            }

            if trimmed.hasPrefix("- ") || trimmed.hasPrefix("* ") {
                flushParagraph()
                blocks.append(.bullet(id: nextID, text: String(trimmed.dropFirst(2))))
                nextID += 1
                idx += 1
                continue
            }

            if trimmed.hasPrefix("> ") {
                flushParagraph()
                blocks.append(.quote(id: nextID, text: String(trimmed.dropFirst(2))))
                nextID += 1
                idx += 1
                continue
            }

            paragraphBuffer.append(line)
            idx += 1
        }

        flushParagraph()
        return blocks
    }
}
