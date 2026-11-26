import Combine
import Foundation

/// Per-session store for file diffs across Session, Working Tree, Branch, and Single Edit scopes.
@MainActor
final class PCodeSessionChangesStore: ObservableObject {
    typealias HunkParser = @Sendable (String) -> [PCodeDiffHunk]

    @Published private(set) var sessionFiles: [PCodeFileDiff] = []
    @Published private(set) var workingTreeFiles: [PCodeFileDiff] = []
    @Published private(set) var branchFiles: [PCodeFileDiff] = []
    @Published private(set) var isLoadingGit: Bool = false
    @Published private(set) var gitError: String? = nil
    @Published private(set) var hasGitRepo: Bool = false
    @Published private(set) var baseBranchName: String? = nil

    private(set) var sessionDirectory: String?
    private(set) var activeScope: PCodeDiffScope = .session

    private let parseHunks: HunkParser
    private let gitProvider: PCodeGitDiffProvider
    private let gitDebounceNanoseconds: UInt64

    private struct PreviewEntry {
        let callID: String
        let path: String
        let absolutePath: String?
        let text: String
        let receivedAt: Date
        let sequence: Int
    }

    private struct CompletedEditEntry {
        let callID: String
        let parentCallID: String
        let path: String
        let absolutePath: String?
        let operation: String
        let kind: PCodeFileChangeKind
        let patch: String
        let added: Int
        let removed: Int
        let truncated: Bool
        let hunks: [PCodeDiffHunk]
        let completedAt: Date
        let sequence: Int
    }

    private struct FileBucket {
        var path: String
        var absolutePath: String?
        var completedEdits: [CompletedEditEntry] = []
        var activePreviews: [String: PreviewEntry] = [:]
        var lastModifiedAt: Date
        var lastSequence: Int
    }

    private final class TaskBox: @unchecked Sendable {
        private let lock = NSLock()
        private var debounceTask: Task<Void, Never>?
        private var cancelRefresh: (@Sendable () -> Void)?

        func replaceDebounce(_ task: Task<Void, Never>?) {
            lock.lock()
            let old = debounceTask
            debounceTask = task
            lock.unlock()
            old?.cancel()
        }

        func replaceRefresh(_ cancel: (@Sendable () -> Void)?) {
            lock.lock()
            let old = cancelRefresh
            cancelRefresh = cancel
            lock.unlock()
            old?()
        }

        func cancelAll() {
            lock.lock()
            let oldDebounce = debounceTask
            let oldRefresh = cancelRefresh
            debounceTask = nil
            cancelRefresh = nil
            lock.unlock()
            oldDebounce?.cancel()
            oldRefresh?()
        }
    }

    private var bucketsByPath: [String: FileBucket] = [:]
    private var singleEditsByCallID: [String: PCodeFileDiff] = [:]
    private var previewPathByCallID: [String: String] = [:]
    private var sequence: Int = 0
    private var syntheticEditCounter: Int = 0
    private var gitRefreshGeneration: Int = 0
    private let tasks = TaskBox()

    init(
        sessionDirectory: String? = nil,
        parseHunks: @escaping HunkParser = { PCodeFallbackPatchParser.parseHunks($0) },
        gitProvider: PCodeGitDiffProvider? = nil,
        gitDebounceNanoseconds: UInt64 = 300_000_000
    ) {
        self.sessionDirectory = sessionDirectory
        self.parseHunks = parseHunks
        self.gitProvider = gitProvider ?? PCodeGitDiffProvider(parseHunks: parseHunks)
        self.gitDebounceNanoseconds = gitDebounceNanoseconds
    }

    deinit {
        tasks.cancelAll()
    }

    // MARK: - Configuration

    func setSessionDirectory(_ directory: String?) {
        guard sessionDirectory != directory else { return }
        sessionDirectory = directory
        if activeScope == .workingTree || activeScope == .branch {
            scheduleGitRefresh(for: activeScope)
        }
    }

