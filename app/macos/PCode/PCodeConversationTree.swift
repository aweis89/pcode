import Darwin
import Foundation

// MARK: - Conversation Tree Data Model

/// One node in a session's conversation tree, reconstructed directly from `transcript.jsonl`
/// and kept current with live `PCodeHostEvent` updates.
///
/// Mirrors Python's `pcode.conversation_tree.TurnNode` so any feature in Pcode.app
/// (the Tree Inspector tab, scroll-spy, branch previews, Peek, native `/copy`) can share
/// one branch-aware representation without extra read RPCs.
struct PCodeConversationTurnNode: Identifiable, Equatable, Sendable {
    enum Kind: String, Equatable, Sendable {
        case turn
        case compaction
        case aside
    }

    enum Status: String, Equatable, Sendable {
        case running
        case completed
        case failed
        case cancelled
        case interrupted
    }

    let id: String
    let parentID: String?
    var kind: Kind
    var prompt: String
    var response: String
    var earlierResponses: [String]
    var status: Status
    var continuation: Bool
    var isSide: Bool
    var toolCount: Int
    var failedToolCount: Int
    var editCount: Int
    /// Insertion sequence for deterministic sibling ordering.
    let order: Int

    var isSyntheticLive: Bool {
        id.hasPrefix("__live_turn_") || id.hasPrefix("history-turn-")
    }

    var promptExcerpt: String {
        Self.singleLineExcerpt(prompt, limit: 120)
    }

    var responseExcerpt: String {
        if !response.isEmpty {
            return Self.singleLineExcerpt(Self.stripInlineMarkdown(response), limit: 140)
        }
        switch status {
        case .running:
            return "Working…"
        case .completed:
            return toolCount > 0 ? "\(toolCount) tool call\(toolCount == 1 ? "" : "s")" : ""
        case .failed:
            return "Turn failed"
        case .cancelled:
            return "Cancelled"
        case .interrupted:
            return "Interrupted"
        }
    }

    static func stripInlineMarkdown(_ text: String) -> String {
        var cleaned = text
            .replacingOccurrences(of: "**", with: "")
            .replacingOccurrences(of: "__", with: "")
            .replacingOccurrences(of: "`", with: "")
        // Strip leading markdown heading markers on the first line.
        while cleaned.hasPrefix("#") || cleaned.hasPrefix(">") {
            cleaned.removeFirst()
        }
        return cleaned.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static func singleLineExcerpt(_ text: String, limit: Int) -> String {
        guard !text.isEmpty, limit > 0 else { return "" }
        let collapsed = text
            .components(separatedBy: .whitespacesAndNewlines)
            .filter { !$0.isEmpty }
            .joined(separator: " ")
        guard collapsed.count > limit else { return collapsed }
        let end = collapsed.index(collapsed.startIndex, offsetBy: max(1, limit - 1))
        return String(collapsed[..<end]).trimmingCharacters(in: .whitespaces) + "…"
    }
}

/// One flattened visual row for rendering `PCodeConversationTree` as a git-graph outline.
struct PCodeConversationTreeRow: Identifiable, Equatable, Sendable {
    enum RailSegment: Equatable, Sendable {
        case empty
        case vertical(active: Bool)
        case branch(active: Bool, isLast: Bool)
    }

    let id: String
    let node: PCodeConversationTurnNode
    /// 1-based depth along this node's branch (counts normal turns & compactions from root).
    let turnNumber: Int
    let isOnActivePath: Bool
    let isActiveHead: Bool
    /// Visual nesting depth; only increments at actual forks (linear runs stay aligned).
    let indentLevel: Int
    let railColumns: [RailSegment]
    let isForkChild: Bool
    let isLastSibling: Bool
    /// Number of hidden descendant nodes when this node is collapsed as an inactive branch root.
    let collapsedDescendantCount: Int
    /// Whether this node starts an inactive side branch that can be collapsed/expanded.
    let isCollapsibleBranchRoot: Bool
    let isCollapsed: Bool
}

/// Pure value-type conversation tree reconstructed by folding `transcript.jsonl` records.
struct PCodeConversationTree: Equatable, Sendable {
    private(set) var nodesByID: [String: PCodeConversationTurnNode] = [:]
    private(set) var insertionOrder: [String] = []
    private(set) var activeID: String?
    /// Legacy fallback for journal records that omit `run_id`.
    private var recordingID: String?
    /// Top-level `ToolStarted` call IDs already counted per turn, preventing duplicate counts.
    private var seenToolCallIDsByTurn: [String: Set<String>] = [:]

