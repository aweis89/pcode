import Combine
import Foundation

/// pcode's `show_thinking` preference as Pcode.app's transcript reads it: `off` hides
/// thinking rows, and any other mode (`scrollback`, or the terminal-only `activity-panel`)
/// shows them. A live session's host reports its own mode; this covers everything else.
enum PCodeThinkingVisibility {
    static func isShown(mode: String) -> Bool { mode != "off" }

    static func loadDefaultFromPreferences() -> Bool {
        let url = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".config/pcode/preferences.json")
        guard let data = try? Data(contentsOf: url),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let mode = json["show_thinking"] as? String
        else { return true }
        return isShown(mode: mode)
    }
}

/// App-owned transcript presentation preferences (persisted in `UserDefaults`).
final class PCodeTranscriptPreferences: ObservableObject {
    static let shared = PCodeTranscriptPreferences()

    private enum Keys {
        static let groupTools = "PCodeTranscriptGroupTools"
        static let promptStyle = "PCodeTranscriptPromptStyle"
    }

    private let defaults: UserDefaults

    /// When `true` (default), consecutive settled tool/edit calls fold into a single
    /// expandable summary pill above any currently running tools.
    @Published var groupTools: Bool {
        didSet {
            guard groupTools != oldValue else { return }
            defaults.set(groupTools, forKey: Keys.groupTools)
        }
    }

    /// How submitted prompts are framed in the transcript.
    @Published var promptStyle: PCodePromptStyle {
        didSet {
            guard promptStyle != oldValue else { return }
            defaults.set(promptStyle.rawValue, forKey: Keys.promptStyle)
        }
    }

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        if defaults.object(forKey: Keys.groupTools) == nil {
            self.groupTools = true
        } else {
            self.groupTools = defaults.bool(forKey: Keys.groupTools)
        }
        self.promptStyle = defaults.string(forKey: Keys.promptStyle)
            .flatMap(PCodePromptStyle.init(rawValue:)) ?? .editorial
    }
}

/// Framing for prompt rows (`PCodePromptRowView`).
enum PCodePromptStyle: String, CaseIterable {
    /// Quiet top hairline divider, leading `❯` glyph, and emphasized prompt text (no box).
    case editorial
    /// Full-bleed edge-to-edge subtle structural band with top/bottom hairlines.
    case band
    /// Content-hugging compact pill that shrink-wraps short prompts.
    case pill
    /// Slim left accent rail paired with emphasized prompt text.
    case bar

    var title: String {
        switch self {
        case .editorial: return "Editorial Divider"
        case .band: return "Full-Bleed Band"
        case .pill: return "Compact Pill"
        case .bar: return "Accent Bar"
        }
    }
}

/// One settled tool or edit entry folded inside a `PCodeToolGroupPayload`.
struct PCodeToolGroupEntry: Equatable {
    enum Content: Equatable {
        case tool(PCodePeekToolCall)
        case diff(PCodeDiffPayload)
    }

    let id: String
    let content: Content
    let depth: Int

    var toolName: String {
        switch content {
        case .tool(let call): return call.name
        case .diff(let diff): return diff.displayToolName
        }
    }

    var failed: Bool {
        switch content {
        case .tool(let call): return call.failed
        case .diff(let diff): return diff.failed
        }
    }

    /// A shell call that ran and exited non-zero without failing (grep found nothing).
    var exitedNonZero: Bool {
        if case .tool(let call) = content { return call.exitedNonZero }
        return false
    }

    var elapsedSeconds: Double? {
        switch content {
        case .tool(let call): return call.elapsedSeconds
        case .diff(let diff): return diff.elapsedSeconds
        }
    }

    var isRunning: Bool {
        switch content {
        case .tool(let call): return call.isRunning
        case .diff(let diff): return diff.isPreview
        }
    }
}

/// Aggregated payload for one collapsed/expanded run of consecutive settled tools & edits.
struct PCodeToolGroupPayload: Equatable {
    struct CategoryCount: Equatable {
        let key: String
        let singular: String
        let plural: String
        let count: Int
        let failedCount: Int

        var label: String {
            count == 1 ? singular : plural
        }
    }

    let entries: [PCodeToolGroupEntry]
    let isExpanded: Bool
    /// Incremented when a newly finished tool folds into this group live, driving the absorb pulse.
    let absorbPulse: UInt64
    /// Set for a nested-call window (a sub-agent's or workflow's calls under their parent):
    /// collapsed, it shows its newest calls in at most this many one-line rows below the
    /// summary. Entries are only ever added, so its height grows to the cap and then holds,
    /// and rows below it never get pulled back up.
    let windowRows: Int?

