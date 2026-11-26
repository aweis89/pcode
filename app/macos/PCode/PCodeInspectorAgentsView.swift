import AppKit
import Combine
import Darwin
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
        var parentAgentID: String?
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
                parentAgentID: parentAgentID,
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

    /// Top-level agents (those with no parent agent, or whose parent isn't tracked in this store).
    var rootAgents: [PCodeInspectorAgentItem] {
        agents.filter { item in
            guard let parentID = item.parentAgentID, !parentID.isEmpty else { return true }
            return recordsByID[parentID] == nil
        }
    }

    /// Direct child sub-agents spawned by `parentID`, in chronological order.
    func childAgents(of parentID: String) -> [PCodeInspectorAgentItem] {
        agents.filter { $0.parentAgentID == parentID }
    }

    /// Ancestor agent IDs from immediate parent upward (cycle-safe), for auto-expanding nested focus targets.
    func ancestorIDs(of agentID: String) -> [String] {
        var result: [String] = []
        var visited: Set<String> = [agentID]
        var current = recordsByID[agentID]?.parentAgentID
        while let parentID = current, !parentID.isEmpty, !visited.contains(parentID), recordsByID[parentID] != nil {
            result.append(parentID)
            visited.insert(parentID)
            current = recordsByID[parentID]?.parentAgentID
        }
        return result
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
        var changed = false
        if let workerID, !workerID.isEmpty, record.workerID != workerID {
            record.workerID = workerID
            changed = true
        }
        if let status, record.status != status {
            record.status = status
            changed = true
            if status != .running && status != .waiting {
                if record.pendingQuestion != nil {
                    record.pendingQuestion = nil
                }
                let childFailed = (status == .failed || status == .cancelled)
                for idx in record.nestedCalls.indices where record.nestedCalls[idx].call.isRunning {
                    record.nestedCalls[idx].call = Self.settling(record.nestedCalls[idx].call, failed: childFailed)
                }
            }
        }
        if clearPendingQuestion {
            if record.pendingQuestion != nil {
                record.pendingQuestion = nil
                changed = true
            }
            if record.status == .waiting {
                record.status = .running
                changed = true
            }
        } else if let pendingQuestion {
            let trimmed = pendingQuestion.trimmingCharacters(in: .whitespacesAndNewlines)
            if trimmed.isEmpty {
                if record.pendingQuestion != nil {
                    record.pendingQuestion = nil
                    changed = true
                }
                if record.status == .waiting {
                    record.status = .running
                    changed = true
                }
            } else {
                if record.pendingQuestion != trimmed {
                    record.pendingQuestion = trimmed
                    changed = true
                }
                if record.status == .running {
                    record.status = .waiting
                    changed = true
                }
            }
        }
        guard changed else { return }
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

            let isDelegate = Self.isDelegateTool(name)
            if isDelegate {
                upsertAgentStarted(
                    callID: callID,
                    parentCallID: parentCallID,
                    detail: detail,
                    purpose: purpose,
                    now: now,
                    replayingHistory: replayingHistory
                )
                changed = true
            }

            if !parentCallID.isEmpty && !isDelegate {
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

            let isDelegateOrTracked = Self.isDelegateTool(name) || isTrackedAgent
            if isDelegateOrTracked {
                let resolvedID: String
                if !callID.isEmpty {
                    resolvedID = callID
                } else {
                    syntheticAgentCounter += 1
                    resolvedID = "agent-\(syntheticAgentCounter)"
                }
                upsertAgentSummary(
                    callID: resolvedID,
                    parentCallID: parentCallID,
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

            if !resolvedParent.isEmpty && !isDelegateOrTracked {
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

        case .workerStatus(let callID, let workerID, let question, let answered, let status):
            let mappedStatus: PCodeInspectorAgentItem.Status? = {
                guard let status else { return nil }
                switch status.lowercased() {
                case "running": return .running
                case "waiting": return .waiting
                case "done", "completed": return .completed
                case "failed", "error": return .failed
                case "stopped", "cancelled", "canceled": return .cancelled
                default: return nil
                }
            }()
            updateWorkerState(
                callID: callID,
                workerID: workerID.isEmpty ? nil : workerID,
                status: mappedStatus,
                pendingQuestion: question,
                clearPendingQuestion: answered != nil,
                now: now
            )

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
        parentCallID: String,
        detail: String,
        purpose: String,
        now: Date,
        replayingHistory: Bool
    ) {
        sequence += 1
        let trimmedPurpose = purpose.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedDetail = detail.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedParent = parentCallID.trimmingCharacters(in: .whitespacesAndNewlines)
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

        let resolvedParentID: String? = !trimmedParent.isEmpty ? trimmedParent : existing?.parentAgentID

        var record = existing ?? AgentRecord(
            id: callID,
            parentAgentID: resolvedParentID,
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

        record.parentAgentID = resolvedParentID ?? record.parentAgentID

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
        parentCallID: String,
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
        let trimmedParent = parentCallID.trimmingCharacters(in: .whitespacesAndNewlines)
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

        let status: PCodeInspectorAgentItem.Status = {
            if failed && existing?.status == .cancelled {
                return .cancelled
            }
            return failed ? .failed : .completed
        }()

        let resolvedParentID: String? = !trimmedParent.isEmpty ? trimmedParent : existing?.parentAgentID

        recordsByID[callID] = AgentRecord(
            id: callID,
            parentAgentID: resolvedParentID,
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
        // Chronological oldest → newest with active agents at the bottom,
        // matching the main transcript and Changes tab.
        let sorted = recordsByID.values.sorted { lhs, rhs in
            let lhsActive = lhs.status == .running || lhs.status == .waiting
            let rhsActive = rhs.status == .running || rhs.status == .waiting
            if lhsActive != rhsActive {
                return !lhsActive && rhsActive
            }
            if lhs.lastActivityAt != rhs.lastActivityAt {
                return lhs.lastActivityAt < rhs.lastActivityAt
            }
            return lhs.sequence < rhs.sequence
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
    @State private var resolvedSpilledReports: [String: String] = [:]
    @State private var failedSpillHandles: Set<String> = []
    @State private var reportLineLimits: [String: Int] = [:]
    @State private var workerFeedbackByAgentID: [String: (token: UUID, text: String, isError: Bool)] = [:]
    @State private var inFlightWorkerAgentIDs: Set<String> = []

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

            Text(store.agents.isEmpty ? "Sub-agents" : "Sub-agents · \(store.agents.count)")
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

    private static let bottomAnchorID = "agents-bottom-anchor"

    @ViewBuilder
    private var contentArea: some View {
        if store.agents.isEmpty {
            emptyState
        } else {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 8) {
                        ForEach(store.rootAgents) { agent in
                            agentCard(agent, depth: 0)
                                .id(agent.id)
                        }
                        Color.clear
                            .frame(height: 1)
                            .id(Self.bottomAnchorID)
                    }
                    .padding(10)
                }
                .onAppear {
                    if let targetID = state.focusedAgentID {
                        scrollToAgent(targetID, with: proxy, animated: false)
                    } else {
                        DispatchQueue.main.async {
                            proxy.scrollTo(Self.bottomAnchorID, anchor: .bottom)
                        }
                    }
                }
                .onChange(of: state.focusedAgentID) { targetID in
                    scrollToAgent(targetID, with: proxy, animated: true)
                }
                .onChange(of: store.agents.count) { _ in
                    if state.focusedAgentID == nil {
                        DispatchQueue.main.async {
                            proxy.scrollTo(Self.bottomAnchorID, anchor: .bottom)
                        }
                    }
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

    private func agentCard(_ agent: PCodeInspectorAgentItem, depth: Int = 0) -> AnyView {
        let isExpanded = expandedAgentIDs.contains(agent.id)
        let isSelected = selectedAgentID == agent.id
        let childAgents = depth < 4 ? store.childAgents(of: agent.id) : []

        return AnyView(
            VStack(alignment: .leading, spacing: 0) {
                agentHeaderRow(agent, isExpanded: isExpanded, childAgentCount: childAgents.count)

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

                        if !childAgents.isEmpty {
                            VStack(alignment: .leading, spacing: 6) {
                                Text(childAgents.count == 1 ? "Sub-agent" : "Sub-agents (\(childAgents.count))")
                                    .font(.system(size: 10.5, weight: .semibold))
                                    .foregroundStyle(.secondary)

                                VStack(alignment: .leading, spacing: 6) {
                                    ForEach(childAgents) { child in
                                        agentCard(child, depth: depth + 1)
                                            .id(child.id)
                                    }
                                }
                                .padding(.leading, 8)
                                .overlay(alignment: .leading) {
                                    RoundedRectangle(cornerRadius: 1)
                                        .fill(Color.primary.opacity(isDark ? 0.16 : 0.12))
                                        .frame(width: 2)
                                }
                            }
                        }

                        if !agent.reportMarkdown.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                            reportSection(agent: agent)
                        } else if agent.isActive && agent.nestedCalls.isEmpty && childAgents.isEmpty {
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
                    .fill(Color.primary.opacity(isDark ? (depth == 0 ? 0.05 : 0.035) : (depth == 0 ? 0.035 : 0.025)))
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
        )
    }

    private func agentHeaderRow(
        _ agent: PCodeInspectorAgentItem,
        isExpanded: Bool,
        childAgentCount: Int = 0
    ) -> some View {
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

                        if childAgentCount > 0 {
                            Label("\(childAgentCount)", systemImage: "person.2")
                                .font(.system(size: 10.5))
                                .foregroundStyle(.secondary)
                        }

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
        let workerID = agent.workerID?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let canSteerDirectly = !workerID.isEmpty && !workerID.contains(".") && actions.manageWorker != nil

        return VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 5) {
                Image(systemName: "questionmark.bubble.fill")
                    .font(.system(size: 11))
                    .foregroundStyle(.orange)
                Text("Question from \(Self.targetLabel(for: agent))")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.orange)
                Spacer(minLength: 4)
                if !canSteerDirectly {
                    Button("Answer") {
                        let draft = draftMessages[agent.id] ?? ""
                        actions.prefillWorkerAction(Self.answerActionPrompt(for: agent, body: draft))
                        draftMessages[agent.id] = ""
                    }
                    .font(.system(size: 10.5, weight: .semibold))
                    .buttonStyle(.borderedProminent)
                    .controlSize(.mini)
                }
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
        let workerID = agent.workerID?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        // Nested workers (`w1.1`) live in child hubs and are not directly addressable on the top-level hub.
        let canSteerDirectly = !workerID.isEmpty && !workerID.contains(".") && actions.manageWorker != nil
        let isBusy = inFlightWorkerAgentIDs.contains(agent.id)
        let draftBinding = Binding<String>(
            get: { draftMessages[agent.id] ?? "" },
            set: { draftMessages[agent.id] = $0 }
        )
        let hasDraft = !(draftMessages[agent.id] ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty

        return VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                TextField(
                    isWaiting ? "Answer \(Self.targetLabel(for: agent))…" : "Message \(Self.targetLabel(for: agent))…",
                    text: draftBinding
                )
                .textFieldStyle(.roundedBorder)
                .font(.system(size: 11.5))
                .disabled(isBusy)
                .onSubmit {
                    guard !isBusy else { return }
                    if canSteerDirectly {
                        sendDirectWorkerAction(
                            for: agent,
                            workerID: workerID,
                            action: isWaiting ? "answer" : "message"
                        )
                    } else {
                        submitWorkerPromptViaParent(for: agent, isAnswer: isWaiting)
                    }
                }

                if canSteerDirectly {
                    Button {
                        sendDirectWorkerAction(
                            for: agent,
                            workerID: workerID,
                            action: isWaiting ? "answer" : "message"
                        )
                    } label: {
                        Label(isWaiting ? "Answer" : "Send", systemImage: "paperplane.fill")
                            .font(.system(size: 11, weight: .medium))
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
                    .disabled(isBusy || !hasDraft)
                    .help(
                        isWaiting
                            ? "Answer \(workerID) directly"
                            : "Send directly to \(workerID) before its next step"
                    )

                    Button {
                        submitWorkerPromptViaParent(for: agent, isAnswer: isWaiting)
                    } label: {
                        Label("Via Parent", systemImage: "arrow.turn.up.left")
                            .font(.system(size: 10.5, weight: .medium))
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .help("Prefill in main composer so the parent agent relays it")
                } else {
                    Button {
                        submitWorkerPromptViaParent(for: agent, isAnswer: isWaiting)
                    } label: {
                        Label(isWaiting ? "Answer" : "Message", systemImage: "paperplane")
                            .font(.system(size: 11, weight: .medium))
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .help(isWaiting ? "Prefill answer in composer" : "Prefill message in composer")
                }

                Button(role: .destructive) {
                    if canSteerDirectly {
                        sendDirectWorkerAction(for: agent, workerID: workerID, action: "stop")
                    } else {
                        actions.prefillWorkerAction(Self.stopActionPrompt(for: agent))
                    }
                } label: {
                    Label("Stop", systemImage: "stop.circle")
                        .font(.system(size: 11, weight: .medium))
                }
                .buttonStyle(.bordered)
                .tint(.red)
                .controlSize(.small)
                .disabled(isBusy)
                .help(canSteerDirectly ? "Stop \(workerID) directly" : "Prefill stop command in composer")
            }

            if let feedback = workerFeedbackByAgentID[agent.id] {
                HStack(spacing: 4) {
                    Image(systemName: feedback.isError ? "exclamationmark.triangle.fill" : "checkmark.circle.fill")
                        .font(.system(size: 10))
                    Text(feedback.text)
                        .font(.system(size: 10.5))
                        .lineLimit(2)
                }
                .foregroundStyle(feedback.isError ? Color.red : Color.green)
            }
        }
    }

    private func sendDirectWorkerAction(
        for agent: PCodeInspectorAgentItem,
        workerID: String,
        action: String
    ) {
        guard !inFlightWorkerAgentIDs.contains(agent.id) else { return }
        guard let manageWorker = actions.manageWorker else {
            submitWorkerPromptViaParent(for: agent, isAnswer: action == "answer")
            return
        }
        let body = action == "stop" ? "" : (draftMessages[agent.id] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if action != "stop" && body.isEmpty {
            return
        }
        inFlightWorkerAgentIDs.insert(agent.id)
        workerFeedbackByAgentID.removeValue(forKey: agent.id)
        let feedbackToken = UUID()
        manageWorker(action, workerID, body) { result in
            inFlightWorkerAgentIDs.remove(agent.id)
            switch result {
            case .success(let message):
                if action != "stop" {
                    let currentDraft = (draftMessages[agent.id] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                    if currentDraft == body {
                        draftMessages[agent.id] = ""
                    }
                }
                workerFeedbackByAgentID[agent.id] = (token: feedbackToken, text: message, isError: false)
                DispatchQueue.main.asyncAfter(deadline: .now() + 3.0) {
                    if workerFeedbackByAgentID[agent.id]?.token == feedbackToken {
                        workerFeedbackByAgentID.removeValue(forKey: agent.id)
                    }
                }
            case .failure(let error):
                workerFeedbackByAgentID[agent.id] = (token: feedbackToken, text: error.localizedDescription, isError: true)
            }
        }
    }

    private func submitWorkerPromptViaParent(for agent: PCodeInspectorAgentItem, isAnswer: Bool) {
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
        let spill = PCodeAgentReportResolver.parseSpill(agent.reportMarkdown)
        let loadedSpill = spill.flatMap { resolvedSpilledReports[$0.handle] }
        let spillUnavailable = spill.map { failedSpillHandles.contains($0.handle) } ?? false
        let fullMarkdown: String = {
            if let loadedSpill {
                return loadedSpill
            }
            if let spill {
                return spill.previewBody
            }
            return PCodeAgentReportResolver.stripWorkerHeader(agent.reportMarkdown)
        }()

        let currentLimit = reportLineLimits[agent.id] ?? PCodeAgentReportResolver.initialLineLimit
        let slice = PCodeAgentReportResolver.sliceLines(fullMarkdown, lineLimit: currentLimit)
        let canCollapse = currentLimit == 0 || (currentLimit > PCodeAgentReportResolver.initialLineLimit && slice.totalLines > PCodeAgentReportResolver.initialLineLimit + PCodeAgentReportResolver.lineLimitTolerance)

        return VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Text("Report")
                    .font(.system(size: 10.5, weight: .semibold))
                    .foregroundStyle(.secondary)
                if let spill, loadedSpill == nil {
                    Text(spillUnavailable ? "Preview only" : "Loading full report…")
                        .font(.system(size: 10))
                        .foregroundStyle(.tertiary)
                } else if slice.totalLines > PCodeAgentReportResolver.initialLineLimit + PCodeAgentReportResolver.lineLimitTolerance {
                    Text("\(slice.totalLines) lines")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.tertiary)
                }
                Spacer()
                Button {
                    actions.copyToClipboard(fullMarkdown)
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
                .help("Copy full Markdown report")
            }

            VStack(alignment: .leading, spacing: 0) {
                PCodeInspectorMarkdownView(markdown: slice.text, isDark: isDark)
                    .padding(9)
                    .frame(maxWidth: .infinity, alignment: .leading)

                if slice.isTruncated || canCollapse {
                    Divider()
                        .opacity(0.4)
                    HStack(spacing: 10) {
                        if slice.isTruncated {
                            let step = min(PCodeAgentReportResolver.pageSize, slice.remainingLines)
                            Button {
                                reportLineLimits[agent.id] = slice.shownLines + PCodeAgentReportResolver.pageSize
                            } label: {
                                Text(
                                    step == slice.remainingLines
                                        ? "Show \(slice.remainingLines) more lines"
                                        : "Show \(step) more lines (\(slice.remainingLines) remaining)"
                                )
                                .font(.system(size: 10.5, weight: .medium))
                            }
                            .buttonStyle(.borderless)
                            .foregroundStyle(Color.accentColor)

                            if slice.remainingLines > PCodeAgentReportResolver.pageSize {
                                Button("Show all") {
                                    reportLineLimits[agent.id] = 0
                                }
                                .font(.system(size: 10.5))
                                .buttonStyle(.borderless)
                                .foregroundStyle(.secondary)
                            }
                        }

                        Spacer(minLength: 4)

                        if canCollapse {
                            Button("Show less") {
                                reportLineLimits.removeValue(forKey: agent.id)
                            }
                            .font(.system(size: 10.5))
                            .buttonStyle(.borderless)
                            .foregroundStyle(.secondary)
                        }
                    }
                    .padding(.horizontal, 9)
                    .padding(.vertical, 6)
                    .background(Color.primary.opacity(isDark ? 0.03 : 0.02))
                }
            }
            .background(
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .fill(Color.primary.opacity(isDark ? 0.04 : 0.025))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .stroke(Color.primary.opacity(isDark ? 0.1 : 0.08), lineWidth: 1)
            )
            .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
        }
        .task(id: spill?.handle) {
            guard let handle = spill?.handle,
                  resolvedSpilledReports[handle] == nil,
                  !failedSpillHandles.contains(handle)
            else { return }
            let loaded = await Task.detached(priority: .userInitiated) {
                PCodeAgentReportResolver.loadSpilledReport(handle: handle)
            }.value
            if let loaded {
                resolvedSpilledReports[handle] = loaded
            } else {
                failedSpillHandles.insert(handle)
            }
        }
    }

    private func syncFocusFromState(_ targetID: String?) {
        guard let targetID,
              store.agents.contains(where: { $0.id == targetID }),
              selectedAgentID != targetID
        else { return }
        selectedAgentID = targetID
        expandedAgentIDs.insert(targetID)
        for ancestor in store.ancestorIDs(of: targetID) {
            expandedAgentIDs.insert(ancestor)
        }
    }

    private func reconcileSelection(for ids: [String]) {
        let validIDs = Set(ids)
        expandedAgentIDs = expandedAgentIDs.intersection(validIDs)
        draftMessages = draftMessages.filter { validIDs.contains($0.key) }
        reportLineLimits = reportLineLimits.filter { validIDs.contains($0.key) }
        workerFeedbackByAgentID = workerFeedbackByAgentID.filter { validIDs.contains($0.key) }
        let activeSpillHandles = Set(
            store.agents.compactMap { PCodeAgentReportResolver.parseSpill($0.reportMarkdown)?.handle }
        )
        resolvedSpilledReports = resolvedSpilledReports.filter { activeSpillHandles.contains($0.key) }
        failedSpillHandles = failedSpillHandles.intersection(activeSpillHandles)
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
                for ancestor in store.ancestorIDs(of: focused) {
                    expandedAgentIDs.insert(ancestor)
                }
            }
        } else if selectedAgentID == nil, let latest = ids.last {
            selectedAgentID = latest
            expandedAgentIDs.insert(latest)
            for ancestor in store.ancestorIDs(of: latest) {
                expandedAgentIDs.insert(ancestor)
            }
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
        .environment(\.openURL, OpenURLAction { url in
            guard PCodeTranscriptMarkdown.isAllowedLinkURL(url) else {
                return .discarded
            }
            if url.scheme?.lowercased() == "file" {
                if !PCodeExternalEditor.open(fileURL: url) {
                    NSSound.beep()
                }
                return .handled
            }
            return .systemAction
        })
    }

    private func inlineMarkdownText(_ raw: String) -> Text {
        if var attributed = try? AttributedString(
            markdown: raw,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        ) {
            for run in attributed.runs {
                if let url = run.link, !PCodeTranscriptMarkdown.isAllowedLinkURL(url) {
                    attributed[run.range].link = nil
                }
            }
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

/// Cleans model-facing report wrappers (`[Tool output too large ...]` / `[worker wN]`),
/// loads full spilled reports from `~/.local/state/pcode/tool-results`, and slices long reports.
enum PCodeAgentReportResolver {
    struct ParsedSpill: Equatable {
        let handle: String
        let previewBody: String
    }

    struct VisibleSlice: Equatable {
        let text: String
        let totalLines: Int
        let shownLines: Int

        var remainingLines: Int { max(0, totalLines - shownLines) }
        var isTruncated: Bool { remainingLines > 0 }
    }

    static let initialLineLimit = 40
    static let lineLimitTolerance = 10
    static let pageSize = 120

    static var defaultToolResultsDirectory: URL {
        if let xdg = ProcessInfo.processInfo.environment["XDG_STATE_HOME"], !xdg.isEmpty {
            return URL(fileURLWithPath: xdg, isDirectory: true)
                .appendingPathComponent("pcode", isDirectory: true)
                .appendingPathComponent("tool-results", isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".local", isDirectory: true)
            .appendingPathComponent("state", isDirectory: true)
            .appendingPathComponent("pcode", isDirectory: true)
            .appendingPathComponent("tool-results", isDirectory: true)
    }

    static func parseSpill(_ raw: String) -> ParsedSpill? {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.hasPrefix("[Tool output too large (") else { return nil }
        let header: String
        let remainder: String
        if let firstNewline = trimmed.firstIndex(of: "\n") {
            header = String(trimmed[..<firstNewline])
            remainder = String(trimmed[trimmed.index(after: firstNewline)...])
        } else {
            header = trimmed
            remainder = ""
        }
        guard header.hasSuffix("]"), let handle = extractHandle(from: header) else { return nil }
        return ParsedSpill(handle: handle, previewBody: stripWorkerHeader(remainder))
    }

    static func stripWorkerHeader(_ raw: String) -> String {
        let trimmedLeading = raw.drop(while: { $0 == "\n" || $0 == "\r" })
        let text = String(trimmedLeading)
        guard text.hasPrefix("[worker ") else { return raw }
        let pattern = #"^\[worker\s+w\d+(?:\.\d+)*\]\r?\n?"#
        guard let regex = try? NSRegularExpression(pattern: pattern) else { return raw }
        let ns = text as NSString
        let range = NSRange(location: 0, length: ns.length)
        guard let match = regex.firstMatch(in: text, options: [], range: range) else { return raw }
        return ns.substring(from: match.range.upperBound)
    }

    static let maxSpillBytes = 4 * 1024 * 1024

    private static let safeSegmentRegex = try? NSRegularExpression(pattern: #"^[A-Za-z0-9._-]+$"#)

    private static func isSafeSegment(_ segment: Substring) -> Bool {
        guard !segment.isEmpty, segment != ".", segment != ".." else { return false }
        guard let regex = safeSegmentRegex else { return false }
        let str = String(segment)
        let ns = str as NSString
        return regex.firstMatch(in: str, options: [], range: NSRange(location: 0, length: ns.length)) != nil
    }

    private static func isOwnedNonSymlinkDirectory(_ path: String) -> Bool {
        var st = stat()
        guard lstat(path, &st) == 0 else { return false }
        return (st.st_mode & S_IFMT) == S_IFDIR && st.st_uid == getuid()
    }

    static func loadSpilledReport(
        handle: String,
        baseDirectory: URL = defaultToolResultsDirectory
    ) -> String? {
        let cleanedHandle = handle.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanedHandle.isEmpty,
              !cleanedHandle.hasPrefix("/"),
              !cleanedHandle.contains("\\")
        else { return nil }

        let components = cleanedHandle.split(separator: "/", omittingEmptySubsequences: false)
        guard !components.isEmpty, components.allSatisfy(isSafeSegment) else { return nil }

        let basePath = baseDirectory.standardizedFileURL.path
        guard isOwnedNonSymlinkDirectory(basePath) else { return nil }

        // Verify every intermediate directory segment is a non-symlink directory owned by us.
        var current = URL(fileURLWithPath: basePath, isDirectory: true)
        if components.count > 1 {
            for dirSegment in components.dropLast() {
                current.appendPathComponent(String(dirSegment), isDirectory: true)
                guard isOwnedNonSymlinkDirectory(current.path) else { return nil }
            }
        }

        let leaf = String(components.last!)
        let candidatePaths = [
            current.appendingPathComponent(leaf, isDirectory: false).path,
            current.appendingPathComponent(leaf + ".content", isDirectory: false).path,
        ]

        for filePath in candidatePaths {
            let fd = Darwin.open(filePath, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
            guard fd >= 0 else { continue }
            defer { Darwin.close(fd) }

            var st = stat()
            guard fstat(fd, &st) == 0,
                  (st.st_mode & S_IFMT) == S_IFREG,
                  st.st_uid == getuid()
            else { continue }

            let fileSize = Int(st.st_size)
            guard fileSize >= 0, fileSize <= maxSpillBytes else { continue }

            var data = Data()
            data.reserveCapacity(min(fileSize, maxSpillBytes))
            var buffer = [UInt8](repeating: 0, count: 16_384)
            var readFailed = false
            while data.count <= maxSpillBytes {
                let n = Darwin.read(fd, &buffer, buffer.count)
                if n > 0 {
                    if data.count + n > maxSpillBytes {
                        readFailed = true
                        break
                    }
                    data.append(contentsOf: buffer.prefix(n))
                } else if n == 0 {
                    break
                } else if errno == EINTR {
                    continue
                } else {
                    readFailed = true
                    break
                }
            }
            guard !readFailed, let text = String(data: data, encoding: .utf8) else { continue }
            return stripWorkerHeader(text)
        }
        return nil
    }

    static func sliceLines(_ markdown: String, lineLimit: Int) -> VisibleSlice {
        let lines = markdown.split(separator: "\n", omittingEmptySubsequences: false)
        let total = lines.count
        if lineLimit <= 0 || total <= lineLimit + lineLimitTolerance {
            return VisibleSlice(text: markdown, totalLines: total, shownLines: total)
        }
        let clipped = lines.prefix(lineLimit).joined(separator: "\n")
        return VisibleSlice(text: clipped, totalLines: total, shownLines: lineLimit)
    }

    private static func extractHandle(from header: String) -> String? {
        let pattern = #"stored to handle '([^']+)'"#
        guard let regex = try? NSRegularExpression(pattern: pattern) else { return nil }
        let ns = header as NSString
        let range = NSRange(location: 0, length: ns.length)
        guard let match = regex.firstMatch(in: header, options: [], range: range),
              match.numberOfRanges > 1
        else { return nil }
        let capture = match.range(at: 1)
        guard capture.location != NSNotFound else { return nil }
        let handle = ns.substring(with: capture).trimmingCharacters(in: .whitespacesAndNewlines)
        return handle.isEmpty ? nil : handle
    }
}