    var isEmpty: Bool { nodesByID.isEmpty }
    var turnCount: Int { nodesByID.count }

    /// Root-to-active node IDs on the currently selected branch (cycle-safe).
    func activePath() -> [String] {
        path(to: activeID)
    }

    /// Root-to-`targetID` node IDs (cycle-safe).
    func path(to targetID: String?) -> [String] {
        guard var current = targetID else { return [] }
        var result: [String] = []
        var visited: Set<String> = []
        while let node = nodesByID[current], !visited.contains(current) {
            visited.insert(current)
            result.append(current)
            guard let parent = node.parentID else { break }
            current = parent
        }
        return result.reversed()
    }

    func activePathSet() -> Set<String> {
        Set(activePath())
    }

    /// Children grouped by `parentID` (nil key = root nodes), in insertion order.
    func childrenByParent() -> [String?: [PCodeConversationTurnNode]] {
        var map: [String?: [PCodeConversationTurnNode]] = [:]
        for id in insertionOrder {
            guard let node = nodesByID[id] else { continue }
            map[node.parentID, default: []].append(node)
        }
        return map
    }

    /// Number of leaf nodes (distinct branch tips) in the tree.
    var branchTipCount: Int {
        guard !nodesByID.isEmpty else { return 0 }
        let children = childrenByParent()
        return nodesByID.keys.reduce(0) { count, id in
            count + ((children[id]?.isEmpty ?? true) ? 1 : 0)
        }
    }

    /// Whether the session has more than one linear path (forks or `/btw` branches).
    var hasBranches: Bool {
        let children = childrenByParent()
        if (children[nil]?.count ?? 0) > 1 { return true }
        for (parentID, kids) in children {
            if kids.count > 1 { return true }
            if let parentID, let parent = nodesByID[parentID], !parent.isSide, kids.contains(where: \.isSide) {
                return true
            }
            if parentID == nil, kids.contains(where: \.isSide) {
                return true
            }
        }
        return false
    }

    /// Number of inactive nodes (turns/branches not on the active path).
    var inactiveNodeCount: Int {
        let activeSet = activePathSet()
        return nodesByID.keys.reduce(0) { $0 + (activeSet.contains($1) ? 0 : 1) }
    }