    init(entries: [PCodeToolGroupEntry], isExpanded: Bool, absorbPulse: UInt64 = 0, windowRows: Int? = nil) {
        self.entries = entries
        self.isExpanded = isExpanded
        self.absorbPulse = absorbPulse
        self.windowRows = windowRows
    }

    var totalCount: Int { entries.count }

    var runningCount: Int {
        entries.reduce(0) { $0 + ($1.isRunning ? 1 : 0) }
    }

    /// The newest entries a collapsed window shows, oldest first; empty for other groups.
    var windowEntries: ArraySlice<PCodeToolGroupEntry> {
        guard let rows = windowRows, !isExpanded else { return [] }
        return entries.suffix(rows)
    }

    var failedCount: Int {
        entries.reduce(0) { $0 + ($1.failed ? 1 : 0) }
    }

    var succeededCount: Int {
        totalCount - failedCount - runningCount
    }

    var totalElapsedSeconds: Double? {
        var total: Double = 0
        var hasAny = false
        for entry in entries {
            if let elapsed = entry.elapsedSeconds, elapsed > 0 {
                total += elapsed
                hasAny = true
            }
        }
        return (hasAny && total >= 0.05) ? total : nil
    }

    var formattedTotalElapsed: String? {
        guard let total = totalElapsedSeconds else { return nil }
        return PCodePeekToolCall.formatDuration(total)
    }

    /// Categories sorted by count descending, preserving first-seen order on ties.
    var categoryCounts: [CategoryCount] {
        var order: [String] = []
        var counts: [String: (singular: String, plural: String, total: Int, failed: Int)] = [:]
        for entry in entries {
            let cat = PCodeToolGrouper.categoryNames(for: entry.toolName)
            if var existing = counts[cat.key] {
                existing.total += 1
                if entry.failed { existing.failed += 1 }
                counts[cat.key] = existing
            } else {
                order.append(cat.key)
                counts[cat.key] = (cat.singular, cat.plural, 1, entry.failed ? 1 : 0)
            }
        }
        let indexed = order.enumerated().compactMap { (idx, key) -> (Int, CategoryCount)? in
            guard let info = counts[key] else { return nil }
            return (idx, CategoryCount(
                key: key,
                singular: info.singular,
                plural: info.plural,
                count: info.total,
                failedCount: info.failed))
        }
        return indexed
            .sorted { lhs, rhs in
                if lhs.1.count != rhs.1.count {
                    return lhs.1.count > rhs.1.count
                }
                return lhs.0 < rhs.0
            }
            .map(\.1)
    }

    /// Plain-text summary used for accessibility, Find, copy, and height estimates.
    var plainSummaryText: String {
        let head: String
        if failedCount == 0 {
            head = totalCount == 1 ? "1 tool" : "\(totalCount) tools"
        } else if succeededCount == 0 {
            head = failedCount == 1 ? "1 failed" : "\(failedCount) failed"
        } else {
            head = "\(succeededCount) ok · \(failedCount) failed"
        }
        var lead = [head]
        if runningCount > 0 {
            lead.append("\(runningCount) running")
        }
        let parts = categoryCounts.map { cat -> String in
            if cat.failedCount > 0 {
                return "\(cat.count) \(cat.label) (\(cat.failedCount) failed)"
            }
            return "\(cat.count) \(cat.label)"
        }
        return (lead + parts).joined(separator: " · ")
    }
}

/// Pure projection engine that folds runs of consecutive settled tool/edit calls into
/// expandable `PCodeToolGroupPayload` rows while keeping active running tools visible below.
enum PCodeToolGrouper {
    struct Projection {
        let items: [PCodeTranscriptItem]
        /// Maps each folded raw item ID (e.g. `"call-123"`) to its owning `.toolGroup` item ID.
        let groupIDByRawItemID: [String: String]
    }

    /// Rows a collapsed nested-call window shows (the terminal's tool panel gives a
    /// delegate a similarly small fixed window).
    static let nestedWindowRows = 4

    static func isDelegateTool(_ name: String) -> Bool {
        switch name.lowercased() {
        case "delegate_task", "delegate", "invoke_subagent":
            return true
        default:
            return false
        }
    }

    static func isGroupable(_ item: PCodeTranscriptItem) -> Bool {
        switch item.payload {
        case .tool(let call, _):
            return !isDelegateTool(call.name) && !call.isBackgroundJob
        case .diff:
            return true
        default:
            return false
        }
    }