    func setActiveScope(_ scope: PCodeDiffScope, triggerRefresh: Bool = true) {
        activeScope = scope
        if triggerRefresh && (scope == .workingTree || scope == .branch) {
            scheduleGitRefresh(for: scope)
        }
    }

    // MARK: - Event ingestion

    func apply(_ event: PCodeHostEvent, now: Date = Date()) {
        applyInternal(event, now: now, replayingHistory: false)
    }

    private func applyInternal(_ event: PCodeHostEvent, now: Date, replayingHistory: Bool) {
        switch event {
        case .editPreview(let callID, let path, let text, let kind):
            handleEditPreview(callID: callID, path: path, text: text, kind: kind, now: now)
            if !replayingHistory {
                rebuildSessionFiles()
            }

        case .editCompleted(
            let callID,
            let parentCallID,
            let path,
            let operation,
            let patch,
            let added,
            let removed,
            let truncated
        ):
            handleEditCompleted(
                callID: callID,
                parentCallID: parentCallID,
                path: path,
                operation: operation,
                patch: patch,
                added: added,
                removed: removed,
                truncated: truncated,
                now: now
            )
            if !replayingHistory {
                rebuildSessionFiles()
            }

        case .history(let events):
            resetSessionState()
            for historicalEvent in events {
                applyInternal(historicalEvent, now: now, replayingHistory: true)
            }
            settleStreamingPreviews()
            rebuildSessionFiles()

        case .conversationReset:
            resetSessionState()
            rebuildSessionFiles()

        case .toolSummary:
            if !replayingHistory && (activeScope == .workingTree || activeScope == .branch) {
                scheduleGitRefresh(for: activeScope)
            }

        case .turnEnded:
            let settled = settleStreamingPreviews()
            if settled && !replayingHistory {
                rebuildSessionFiles()
            }
            if !replayingHistory && (activeScope == .workingTree || activeScope == .branch) {
                scheduleGitRefresh(for: activeScope)
            }

        default:
            break
        }
    }

    // MARK: - Queries

    func files(for scope: PCodeDiffScope) -> [PCodeFileDiff] {
        switch scope {
        case .session:
            return sessionFiles
        case .workingTree:
            return workingTreeFiles
        case .branch:
            return branchFiles
        case .singleEdit(let callID):
            if let edit = singleEdit(for: callID) {
                return [edit]
            }
            return []
        }
    }

    func singleEdit(for callID: String) -> PCodeFileDiff? {
        if let completed = singleEditsByCallID[callID] {
            return completed
        }
        if let previewPath = previewPathByCallID[callID],
           let preview = bucketsByPath[previewPath]?.activePreviews[callID]
        {
            return makePreviewOnlyDiff(from: preview)
        }
        return nil
    }

    func sidebarFiles(limit: Int = 6) -> (visible: [PCodeFileDiff], overflowCount: Int) {
        let clampedLimit = max(0, limit)
        if sessionFiles.count <= clampedLimit {
            return (sessionFiles, 0)
        }
        return (Array(sessionFiles.prefix(clampedLimit)), sessionFiles.count - clampedLimit)
    }

    var sessionStats: (added: Int, removed: Int) {
        totalStats(for: .session)
    }

    func totalStats(for scope: PCodeDiffScope = .session) -> (added: Int, removed: Int) {
        var added = 0
        var removed = 0
        for file in files(for: scope) {
            added += file.added
            removed += file.removed
        }
        return (added, removed)
    }

    // MARK: - Async Git Refresh

    func cancelPendingGitRefresh() {
        tasks.cancelAll()
        isLoadingGit = false
    }

    func scheduleGitRefresh(for scope: PCodeDiffScope? = nil) {
        let targetScope = scope ?? activeScope
        guard targetScope == .workingTree || targetScope == .branch else { return }

        let delay = gitDebounceNanoseconds
        let debounce = Task { [weak self] in
            if delay > 0 {
                try? await Task.sleep(nanoseconds: delay)
            }
            guard !Task.isCancelled, let self else { return }
            await self.refreshGit(for: targetScope)
        }
        tasks.replaceDebounce(debounce)
    }