    /// Consume one decoded JSON record from `transcript.jsonl`.
    mutating func consume(record: [String: Any]) {
        guard let kind = record["kind"] as? String else { return }
        switch kind {
        case "turn_started":
            guard let id = record["run_id"] as? String, !id.isEmpty else { return }
            let isMain = PCodeJournalBranches.isMainTurnStart(record)
            let parent: String? = record.keys.contains("parent_id")
                ? (record["parent_id"] as? String)
                : activeID
            if let parent, nodesByID[parent] == nil { return }
            let prompt = (record["prompt"] as? String) ?? ""
            let isAside = (record["aside"] as? Bool) == true
            let continuation = (record["continuation"] as? Bool) == true
            let node = PCodeConversationTurnNode(
                id: id,
                parentID: parent,
                kind: isAside || !isMain ? .aside : .turn,
                prompt: prompt,
                response: "",
                earlierResponses: [],
                status: .interrupted,
                continuation: continuation,
                isSide: !isMain,
                toolCount: 0,
                failedToolCount: 0,
                editCount: 0,
                order: insertionOrder.count
            )
            if nodesByID[id] == nil {
                insertionOrder.append(id)
            }
            nodesByID[id] = node
            if isMain {
                activeID = id
                recordingID = id
            }

        case "compaction_checkpoint":
            guard let id = record["node_id"] as? String, !id.isEmpty else { return }
            let parent = record["parent_id"] as? String
            if let parent, nodesByID[parent] == nil { return }
            let focus = (record["focus"] as? String) ?? ""
            let before = (record["before"] as? NSNumber)?.intValue
            let after = (record["after"] as? NSNumber)?.intValue
            let summary: String
            if let before, let after {
                summary = "Context compacted: ~\(before) → ~\(after) tokens"
            } else {
                summary = "Context compacted"
            }
            let node = PCodeConversationTurnNode(
                id: id,
                parentID: parent,
                kind: .compaction,
                prompt: focus,
                response: summary,
                earlierResponses: [],
                status: .completed,
                continuation: false,
                isSide: false,
                toolCount: 0,
                failedToolCount: 0,
                editCount: 0,
                order: insertionOrder.count
            )
            if nodesByID[id] == nil {
                insertionOrder.append(id)
            }
            nodesByID[id] = node
            activeID = id
            recordingID = nil

        case "tree_selected":
            if let id = record["node_id"] as? String, !id.isEmpty {
                if nodesByID[id] != nil {
                    activeID = id
                }
            } else if record["node_id"] == nil || record["node_id"] is NSNull {
                // `node_id: null` selects Conversation Start (empty context).
                activeID = nil
            }

        default:
            let runID = (record["run_id"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? recordingID
            guard let targetID = runID, var node = nodesByID[targetID] else { return }
            switch kind {
            case "Message":
                if let markdown = record["markdown"] as? String {
                    let trimmed = markdown.trimmingCharacters(in: .whitespacesAndNewlines)
                    if !trimmed.isEmpty, markdown != node.response {
                        if !node.response.isEmpty {
                            node.earlierResponses.append(node.response)
                        }
                        node.response = markdown
                    }
                }
            case "ToolStarted":
                let parentCall = (record["parent_call_id"] as? String) ?? ""
                if parentCall.isEmpty {
                    let callID = (record["call_id"] as? String) ?? ""
                    if callID.isEmpty || seenToolCallIDsByTurn[targetID, default: []].insert(callID).inserted {
                        node.toolCount += 1
                    }
                }
            case "ToolSummary":
                let parentCall = (record["parent_call_id"] as? String) ?? ""
                if parentCall.isEmpty, (record["failed"] as? Bool) == true {
                    node.failedToolCount += 1
                }
            case "EditCompleted":
                let parentCall = (record["parent_call_id"] as? String) ?? ""
                if parentCall.isEmpty {
                    node.editCount += 1
                }
            case "turn_completed":
                node.status = .completed
            case "turn_failed":
                node.status = .failed
            case "turn_cancelled":
                node.status = .cancelled
            default:
                return
            }
            nodesByID[targetID] = node
        }
    }

    /// Append or update a synthetic in-flight turn node while a turn streams live
    /// (before the settled record is re-read from `transcript.jsonl`).
    mutating func upsertLiveTurn(
        id: String,
        parentID: String?,
        prompt: String,
        isSide: Bool
    ) {
        if var existing = nodesByID[id] {
            existing.prompt = prompt
            existing.status = .running
            nodesByID[id] = existing
            if !isSide { activeID = id }
            return
        }
        let node = PCodeConversationTurnNode(
            id: id,
            parentID: parentID,
            kind: isSide ? .aside : .turn,
            prompt: prompt,
            response: "",
            earlierResponses: [],
            status: .running,
            continuation: false,
            isSide: isSide,
            toolCount: 0,
            failedToolCount: 0,
            editCount: 0,
            order: insertionOrder.count
        )
        insertionOrder.append(id)
        nodesByID[id] = node
        if !isSide {
            activeID = id
            recordingID = id
        }
    }

    mutating func updateLiveTurn(
        id: String,
        response: String? = nil,
        status: PCodeConversationTurnNode.Status? = nil,
        incrementTools: Bool = false,
        incrementFailedTools: Bool = false,
        incrementEdits: Bool = false
    ) {
        guard var node = nodesByID[id] else { return }
        if let response, !response.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            node.response = response
        }
        if let status {
            node.status = status
        }
        if incrementTools { node.toolCount += 1 }
        if incrementFailedTools { node.failedToolCount += 1 }
        if incrementEdits { node.editCount += 1 }
        nodesByID[id] = node
    }

    // MARK: - Journal Parsing

    /// Journal kinds that never affect the conversation tree, skipped before JSON parsing.
    private static let skippedKinds: Set<String> = [
        "TextDelta", "ThinkingDelta", "Thinking", "RunStatus", "PlanUpdated", "PlanPreview",
        "ChildText", "ChildPlan", "WorkerStatus", "CacheBust", "request", "response",
        "EditPreview", "CommandOutput", "steering", "shell_result", "note", "warning", "error",
    ]

    /// Parse a full `transcript.jsonl` buffer into a `PCodeConversationTree`.
    static func parse(journalData data: Data) -> PCodeConversationTree {
        var tree = PCodeConversationTree()
        var scan = 0
        while scan < data.count {
            let nl = data[scan...].firstIndex(of: 0x0A) ?? data.count
            let line = data[scan..<nl]
            scan = nl < data.count ? nl + 1 : data.count
            guard !line.isEmpty else { continue }
            if let kind = PCodeLiveTranscriptController.sniffJournalKind(line),
               skippedKinds.contains(kind) {
                continue
            }
            guard let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else {
                continue
            }
            tree.consume(record: obj)
        }
        return tree
    }

    /// Read and parse `transcript.jsonl` safely (matching `preloadJournal`'s `O_NOFOLLOW` + UID checks).
    static func loadFromDisk(sessionID: String) -> PCodeConversationTree? {
        guard !sessionID.isEmpty, PCode.valid(sessionID, length: 1...64) else { return nil }
        let dir = PCodeSessionInfo.root.appendingPathComponent(sessionID)
        let path = dir.appendingPathComponent("transcript.jsonl").path

        var dirStat = stat()
        guard Darwin.lstat(dir.path, &dirStat) == 0,
              (dirStat.st_mode & S_IFMT) == S_IFDIR,
              dirStat.st_uid == Darwin.getuid()
        else { return nil }

        var fileStat = stat()
        guard Darwin.lstat(path, &fileStat) == 0,
              (fileStat.st_mode & S_IFMT) == S_IFREG,
              fileStat.st_uid == Darwin.getuid()
        else { return nil }

        let fd = Darwin.open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
        guard fd >= 0 else { return nil }
        defer { Darwin.close(fd) }

        var fdStat = stat()
        guard Darwin.fstat(fd, &fdStat) == 0,
              (fdStat.st_mode & S_IFMT) == S_IFREG,
              fdStat.st_uid == Darwin.getuid()
        else { return nil }

        let size = max(0, Int(fdStat.st_size))
        guard size > 0 else { return PCodeConversationTree() }

        var data = Data(count: size)
        var totalRead = 0
        data.withUnsafeMutableBytes { raw in
            guard let base = raw.baseAddress else { return }
            while totalRead < size {
                let n = Darwin.read(fd, base.advanced(by: totalRead), size - totalRead)
                if n < 0 {
                    if errno == EINTR { continue }
                    break
                }
                if n == 0 { break }
                totalRead += n
            }
        }
        data.count = totalRead
        return parse(journalData: data)
    }

    // MARK: - Flattened Outline Projection

    /// Depth-first flattened rows for the Tree Inspector tab.
    ///
    /// Matches Python's `ConversationTree.rows()` nesting rules: linear continuations stay
    /// aligned at the same indent level, and only actual forks (or `/btw` side branches) add
    /// a visual rail column.
    func flattenedRows(
        collapsedBranchRoots: Set<String> = [],
        includeAsides: Bool = true,
        includeCompactions: Bool = true,
        activeBranchOnly: Bool = false,
        searchQuery: String = ""
    ) -> [PCodeConversationTreeRow] {
        guard !nodesByID.isEmpty else { return [] }
        let activeSet = activePathSet()
        let allChildren = childrenByParent()
        let trimmedQuery = searchQuery.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        func isVisible(_ node: PCodeConversationTurnNode) -> Bool {
            if activeBranchOnly && !activeSet.contains(node.id) { return false }
            if !includeAsides && node.kind == .aside { return false }
            if !includeCompactions && node.kind == .compaction { return false }
            return true
        }

        // Collect nearest visible descendants under `parentID`, splicing through hidden nodes
        // (so hiding Compactions or /btw never prunes turns that come after them).
        func visibleChildren(of parentID: String?) -> [PCodeConversationTurnNode] {
            var result: [PCodeConversationTurnNode] = []
            var seen: Set<String> = []
            func collect(from pid: String?) {
                for child in allChildren[pid] ?? [] {
                    guard seen.insert(child.id).inserted else { continue }
                    if isVisible(child) {
                        result.append(child)
                    } else if !activeBranchOnly || activeSet.contains(child.id) {
                        collect(from: child.id)
                    }
                }
            }
            collect(from: parentID)
            return result
        }

        // Count visible descendants under any node (for collapsed branch badges).
        var descendantCountCache: [String: Int] = [:]
        func countDescendants(of nodeID: String, visited: inout Set<String>) -> Int {
            if let cached = descendantCountCache[nodeID] { return cached }
            guard !visited.contains(nodeID) else { return 0 }
            visited.insert(nodeID)
            var total = 0
            for child in visibleChildren(of: nodeID) {
                total += 1 + countDescendants(of: child.id, visited: &visited)
            }
            descendantCountCache[nodeID] = total
            return total
        }

        func forks(_ siblings: [PCodeConversationTurnNode], parent: PCodeConversationTurnNode?) -> Bool {
            if siblings.count > 1 { return true }
            return (parent == nil || parent?.isSide == false) && siblings.contains(where: \.isSide)
        }

        var rows: [PCodeConversationTreeRow] = []
        var visited: Set<String> = []

        func walk(
            node: PCodeConversationTurnNode,
            turnDepth: Int,
            indentLevel: Int,
            ancestorRails: [PCodeConversationTreeRow.RailSegment],
            isForkChild: Bool,
            isLastSibling: Bool,
            hasActiveFollowingSibling: Bool,
            parentOnActivePath: Bool
        ) {
            guard !visited.contains(node.id) else { return }
            visited.insert(node.id)

            let onActive = activeSet.contains(node.id)
            let isHead = (node.id == activeID)
            let isInactiveBranchRoot = !onActive && parentOnActivePath
            var descVisited: Set<String> = []
            let descendants = countDescendants(of: node.id, visited: &descVisited)
            let isCollapsible = isInactiveBranchRoot && descendants > 0 && trimmedQuery.isEmpty
            let isCollapsed = isCollapsible && collapsedBranchRoots.contains(node.id)

            var rails = ancestorRails
            if isForkChild {
                rails.append(.branch(active: onActive, isLast: isLastSibling))
            }

            let row = PCodeConversationTreeRow(
                id: node.id,
                node: node,
                turnNumber: turnDepth,
                isOnActivePath: onActive,
                isActiveHead: isHead,
                indentLevel: indentLevel,
                railColumns: rails,
                isForkChild: isForkChild,
                isLastSibling: isLastSibling,
                collapsedDescendantCount: isCollapsed ? descendants : 0,
                isCollapsibleBranchRoot: isCollapsible,
                isCollapsed: isCollapsed
            )
            rows.append(row)

            if isCollapsed { return }

            let kids = visibleChildren(of: node.id)
            let childForks = forks(kids, parent: node)
            var nextAncestorRails = ancestorRails
            if isForkChild {
                nextAncestorRails.append(isLastSibling ? .empty : .vertical(active: hasActiveFollowingSibling))
            }
            let nextIndent = indentLevel + (childForks ? 1 : 0)

            for (index, child) in kids.enumerated() {
                let activeFollows = kids[(index + 1)...].contains { activeSet.contains($0.id) }
                walk(
                    node: child,
                    turnDepth: turnDepth + 1,
                    indentLevel: nextIndent,
                    ancestorRails: nextAncestorRails,
                    isForkChild: childForks,
                    isLastSibling: index == kids.count - 1,
                    hasActiveFollowingSibling: activeFollows,
                    parentOnActivePath: onActive
                )
            }
        }

        let roots = visibleChildren(of: nil)
        let rootForks = forks(roots, parent: nil)
        for (index, root) in roots.enumerated() {
            let activeFollows = roots[(index + 1)...].contains { activeSet.contains($0.id) }
            walk(
                node: root,
                turnDepth: 1,
                indentLevel: rootForks ? 1 : 0,
                ancestorRails: [],
                isForkChild: rootForks,
                isLastSibling: index == roots.count - 1,
                hasActiveFollowingSibling: activeFollows,
                parentOnActivePath: true
            )
        }

        if !trimmedQuery.isEmpty {
            rows = rows.filter { row in
                row.node.prompt.lowercased().contains(trimmedQuery)
                    || row.node.response.lowercased().contains(trimmedQuery)
                    || row.node.id.lowercased().contains(trimmedQuery)
            }
        }
        return rows
    }
}

// MARK: - Live Per-Session Store

/// Per-session observable store holding the full `PCodeConversationTree` (all branches),
/// synchronized from `transcript.jsonl` on disk and live `PCodeHostEvent` notifications.
@MainActor
final class PCodeConversationTreeStore: ObservableObject {
    @Published private(set) var tree = PCodeConversationTree()
    /// Turn ID currently visible near the top/focus of the native transcript (scroll-spy).
    @Published var visibleTurnID: String?
    /// True while the host reports an active/running turn (mutating tree actions are disabled).
    @Published private(set) var isBusy: Bool = false