    static func isActive(_ item: PCodeTranscriptItem) -> Bool {
        switch item.payload {
        case .tool(let call, _):
            return call.isRunning
        case .diff(let diff, _):
            return diff.isPreview
        default:
            return false
        }
    }

    static func categoryNames(for toolName: String) -> (key: String, singular: String, plural: String) {
        let lower = toolName.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        switch lower {
        case "shell", "run_command", "bash", "start_command", "check_command", "stop_command",
             "wait_for_job", "job_output", "stop_job":
            return ("shell", "shell", "shells")
        case "edit_file", "edit":
            return ("edit", "edit", "edits")
        case "write_file", "write", "create":
            return ("write", "write", "writes")
        case "read_file", "read":
            return ("read", "read", "reads")
        case "search_files", "grep", "search":
            return ("search", "search", "searches")
        case "find_files", "list_files", "find":
            return ("find", "find", "finds")
        case "list_directory":
            return ("list", "list", "lists")
        case "create_directory":
            return ("mkdir", "mkdir", "mkdirs")
        case "file_info":
            return ("stat", "file info", "file info")
        case "read_tool_result":
            return ("result", "result", "results")
        case "grep_tool_result":
            return ("result_search", "result search", "result searches")
        case "run_code":
            return ("code", "code run", "code runs")
        case "run_workflow":
            return ("workflow", "workflow", "workflows")
        case "write_plan":
            return ("plan", "plan", "plans")
        case "search_sessions", "read_session":
            return ("session", "session", "sessions")
        case "web_search":
            return ("web_search", "web search", "web searches")
        case "get_page", "fetch_url":
            return ("fetch", "fetch", "fetches")
        case "search_tools":
            return ("tool_search", "tool search", "tool searches")
        default:
            let cleaned = lower.isEmpty ? "tool" : lower.replacingOccurrences(of: "_", with: " ")
            return (cleaned, cleaned, cleaned)
        }
    }

    /// Project canonical `rawItems` into display `items`.
    ///
    /// `heldOpenIDs` are settled items that keep their own card as if still running, so a
    /// call that finishes in milliseconds is readable before it folds into its group.
    static func project(
        rawItems: [PCodeTranscriptItem],
        groupTools: Bool,
        hideThinking: Bool = false,
        expandedGroupIDs: Set<String>,
        absorbPulses: [String: UInt64],
        heldOpenIDs: Set<String> = []
    ) -> Projection {
        let rawItems = hideThinking ? rawItems.filter { $0.kind != .thinking } : rawItems
        guard groupTools else {
            return Projection(items: rawItems, groupIDByRawItemID: [:])
        }

        var out: [PCodeTranscriptItem] = []
        out.reserveCapacity(rawItems.count)
        var groupMap: [String: String] = [:]

        func canGroup(at index: Int) -> Bool {
            let candidate = rawItems[index]
            guard isGroupable(candidate) else { return false }
            // Any tool heading nested child calls (`depth > candidate.depth`) stays its own row
            // so its children are never orphaned beneath a collapsed group pill.
            if index + 1 < rawItems.count && rawItems[index + 1].depth > candidate.depth {
                return false
            }
            return true
        }

        var i = 0
        while i < rawItems.count {
            let item = rawItems[i]
            // A parent's nested calls (a sub-agent's, a workflow's) stream into one fixed
            // window below it. Folding them like a top-level run would add a card as each
            // call starts and drop it as the call settles; this high in the transcript
            // that bounces everything below up and down while the sub-agent works.
            if i + 1 < rawItems.count && rawItems[i + 1].depth > item.depth {
                out.append(item)
                i += 1
                let childStart = i
                while i < rawItems.count && rawItems[i].depth > item.depth {
                    i += 1
                }
                appendNestedWindow(
                    children: rawItems[childStart..<i],
                    depth: item.depth + 1,
                    expandedGroupIDs: expandedGroupIDs,
                    into: &out,
                    groupMap: &groupMap)
                continue
            }
            guard canGroup(at: i) else {
                out.append(item)
                i += 1
                continue
            }

            let runDepth = item.depth
            let runStart = i
            while i < rawItems.count && canGroup(at: i) && rawItems[i].depth == runDepth {
                i += 1
            }
            let run = rawItems[runStart..<i]
            let trailingSpacing = run.last?.spacingAfter ?? 5

            var settled: [PCodeTranscriptItem] = []
            var active: [PCodeTranscriptItem] = []
            for r in run {
                if isActive(r) || heldOpenIDs.contains(r.id) {
                    active.append(r)
                } else {
                    settled.append(r)
                }
            }

            // Only active items (nothing finished yet): show full active cards as-is.
            if settled.isEmpty {
                out.append(contentsOf: active)
                continue
            }

            // Lone settled tool/edit with nothing else running in this run: keep its own single line.
            if settled.count == 1 && active.isEmpty {
                out.append(settled[0])
                continue
            }

            let groupID = "tool-group-\(run[runStart].id)"
            var entries: [PCodeToolGroupEntry] = []
            entries.reserveCapacity(settled.count)
            for s in settled {
                groupMap[s.id] = groupID
                switch s.payload {
                case .tool(let call, _):
                    entries.append(PCodeToolGroupEntry(id: s.id, content: .tool(call), depth: s.depth))
                case .diff(let diff, _):
                    entries.append(PCodeToolGroupEntry(id: s.id, content: .diff(diff), depth: s.depth))
                default:
                    break
                }
            }

            let isExpanded = expandedGroupIDs.contains(groupID)
            let pulse = absorbPulses[groupID] ?? 0
            let groupPayload = PCodeToolGroupPayload(
                entries: entries,
                isExpanded: isExpanded,
                absorbPulse: pulse)

            let groupSpacing: CGFloat
            if isExpanded {
                groupSpacing = 4
            } else if !active.isEmpty {
                groupSpacing = 5
            } else {
                groupSpacing = trailingSpacing
            }

            out.append(PCodeTranscriptItem(
                id: groupID,
                payload: .toolGroup(groupPayload),
                spacingAfter: groupSpacing,
                revision: 0,
                depth: runDepth))

            if isExpanded {
                for (idx, s) in settled.enumerated() {
                    let isLastInRun = (idx == settled.count - 1) && active.isEmpty
                    out.append(PCodeTranscriptItem(
                        id: s.id,
                        payload: s.payload,
                        spacingAfter: isLastInRun ? trailingSpacing : 4,
                        revision: s.revision,
                        depth: runDepth + 1))
                }
            }

            for (idx, a) in active.enumerated() {
                let isLastInRun = (idx == active.count - 1)
                out.append(PCodeTranscriptItem(
                    id: a.id,
                    payload: a.payload,
                    spacingAfter: isLastInRun ? trailingSpacing : 5,
                    revision: a.revision,
                    depth: runDepth))
            }
        }

        return Projection(items: out, groupIDByRawItemID: groupMap)
    }