    func refreshGit(for scope: PCodeDiffScope? = nil) async {
        let targetScope = scope ?? activeScope
        guard targetScope == .workingTree || targetScope == .branch else { return }

        guard let directory = sessionDirectory, !directory.isEmpty else {
            hasGitRepo = false
            gitError = nil
            workingTreeFiles = []
            branchFiles = []
            baseBranchName = nil
            isLoadingGit = false
            return
        }

        gitRefreshGeneration += 1
        let generation = gitRefreshGeneration
        isLoadingGit = true

        let provider = gitProvider
        let work = Task<PCodeGitDiffResult, Never> {
            switch targetScope {
            case .workingTree:
                return await provider.fetchWorkingTree(in: directory)
            case .branch:
                return await provider.fetchBranchDiff(in: directory)
            case .session, .singleEdit:
                return .notAGitRepo
            }
        }
        tasks.replaceRefresh { work.cancel() }

        let result = await work.value

        guard !Task.isCancelled, generation == gitRefreshGeneration else {
            return
        }

        isLoadingGit = false
        hasGitRepo = result.hasGitRepo
        gitError = result.error

        if !result.hasGitRepo {
            workingTreeFiles = []
            branchFiles = []
            baseBranchName = nil
            return
        }

        switch targetScope {
        case .workingTree:
            workingTreeFiles = result.files
        case .branch:
            branchFiles = result.files
            baseBranchName = result.baseBranchName
        case .session, .singleEdit:
            break
        }
    }

    // MARK: - Session folding internals

    private func resetSessionState() {
        bucketsByPath.removeAll()
        singleEditsByCallID.removeAll()
        previewPathByCallID.removeAll()
        sequence = 0
        syntheticEditCounter = 0
    }

    private func handleEditPreview(
        callID: String,
        path: String,
        text: String,
        kind: String,
        now: Date
    ) {
        guard !callID.isEmpty else { return }

        // Empty path clears a streaming preview for this callID.
        if path.isEmpty {
            removePreview(callID: callID)
            return
        }

        // Ignore non-file previews such as `run_code` / `run_workflow` snippets (`kind == "code"`).
        guard kind == "edit" else { return }

        let normalized = PCodePathNormalizer.normalize(rawPath: path, sessionDirectory: sessionDirectory)
        guard !normalized.displayPath.isEmpty else { return }

        if let previousPath = previewPathByCallID[callID], previousPath != normalized.displayPath {
            removePreview(callID: callID)
        }

        sequence += 1
        let preview = PreviewEntry(
            callID: callID,
            path: normalized.displayPath,
            absolutePath: normalized.absolutePath,
            text: text,
            receivedAt: now,
            sequence: sequence
        )

        var bucket = bucketsByPath[normalized.displayPath] ?? FileBucket(
            path: normalized.displayPath,
            absolutePath: normalized.absolutePath,
            lastModifiedAt: now,
            lastSequence: sequence
        )
        if let abs = normalized.absolutePath {
            bucket.absolutePath = abs
        }
        bucket.activePreviews[callID] = preview
        bucket.lastModifiedAt = now
        bucket.lastSequence = sequence
        bucketsByPath[normalized.displayPath] = bucket
        previewPathByCallID[callID] = normalized.displayPath
    }