    /// Called whenever the active branch's non-compaction turn IDs update, so the native
    /// transcript can backfill `turnID` on live-streamed prompt rows.
    var onActivePromptTurnIDsChanged: (([String]) -> Void)?

    @Published private(set) var sessionID: String?
    private var reloadGeneration: UInt64 = 0
    private var liveTurnCounter: Int = 0
    private var activeLiveTurnID: String?
    private var isReplayingHistory: Bool = false

    init(sessionID: String? = nil) {
        self.sessionID = sessionID
        if let sessionID, !sessionID.isEmpty {
            reloadFromDisk(sessionID: sessionID)
        }
    }

    func setSessionID(_ newID: String?) {
        let normalized = (newID?.isEmpty == false) ? newID : nil
        guard normalized != sessionID else { return }
        guard let normalized else {
            reset()
            return
        }
        sessionID = normalized
        tree = PCodeConversationTree()
        visibleTurnID = nil
        reloadFromDisk(sessionID: normalized)
    }

    func reset() {
        reloadGeneration &+= 1
        sessionID = nil
        activeLiveTurnID = nil
        tree = PCodeConversationTree()
        visibleTurnID = nil
    }

    private func notifyActivePromptTurnIDs() {
        guard let onActivePromptTurnIDsChanged else { return }
        let ids = tree.activePath().filter { id in
            guard let node = tree.nodesByID[id] else { return false }
            return node.kind != .compaction && !node.isSyntheticLive
        }
        if !ids.isEmpty {
            onActivePromptTurnIDsChanged(ids)
        }
    }