    /// Emit one window group for a parent's nested `children`, followed by the children
    /// themselves while the user has it expanded.
    private static func appendNestedWindow(
        children: ArraySlice<PCodeTranscriptItem>,
        depth: Int,
        expandedGroupIDs: Set<String>,
        into out: inout [PCodeTranscriptItem],
        groupMap: inout [String: String]
    ) {
        guard let first = children.first else { return }
        // Keyed by the first child, which never changes: later children insert after it.
        let groupID = "tool-group-\(first.id)"
        var entries: [PCodeToolGroupEntry] = []
        var others: [PCodeTranscriptItem] = []
        for child in children {
            switch child.payload {
            case .tool(let call, _):
                entries.append(PCodeToolGroupEntry(id: child.id, content: .tool(call), depth: child.depth))
            case .diff(let diff, _):
                entries.append(PCodeToolGroupEntry(id: child.id, content: .diff(diff), depth: child.depth))
            default:
                // Only calls nest today; anything else keeps its own row after the window.
                others.append(child)
                continue
            }
            groupMap[child.id] = groupID
        }
        let trailingSpacing = children.last?.spacingAfter ?? 5
        let isExpanded = expandedGroupIDs.contains(groupID)
        if !entries.isEmpty {
            out.append(PCodeTranscriptItem(
                id: groupID,
                payload: .toolGroup(PCodeToolGroupPayload(
                    entries: entries,
                    isExpanded: isExpanded,
                    windowRows: nestedWindowRows)),
                spacingAfter: isExpanded ? 4 : trailingSpacing,
                revision: 0,
                depth: depth))
        }
        if isExpanded {
            let lastID = entries.last?.id
            for child in children where groupMap[child.id] != nil {
                out.append(PCodeTranscriptItem(
                    id: child.id,
                    payload: child.payload,
                    spacingAfter: child.id == lastID ? trailingSpacing : 4,
                    revision: child.revision,
                    depth: child.depth + 1))
            }
        }
        out.append(contentsOf: others)
    }
}