    private func handleEditCompleted(
        callID: String,
        parentCallID: String,
        path: String,
        operation: String,
        patch: String,
        added: Int,
        removed: Int,
        truncated: Bool,
        now: Date
    ) {
        guard !path.isEmpty else { return }

        let normalized = PCodePathNormalizer.normalize(rawPath: path, sessionDirectory: sessionDirectory)
        guard !normalized.displayPath.isEmpty else { return }

        let effectiveCallID: String
        if callID.isEmpty {
            syntheticEditCounter += 1
            effectiveCallID = "edit-\(syntheticEditCounter)"
        } else {
            effectiveCallID = callID
        }

        // Clear any matching preview by callID, plus any active preview on the same file
        // (runtime streaming previews use synthetic `edit-preview:<index>` IDs).
        removePreview(callID: effectiveCallID)
        clearPreviews(forPath: normalized.displayPath)

        sequence += 1
        let parsedHunks = parseHunks(patch)
        let changeKind = PCodeFileChangeKind(operation: operation)

        let entry = CompletedEditEntry(
            callID: effectiveCallID,
            parentCallID: parentCallID,
            path: normalized.displayPath,
            absolutePath: normalized.absolutePath,
            operation: operation,
            kind: changeKind,
            patch: patch,
            added: added,
            removed: removed,
            truncated: truncated,
            hunks: parsedHunks,
            completedAt: now,
            sequence: sequence
        )

        let singleDiff = PCodeFileDiff(
            id: effectiveCallID,
            path: normalized.displayPath,
            absolutePath: normalized.absolutePath,
            kind: changeKind,
            added: added,
            removed: removed,
            hunks: parsedHunks,
            rawPatch: patch,
            lastModifiedAt: now,
            editCount: 1,
            isStreaming: false,
            truncated: truncated,
            callIDs: [effectiveCallID]
        )
        singleEditsByCallID[effectiveCallID] = singleDiff

        var bucket = bucketsByPath[normalized.displayPath] ?? FileBucket(
            path: normalized.displayPath,
            absolutePath: normalized.absolutePath,
            lastModifiedAt: now,
            lastSequence: sequence
        )
        if let abs = normalized.absolutePath {
            bucket.absolutePath = abs
        }

        if let existingIndex = bucket.completedEdits.firstIndex(where: { $0.callID == effectiveCallID }) {
            bucket.completedEdits[existingIndex] = entry
        } else {
            bucket.completedEdits.append(entry)
        }
        bucket.lastModifiedAt = now
        bucket.lastSequence = sequence
        bucketsByPath[normalized.displayPath] = bucket
    }

    private func removePreview(callID: String) {
        guard let oldPath = previewPathByCallID.removeValue(forKey: callID) else { return }
        guard var bucket = bucketsByPath[oldPath] else { return }
        bucket.activePreviews.removeValue(forKey: callID)
        if bucket.completedEdits.isEmpty && bucket.activePreviews.isEmpty {
            bucketsByPath.removeValue(forKey: oldPath)
        } else {
            bucketsByPath[oldPath] = bucket
        }
    }

    private func clearPreviews(forPath path: String) {
        guard var bucket = bucketsByPath[path], !bucket.activePreviews.isEmpty else { return }
        for previewCallID in bucket.activePreviews.keys {
            previewPathByCallID.removeValue(forKey: previewCallID)
        }
        bucket.activePreviews.removeAll()
        if bucket.completedEdits.isEmpty {
            bucketsByPath.removeValue(forKey: path)
        } else {
            bucketsByPath[path] = bucket
        }
    }

    @discardableResult
    private func settleStreamingPreviews() -> Bool {
        guard !previewPathByCallID.isEmpty else { return false }
        let pathsToTouch = Set(previewPathByCallID.values)
        previewPathByCallID.removeAll()
        for path in pathsToTouch {
            guard var bucket = bucketsByPath[path] else { continue }
            bucket.activePreviews.removeAll()
            if bucket.completedEdits.isEmpty {
                bucketsByPath.removeValue(forKey: path)
            } else {
                bucketsByPath[path] = bucket
            }
        }
        return true
    }

    private func rebuildSessionFiles() {
        let sortedBuckets = bucketsByPath.values.sorted { lhs, rhs in
            if lhs.lastModifiedAt != rhs.lastModifiedAt {
                return lhs.lastModifiedAt > rhs.lastModifiedAt
            }
            return lhs.lastSequence > rhs.lastSequence
        }
        sessionFiles = sortedBuckets.map(buildFileDiff(from:))
    }