    func reloadFromDisk(sessionID overrideID: String? = nil) {
        let targetID = overrideID ?? sessionID
        guard let targetID, !targetID.isEmpty else { return }
        sessionID = targetID
        reloadGeneration &+= 1
        let gen = reloadGeneration
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let parsed = PCodeConversationTree.loadFromDisk(sessionID: targetID) else { return }
            DispatchQueue.main.async {
                guard let self, self.reloadGeneration == gen else { return }
                var next = parsed
                // Reconcile an in-flight synthetic live turn against what Python already synced to disk:
                // if disk already has the real UUID for this running turn, adopt it instead of duplicating.
                if let liveID = self.activeLiveTurnID,
                   let liveNode = self.tree.nodesByID[liveID],
                   liveNode.isSyntheticLive
                {
                    if let diskActiveID = next.activeID,
                       let diskNode = next.nodesByID[diskActiveID],
                       !liveNode.isSide,
                       (diskNode.prompt == liveNode.prompt || (self.isBusy && diskNode.status == .interrupted))
                    {
                        self.activeLiveTurnID = diskActiveID
                        next.updateLiveTurn(
                            id: diskActiveID,
                            response: liveNode.response.isEmpty ? nil : liveNode.response,
                            status: self.isBusy ? .running : diskNode.status
                        )
                    } else if next.nodesByID[liveID] == nil {
                        next.upsertLiveTurn(
                            id: liveID,
                            parentID: next.activeID,
                            prompt: liveNode.prompt,
                            isSide: liveNode.isSide
                        )
                        next.updateLiveTurn(
                            id: liveID,
                            response: liveNode.response.isEmpty ? nil : liveNode.response,
                            status: .running
                        )
                    }
                }
                if next != self.tree {
                    self.tree = next
                }
                self.notifyActivePromptTurnIDs()
            }
        }
    }

    /// Fold one host event into the live tree store.
    func apply(_ event: PCodeHostEvent) {
        switch event {
        case .conversationReset:
            reset()

        case .sessionChanged(let newID):
            if !newID.isEmpty {
                if newID != sessionID {
                    setSessionID(newID)
                } else {
                    // `/compact` emits `session_changed` with the same ID after writing a checkpoint.
                    reloadFromDisk(sessionID: newID)
                }
            }

        case .sessionState(let delta):
            if let busy = delta.busy {
                isBusy = busy
            }

        case .history(let events):
            // `history` carries only the active branch, so re-read `transcript.jsonl` to get
            // all branches; if disk isn't available yet (e.g. unit tests), fold the history
            // events directly as a fallback.
            isReplayingHistory = true
            defer { isReplayingHistory = false }
            activeLiveTurnID = nil
            if let sid = sessionID, !sid.isEmpty {
                reloadFromDisk(sessionID: sid)
            }
            if tree.isEmpty, !events.isEmpty {
                var fallback = PCodeConversationTree()
                var currentID: String?
                var seq = 0
                for item in events {
                    switch item {
                    case .turnStarted(let prompt, let isSide, let runID):
                        seq += 1
                        let id = runID.isEmpty ? "history-turn-\(seq)" : runID
                        fallback.upsertLiveTurn(id: id, parentID: fallback.activeID, prompt: prompt, isSide: isSide)
                        currentID = id
                    case .message(let markdown):
                        if let currentID {
                            fallback.updateLiveTurn(id: currentID, response: markdown)
                        }
                    case .toolStarted(_, let parentCallID, _, _, _, _, _):
                        if parentCallID.isEmpty, let currentID {
                            fallback.updateLiveTurn(id: currentID, incrementTools: true)
                        }
                    case .toolSummary(_, let parentCallID, _, _, _, let failed, _, _, _, _, _):
                        if parentCallID.isEmpty, failed, let currentID {
                            fallback.updateLiveTurn(id: currentID, incrementFailedTools: true)
                        }
                    case .editCompleted(_, let parentCallID, _, _, _, _, _, _):
                        if parentCallID.isEmpty, let currentID {
                            fallback.updateLiveTurn(id: currentID, incrementEdits: true)
                        }
                    case .turnEnded(let status):
                        if let currentID {
                            let mapped: PCodeConversationTurnNode.Status
                            switch status {
                            case .complete: mapped = .completed
                            case .failed: mapped = .failed
                            case .cancelled: mapped = .cancelled
                            case .running: mapped = .running
                            }
                            fallback.updateLiveTurn(id: currentID, status: mapped)
                        }
                    default:
                        break
                    }
                }
                tree = fallback
            }

        case .turnStarted(let prompt, let isSide, let runID):
            let id: String
            if !runID.isEmpty {
                id = runID
            } else {
                liveTurnCounter += 1
                id = "__live_turn_\(liveTurnCounter)"
            }
            activeLiveTurnID = id
            var next = tree
            next.upsertLiveTurn(id: id, parentID: next.activeID, prompt: prompt, isSide: isSide)
            tree = next

        case .message(let markdown):
            guard let id = activeLiveTurnID ?? tree.activeID else { return }
            var next = tree
            next.updateLiveTurn(id: id, response: markdown)
            tree = next

        case .toolStarted(_, let parentCallID, _, _, _, _, _):
            guard parentCallID.isEmpty, let id = activeLiveTurnID ?? tree.activeID else { return }
            var next = tree
            next.updateLiveTurn(id: id, incrementTools: true)
            tree = next

        case .toolSummary(_, let parentCallID, _, _, _, let failed, _, _, _, _, _):
            guard parentCallID.isEmpty, failed, let id = activeLiveTurnID ?? tree.activeID else { return }
            var next = tree
            next.updateLiveTurn(id: id, incrementFailedTools: true)
            tree = next

        case .editCompleted(_, let parentCallID, _, _, _, _, _, _):
            guard parentCallID.isEmpty, let id = activeLiveTurnID ?? tree.activeID else { return }
            var next = tree
            next.updateLiveTurn(id: id, incrementEdits: true)
            tree = next

        case .turnEnded(let status):
            let finishedID = activeLiveTurnID ?? tree.activeID
            activeLiveTurnID = nil
            if let finishedID {
                let mapped: PCodeConversationTurnNode.Status
                switch status {
                case .complete: mapped = .completed
                case .failed: mapped = .failed
                case .cancelled: mapped = .cancelled
                case .running: mapped = .running
                }
                var next = tree
                next.updateLiveTurn(id: finishedID, status: mapped)
                tree = next
            }
            if !isReplayingHistory, let sid = sessionID, !sid.isEmpty {
                reloadFromDisk(sessionID: sid)
            }

        default:
            break
        }
    }
}