    private func buildFileDiff(from bucket: FileBucket) -> PCodeFileDiff {
        let orderedPreviews = bucket.activePreviews.values.sorted { $0.sequence < $1.sequence }
        let isStreaming = !orderedPreviews.isEmpty

        if bucket.completedEdits.isEmpty, let latestPreview = orderedPreviews.last {
            return makePreviewOnlyDiff(from: latestPreview)
        }

        let aggregatedKind = aggregateChangeKind(bucket.completedEdits)
        var totalAdded = 0
        var totalRemoved = 0
        var anyTruncated = false
        var callIDs: [String] = []
        var patches: [String] = []
        var combinedHunks: [PCodeDiffHunk] = []

        let multipleEdits = bucket.completedEdits.count > 1 || isStreaming
        for (editIndex, edit) in bucket.completedEdits.enumerated() {
            totalAdded += edit.added
            totalRemoved += edit.removed
            anyTruncated = anyTruncated || edit.truncated
            callIDs.append(edit.callID)
            if !edit.patch.isEmpty {
                patches.append(edit.patch)
            }
            if multipleEdits {
                combinedHunks.append(contentsOf: uniquifyHunks(edit.hunks, prefix: "e\(editIndex)"))
            } else {
                combinedHunks.append(contentsOf: edit.hunks)
            }
        }

        if let latestPreview = orderedPreviews.last {
            if !callIDs.contains(latestPreview.callID) {
                callIDs.append(latestPreview.callID)
            }
            if !latestPreview.text.isEmpty {
                patches.append(latestPreview.text)
                let previewHunks = PCodeFallbackPatchParser.parsePreviewHunks(
                    latestPreview.text,
                    using: parseHunks
                )
                combinedHunks.append(contentsOf: uniquifyHunks(previewHunks, prefix: "preview"))
            }
        }

        return PCodeFileDiff(
            id: bucket.path,
            path: bucket.path,
            absolutePath: bucket.absolutePath,
            kind: aggregatedKind,
            added: totalAdded,
            removed: totalRemoved,
            hunks: combinedHunks,
            rawPatch: patches.joined(separator: "\n"),
            lastModifiedAt: bucket.lastModifiedAt,
            editCount: bucket.completedEdits.count,
            isStreaming: isStreaming,
            truncated: anyTruncated,
            callIDs: callIDs
        )
    }

    private func makePreviewOnlyDiff(from preview: PreviewEntry) -> PCodeFileDiff {
        let previewHunks = PCodeFallbackPatchParser.parsePreviewHunks(preview.text, using: parseHunks)
        let stats = PCodeFallbackPatchParser.countLineStats(in: previewHunks)
        return PCodeFileDiff(
            id: preview.path,
            path: preview.path,
            absolutePath: preview.absolutePath,
            kind: .modified,
            added: stats.added,
            removed: stats.removed,
            hunks: previewHunks,
            rawPatch: preview.text,
            lastModifiedAt: preview.receivedAt,
            editCount: 0,
            isStreaming: true,
            truncated: false,
            callIDs: [preview.callID]
        )
    }

    private func aggregateChangeKind(_ edits: [CompletedEditEntry]) -> PCodeFileChangeKind {
        guard let first = edits.first, let last = edits.last else { return .modified }
        if last.kind == .deleted {
            return .deleted
        }
        if first.kind == .added {
            return .added
        }
        if last.kind == .renamed {
            return .renamed
        }
        return .modified
    }

    private func uniquifyHunks(_ hunks: [PCodeDiffHunk], prefix: String) -> [PCodeDiffHunk] {
        hunks.enumerated().map { idx, hunk in
            let hunkPrefix = "\(prefix)-\(idx)"
            return PCodeDiffHunk(
                id: "\(hunkPrefix)-\(hunk.id)",
                header: hunk.header,
                oldStart: hunk.oldStart,
                oldCount: hunk.oldCount,
                newStart: hunk.newStart,
                newCount: hunk.newCount,
                lines: hunk.lines.map { line in
                    PCodeDiffLine(
                        id: "\(hunkPrefix)-\(line.id)",
                        kind: line.kind,
                        oldLineNumber: line.oldLineNumber,
                        newLineNumber: line.newLineNumber,
                        text: line.text,
                        intralineSpans: line.intralineSpans
                    )
                }
            )
        }
    }
}
