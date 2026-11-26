import Foundation

/// Lightweight built-in unified diff parser used as the default `parseHunks` fallback
/// so Track B compiles and runs independently of `PCodeDiffParser` (Track A).
enum PCodeFallbackPatchParser {
    /// Parses unified diff `@@ -oldStart,oldCount +newStart,newCount @@` hunks from a patch string.
    static func parseHunks(_ patch: String) -> [PCodeDiffHunk] {
        guard !patch.isEmpty else { return [] }

        var rawLines = patch
            .replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
            .split(separator: "\n", omittingEmptySubsequences: false)
            .map(String.init)

        if rawLines.last == "" {
            rawLines.removeLast()
        }

        var hunks: [PCodeDiffHunk] = []
        var activeHeader: String?
        var oldStart = 0
        var oldCount = 0
        var newStart = 0
        var newCount = 0
        var oldLine = 0
        var newLine = 0
        var oldRemaining = 0
        var newRemaining = 0
        var activeLines: [PCodeDiffLine] = []

        func flushHunk() {
            guard let header = activeHeader else { return }
            let hunkIndex = hunks.count
            hunks.append(
                PCodeDiffHunk(
                    id: "hunk-\(hunkIndex)-\(oldStart)-\(newStart)",
                    header: header,
                    oldStart: oldStart,
                    oldCount: oldCount,
                    newStart: newStart,
                    newCount: newCount,
                    lines: activeLines
                )
            )
            activeHeader = nil
            activeLines = []
        }

        for line in rawLines {
            if line.hasPrefix("diff --git ") {
                flushHunk()
                continue
            }

            if let parsed = parseHunkHeader(line) {
                flushHunk()
                activeHeader = line
                oldStart = parsed.oldStart
                oldCount = parsed.oldCount
                newStart = parsed.newStart
                newCount = parsed.newCount
                oldLine = parsed.oldStart
                newLine = parsed.newStart
                oldRemaining = parsed.oldCount
                newRemaining = parsed.newCount
                activeLines = []
                continue
            }

            guard activeHeader != nil else { continue }

            // Once a hunk's declared line budget is exhausted, file header lines belong to the next section.
            if oldRemaining <= 0 && newRemaining <= 0 && !line.hasPrefix("\\") {
                if line.hasPrefix("--- ") || line.hasPrefix("+++ ") || line.hasPrefix("index ")
                    || line.hasPrefix("new file mode ") || line.hasPrefix("deleted file mode ")
                    || line.hasPrefix("rename ") || line.hasPrefix("similarity ")
                {
                    flushHunk()
                    continue
                }
            }

            let hunkIndex = hunks.count
            let lineIndex = activeLines.count
            let lineID = "h\(hunkIndex)-l\(lineIndex)"

            if line.hasPrefix("+") {
                activeLines.append(
                    PCodeDiffLine(
                        id: lineID,
                        kind: .added,
                        oldLineNumber: nil,
                        newLineNumber: newLine,
                        text: String(line.dropFirst())
                    )
                )
                newLine += 1
                newRemaining -= 1
            } else if line.hasPrefix("-") {
                activeLines.append(
                    PCodeDiffLine(
                        id: lineID,
                        kind: .deleted,
                        oldLineNumber: oldLine,
                        newLineNumber: nil,
                        text: String(line.dropFirst())
                    )
                )
                oldLine += 1
                oldRemaining -= 1
            } else if line.hasPrefix(" ") {
                activeLines.append(
                    PCodeDiffLine(
                        id: lineID,
                        kind: .context,
                        oldLineNumber: oldLine,
                        newLineNumber: newLine,
                        text: String(line.dropFirst())
                    )
                )
                oldLine += 1
                newLine += 1
                oldRemaining -= 1
                newRemaining -= 1
            } else if line.hasPrefix("\\") {
                activeLines.append(
                    PCodeDiffLine(
                        id: lineID,
                        kind: .meta,
                        oldLineNumber: nil,
                        newLineNumber: nil,
                        text: line
                    )
                )
            } else if line.isEmpty && oldRemaining > 0 && newRemaining > 0 {
                // Some unified diff generators omit the leading space on blank context lines.
                activeLines.append(
                    PCodeDiffLine(
                        id: lineID,
                        kind: .context,
                        oldLineNumber: oldLine,
                        newLineNumber: newLine,
                        text: ""
                    )
                )
                oldLine += 1
                newLine += 1
                oldRemaining -= 1
                newRemaining -= 1
            } else {
                activeLines.append(
                    PCodeDiffLine(
                        id: lineID,
                        kind: .meta,
                        oldLineNumber: nil,
                        newLineNumber: nil,
                        text: line
                    )
                )
            }
        }

        flushHunk()
        return hunks
    }

    /// Parses streaming `editPreview` text, which typically carries raw `+/-` lines without `@@` headers.
    static func parsePreviewHunks(_ text: String, using parseHunks: (String) -> [PCodeDiffHunk] = parseHunks) -> [PCodeDiffHunk] {
        guard !text.isEmpty else { return [] }
        let parsed = parseHunks(text)
        if !parsed.isEmpty {
            return parsed
        }

        var rawLines = text
            .replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
            .split(separator: "\n", omittingEmptySubsequences: false)
            .map(String.init)
        if rawLines.last == "" {
            rawLines.removeLast()
        }
        guard !rawLines.isEmpty else { return [] }

        var lines: [PCodeDiffLine] = []
        var oldCount = 0
        var newCount = 0
        for (idx, line) in rawLines.enumerated() {
            let id = "preview-l\(idx)"
            if line.hasPrefix("+") {
                newCount += 1
                lines.append(
                    PCodeDiffLine(
                        id: id,
                        kind: .added,
                        oldLineNumber: nil,
                        newLineNumber: newCount,
                        text: String(line.dropFirst())
                    )
                )
            } else if line.hasPrefix("-") {
                oldCount += 1
                lines.append(
                    PCodeDiffLine(
                        id: id,
                        kind: .deleted,
                        oldLineNumber: oldCount,
                        newLineNumber: nil,
                        text: String(line.dropFirst())
                    )
                )
            } else if line.hasPrefix(" ") {
                oldCount += 1
                newCount += 1
                lines.append(
                    PCodeDiffLine(
                        id: id,
                        kind: .context,
                        oldLineNumber: oldCount,
                        newLineNumber: newCount,
                        text: String(line.dropFirst())
                    )
                )
            } else {
                oldCount += 1
                newCount += 1
                lines.append(
                    PCodeDiffLine(
                        id: id,
                        kind: .context,
                        oldLineNumber: oldCount,
                        newLineNumber: newCount,
                        text: line
                    )
                )
            }
        }

        return [
            PCodeDiffHunk(
                id: "preview-hunk-0",
                header: "@@ -1,\(oldCount) +1,\(newCount) @@",
                oldStart: oldCount > 0 ? 1 : 0,
                oldCount: oldCount,
                newStart: newCount > 0 ? 1 : 0,
                newCount: newCount,
                lines: lines
            )
        ]
    }

    static func countLineStats(in hunks: [PCodeDiffHunk]) -> (added: Int, removed: Int) {
        var added = 0
        var removed = 0
        for hunk in hunks {
            for line in hunk.lines {
                switch line.kind {
                case .added: added += 1
                case .deleted: removed += 1
                case .context, .meta: break
                }
            }
        }
        return (added, removed)
    }

    static func parseHunkHeader(_ line: String) -> (oldStart: Int, oldCount: Int, newStart: Int, newCount: Int)? {
        guard line.hasPrefix("@@ -") else { return nil }
        let afterOpen = line.dropFirst(3) // drop "@@ "
        guard let closeRange = afterOpen.range(of: " @@") else { return nil }
        let spec = afterOpen[..<closeRange.lowerBound]
        let parts = spec.split(separator: " ")
        guard parts.count >= 2,
              parts[0].hasPrefix("-"),
              parts[1].hasPrefix("+")
        else {
            return nil
        }

        guard let oldRange = parseRangeSpec(parts[0].dropFirst()),
              let newRange = parseRangeSpec(parts[1].dropFirst())
        else {
            return nil
        }

        return (oldRange.start, oldRange.count, newRange.start, newRange.count)
    }

    private static func parseRangeSpec(_ spec: Substring) -> (start: Int, count: Int)? {
        if let comma = spec.firstIndex(of: ",") {
            guard let start = Int(spec[..<comma]),
                  let count = Int(spec[spec.index(after: comma)...])
            else { return nil }
            return (start, count)
        }
        guard let start = Int(spec) else { return nil }
        return (start, 1)
    }
}

/// Normalizes file paths relative to a session directory while preserving the resolved absolute path.
enum PCodePathNormalizer {
    static func normalize(rawPath: String, sessionDirectory: String?) -> (displayPath: String, absolutePath: String?) {
        guard !rawPath.isEmpty else { return ("", nil) }

        let cleanedSessionDir: String? = {
            guard let dir = sessionDirectory, !dir.isEmpty else { return nil }
            let expanded = (dir as NSString).expandingTildeInPath
            return URL(fileURLWithPath: expanded).standardizedFileURL.path
        }()

        let isAbsoluteOrTilde = rawPath.hasPrefix("/") || rawPath.hasPrefix("~")
        if isAbsoluteOrTilde {
            let expanded = (rawPath as NSString).expandingTildeInPath
            let absPath = URL(fileURLWithPath: expanded).standardizedFileURL.path
            if let base = cleanedSessionDir {
                if let rel = relativePath(of: absPath, inside: base) {
                    return (rel, absPath)
                }
                // Handle macOS symlinked temp roots (e.g., /var/folders vs /private/var/folders).
                let resolvedAbs = URL(fileURLWithPath: absPath).resolvingSymlinksInPath().standardizedFileURL.path
                let resolvedBase = URL(fileURLWithPath: base).resolvingSymlinksInPath().standardizedFileURL.path
                if let rel = relativePath(of: resolvedAbs, inside: resolvedBase) {
                    return (rel, absPath)
                }
            }
            return (absPath, absPath)
        }

        var cleanedRelative = rawPath
        while cleanedRelative.hasPrefix("./") {
            cleanedRelative.removeFirst(2)
        }

        if let base = cleanedSessionDir {
            let absURL = URL(fileURLWithPath: base).appendingPathComponent(cleanedRelative).standardizedFileURL
            let absPath = absURL.path
            if let rel = relativePath(of: absPath, inside: base) {
                return (rel, absPath)
            }
            return (cleanedRelative, absPath)
        }

        return (cleanedRelative, nil)
    }

    private static func relativePath(of candidate: String, inside base: String) -> String? {
        if base == "/" {
            guard candidate.hasPrefix("/") else { return nil }
            let dropped = String(candidate.dropFirst())
            return dropped.isEmpty ? nil : dropped
        }
        let prefix = base.hasSuffix("/") ? base : base + "/"
        guard candidate.hasPrefix(prefix) else { return nil }
        let suffix = String(candidate.dropFirst(prefix.count))
        return suffix.isEmpty ? nil : suffix
    }
}

/// Result returned by `PCodeGitDiffProvider` queries.
struct PCodeGitDiffResult: Equatable, Sendable {
    let hasGitRepo: Bool
    let files: [PCodeFileDiff]
    let baseBranchName: String?
    let error: String?

    static let notAGitRepo = PCodeGitDiffResult(
        hasGitRepo: false,
        files: [],
        baseBranchName: nil,
        error: nil
    )
}

/// Async git diff provider that executes `/usr/bin/git` off the main thread with task cancellation
/// and non-blocking stdout/stderr pipes.
struct PCodeGitDiffProvider: Sendable {
    typealias HunkParser = @Sendable (String) -> [PCodeDiffHunk]

    static let defaultMaxUntrackedFileBytes = 256 * 1024
    static let defaultMaxUntrackedFiles = 100

    private static let diffOptions: [String] = [
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--find-renames",
        "--src-prefix=a/",
        "--dst-prefix=b/",
    ]

    let parseHunks: HunkParser
    let maxUntrackedFileBytes: Int
    let maxUntrackedFiles: Int
    let gitExecutablePath: String

    init(
        parseHunks: @escaping HunkParser = { PCodeDiffParser.parseHunks($0) },
        maxUntrackedFileBytes: Int = PCodeGitDiffProvider.defaultMaxUntrackedFileBytes,
        maxUntrackedFiles: Int = PCodeGitDiffProvider.defaultMaxUntrackedFiles,
        gitExecutablePath: String = "/usr/bin/git"
    ) {
        self.parseHunks = parseHunks
        self.maxUntrackedFileBytes = maxUntrackedFileBytes
        self.maxUntrackedFiles = maxUntrackedFiles
        self.gitExecutablePath = gitExecutablePath
    }

    /// Fetches working tree changes (`git status --porcelain=v1 -z`, `git diff HEAD` or `git diff`
    /// on unborn HEAD, plus synthetic added-file diffs for untracked non-binary files).
    func fetchWorkingTree(in directory: String) async -> PCodeGitDiffResult {
        guard isExistingDirectory(directory) else { return .notAGitRepo }

        do {
            try Task.checkCancellation()
            guard let repoRoot = try await resolveRepoRoot(in: directory) else {
                return .notAGitRepo
            }

            let statusEntries = try await fetchPorcelainStatus(in: repoRoot)
            let hasHead = try await hasHeadCommit(in: repoRoot)

            var diffsByPath: [String: PCodeFileDiff] = [:]

            if hasHead {
                let diffOutput = try await runGit(
                    ["-c", "core.quotepath=false", "diff"] + Self.diffOptions + ["HEAD"],
                    in: repoRoot
                )
                if diffOutput.status == 0 {
                    let parsed = parseGitDiffOutput(
                        diffOutput.stdoutString,
                        repoRoot: repoRoot,
                        sessionDirectory: directory
                    )
                    for file in parsed {
                        diffsByPath[file.path] = file
                    }
                } else {
                    return PCodeGitDiffResult(
                        hasGitRepo: true,
                        files: [],
                        baseBranchName: nil,
                        error: trimmedError(diffOutput.stderrString, fallback: "git diff HEAD failed")
                    )
                }
            } else {
                // Unborn HEAD (fresh `git init` with no commits yet): combine staged and unstaged diffs.
                let cachedOutput = try await runGit(
                    ["-c", "core.quotepath=false", "diff"] + Self.diffOptions + ["--cached"],
                    in: repoRoot
                )
                if cachedOutput.status == 0 {
                    for file in parseGitDiffOutput(
                        cachedOutput.stdoutString,
                        repoRoot: repoRoot,
                        sessionDirectory: directory
                    ) {
                        diffsByPath[file.path] = file
                    }
                }

                let unstagedOutput = try await runGit(
                    ["-c", "core.quotepath=false", "diff"] + Self.diffOptions,
                    in: repoRoot
                )
                if unstagedOutput.status == 0 {
                    for file in parseGitDiffOutput(
                        unstagedOutput.stdoutString,
                        repoRoot: repoRoot,
                        sessionDirectory: directory
                    ) {
                        if let existing = diffsByPath[file.path] {
                            diffsByPath[file.path] = mergeFileDiffs(existing, file)
                        } else {
                            diffsByPath[file.path] = file
                        }
                    }
                }
            }

            // Append synthetic added-file patches for untracked non-binary files, and ensure
            // empty/mode-only tracked status entries still appear.
            var untrackedCount = 0
            for entry in statusEntries {
                try Task.checkCancellation()
                let absPath = joinPath(repoRoot, entry.path)
                let normalized = PCodePathNormalizer.normalize(rawPath: absPath, sessionDirectory: directory)

                if entry.isUntracked {
                    guard untrackedCount < maxUntrackedFiles else { continue }
                    guard diffsByPath[normalized.displayPath] == nil else { continue }
                    if let synthetic = makeSyntheticUntrackedDiff(
                        repoRelativePath: entry.path,
                        displayPath: normalized.displayPath,
                        absolutePath: absPath
                    ) {
                        diffsByPath[synthetic.path] = synthetic
                        untrackedCount += 1
                    }
                } else if diffsByPath[normalized.displayPath] == nil {
                    diffsByPath[normalized.displayPath] = PCodeFileDiff(
                        path: normalized.displayPath,
                        absolutePath: normalized.absolutePath ?? absPath,
                        kind: entry.kind,
                        added: 0,
                        removed: 0,
                        hunks: [],
                        rawPatch: "",
                        lastModifiedAt: fileModificationDate(at: absPath) ?? Date(),
                        editCount: 1
                    )
                }
            }

            let sortedFiles = diffsByPath.values.sorted {
                $0.path.localizedStandardCompare($1.path) == .orderedAscending
            }
            return PCodeGitDiffResult(
                hasGitRepo: true,
                files: sortedFiles,
                baseBranchName: nil,
                error: nil
            )
        } catch is CancellationError {
            return PCodeGitDiffResult(hasGitRepo: true, files: [], baseBranchName: nil, error: nil)
        } catch {
            return PCodeGitDiffResult(
                hasGitRepo: true,
                files: [],
                baseBranchName: nil,
                error: error.localizedDescription
            )
        }
    }

    /// Fetches branch diff vs. detected base branch (`main`/`master`/upstream via `git merge-base`),
    /// comparing the merge-base against the working tree (combining `<base>...HEAD` and uncommitted edits)
    /// plus untracked non-binary files.
    func fetchBranchDiff(in directory: String) async -> PCodeGitDiffResult {
        guard isExistingDirectory(directory) else { return .notAGitRepo }

        do {
            try Task.checkCancellation()
            guard let repoRoot = try await resolveRepoRoot(in: directory) else {
                return .notAGitRepo
            }

            let hasHead = try await hasHeadCommit(in: repoRoot)
            guard hasHead else {
                // No commits yet — return working tree changes with no base branch.
                return await fetchWorkingTree(in: directory)
            }

            guard let base = try await detectBaseBranch(in: repoRoot) else {
                // No recognizable base branch — fall back to working tree diff.
                return await fetchWorkingTree(in: directory)
            }

            // `git diff <mergeBase>` diffs the merge-base commit directly against the working tree,
            // capturing both branch commits (`<base>...HEAD`) and uncommitted staged/unstaged changes
            // in one accurate net patch.
            let diffOutput = try await runGit(
                ["-c", "core.quotepath=false", "diff"] + Self.diffOptions + [base.mergeBase],
                in: repoRoot
            )
            guard diffOutput.status == 0 else {
                return PCodeGitDiffResult(
                    hasGitRepo: true,
                    files: [],
                    baseBranchName: base.name,
                    error: trimmedError(diffOutput.stderrString, fallback: "git diff \(base.name) failed")
                )
            }

            var diffsByPath: [String: PCodeFileDiff] = [:]
            for file in parseGitDiffOutput(
                diffOutput.stdoutString,
                repoRoot: repoRoot,
                sessionDirectory: directory
            ) {
                diffsByPath[file.path] = file
            }

            // Include untracked non-binary files as well so the branch view matches the full working tree.
            let statusEntries = try await fetchPorcelainStatus(in: repoRoot)
            var untrackedCount = 0
            for entry in statusEntries where entry.isUntracked {
                try Task.checkCancellation()
                guard untrackedCount < maxUntrackedFiles else { break }
                let absPath = joinPath(repoRoot, entry.path)
                let normalized = PCodePathNormalizer.normalize(rawPath: absPath, sessionDirectory: directory)
                guard diffsByPath[normalized.displayPath] == nil else { continue }
                if let synthetic = makeSyntheticUntrackedDiff(
                    repoRelativePath: entry.path,
                    displayPath: normalized.displayPath,
                    absolutePath: absPath
                ) {
                    diffsByPath[synthetic.path] = synthetic
                    untrackedCount += 1
                }
            }

            let sortedFiles = diffsByPath.values.sorted {
                $0.path.localizedStandardCompare($1.path) == .orderedAscending
            }
            return PCodeGitDiffResult(
                hasGitRepo: true,
                files: sortedFiles,
                baseBranchName: base.name,
                error: nil
            )
        } catch is CancellationError {
            return PCodeGitDiffResult(hasGitRepo: true, files: [], baseBranchName: nil, error: nil)
        } catch {
            return PCodeGitDiffResult(
                hasGitRepo: true,
                files: [],
                baseBranchName: nil,
                error: error.localizedDescription
            )
        }
    }

    // MARK: - Git helpers

    private struct GitCommandOutput: Sendable {
        let status: Int32
        let stdout: Data
        let stderr: Data

        var stdoutString: String {
            String(decoding: stdout, as: UTF8.self)
        }

        var stderrString: String {
            String(decoding: stderr, as: UTF8.self)
        }
    }

    private struct PorcelainEntry: Sendable {
        let path: String
        let kind: PCodeFileChangeKind
        let isUntracked: Bool
    }

    private func resolveRepoRoot(in directory: String) async throws -> String? {
        let output = try await runGit(["rev-parse", "--show-toplevel"], in: directory)
        guard output.status == 0 else { return nil }
        let root = output.stdoutString.trimmingCharacters(in: .whitespacesAndNewlines)
        return root.isEmpty ? nil : root
    }

    private func hasHeadCommit(in repoRoot: String) async throws -> Bool {
        let output = try await runGit(["rev-parse", "--verify", "--quiet", "HEAD"], in: repoRoot)
        return output.status == 0
    }

    private func fetchPorcelainStatus(in repoRoot: String) async throws -> [PorcelainEntry] {
        let output = try await runGit(
            ["-c", "core.quotepath=false", "status", "--porcelain=v1", "-z", "-uall"],
            in: repoRoot
        )
        guard output.status == 0 else { return [] }
        return Self.parsePorcelainZ(output.stdout)
    }

    private func detectBaseBranch(in repoRoot: String) async throws -> (name: String, mergeBase: String)? {
        let currentBranchOutput = try await runGit(["rev-parse", "--abbrev-ref", "HEAD"], in: repoRoot)
        let currentBranch = currentBranchOutput.status == 0
            ? currentBranchOutput.stdoutString.trimmingCharacters(in: .whitespacesAndNewlines)
            : ""

        var candidates: [String] = []
        func addCandidate(_ ref: String) {
            let trimmed = ref.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty, !candidates.contains(trimmed) else { return }
            candidates.append(trimmed)
        }

        // Prefer main/master when on another branch.
        if currentBranch != "main" { addCandidate("main") }
        if currentBranch != "master" { addCandidate("master") }

        // Check origin's default branch if configured (e.g., origin/main).
        let originHead = try await runGit(
            ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
            in: repoRoot
        )
        if originHead.status == 0 {
            addCandidate(originHead.stdoutString)
        }

        addCandidate("origin/main")
        addCandidate("origin/master")

        // Upstream tracking branch as fallback (e.g., when already on main with origin/main upstream).
        let upstream = try await runGit(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
            in: repoRoot
        )
        if upstream.status == 0 {
            addCandidate(upstream.stdoutString)
        }

        // If already on main/master with no remote, fall back to self so baseBranchName is still reported.
        if currentBranch == "main" || currentBranch == "master" {
            addCandidate(currentBranch)
        }

        for candidate in candidates {
            try Task.checkCancellation()
            let verify = try await runGit(["rev-parse", "--verify", "--quiet", "\(candidate)^{commit}"], in: repoRoot)
            guard verify.status == 0 else { continue }

            let mb = try await runGit(["merge-base", candidate, "HEAD"], in: repoRoot)
            if mb.status == 0 {
                let sha = mb.stdoutString.trimmingCharacters(in: .whitespacesAndNewlines)
                if !sha.isEmpty {
                    return (candidate, sha)
                }
            }
        }

        return nil
    }

    // MARK: - Parsing

    private static func parsePorcelainZ(_ data: Data) -> [PorcelainEntry] {
        guard !data.isEmpty else { return [] }
        let tokens = data.split(separator: 0, omittingEmptySubsequences: false)
        var entries: [PorcelainEntry] = []
        var i = 0

        while i < tokens.count {
            let token = tokens[i]
            guard token.count >= 4 else {
                i += 1
                continue
            }
            let str = String(decoding: token, as: UTF8.self)
            guard str.count >= 4 else {
                i += 1
                continue
            }
            let x = str[str.startIndex]
            let y = str[str.index(after: str.startIndex)]
            let pathStart = str.index(str.startIndex, offsetBy: 3)
            let path = String(str[pathStart...])

            let isRenameOrCopy = x == "R" || y == "R" || x == "C" || y == "C"
            if isRenameOrCopy {
                // In `-z` mode, rename/copy entries are followed by an extra NUL-terminated original path.
                i += 2
            } else {
                i += 1
            }

            guard !path.isEmpty else { continue }

            if x == "?" && y == "?" {
                entries.append(PorcelainEntry(path: path, kind: .added, isUntracked: true))
            } else if x == "!" && y == "!" {
                continue
            } else if x == "R" || y == "R" {
                entries.append(PorcelainEntry(path: path, kind: .renamed, isUntracked: false))
            } else if x == "D" || y == "D" {
                entries.append(PorcelainEntry(path: path, kind: .deleted, isUntracked: false))
            } else if x == "A" || y == "A" {
                entries.append(PorcelainEntry(path: path, kind: .added, isUntracked: false))
            } else {
                entries.append(PorcelainEntry(path: path, kind: .modified, isUntracked: false))
            }
        }

        return entries
    }

    private func parseGitDiffOutput(
        _ rawDiff: String,
        repoRoot: String,
        sessionDirectory: String
    ) -> [PCodeFileDiff] {
        guard !rawDiff.isEmpty else { return [] }

        let sections = splitGitDiffSections(rawDiff)
        var results: [PCodeFileDiff] = []
        results.reserveCapacity(sections.count)

        for section in sections {
            guard let repoRelPath = extractPath(fromGitDiffSection: section) else { continue }
            let kind = extractChangeKind(fromGitDiffSection: section)
            let isBinary = section.contains("\nBinary files ") || section.contains("\nGIT binary patch")
            let hunks = isBinary ? [] : parseHunks(section)
            let stats = PCodeFallbackPatchParser.countLineStats(in: hunks)
            let absPath = joinPath(repoRoot, repoRelPath)
            let normalized = PCodePathNormalizer.normalize(rawPath: absPath, sessionDirectory: sessionDirectory)

            results.append(
                PCodeFileDiff(
                    path: normalized.displayPath,
                    absolutePath: normalized.absolutePath ?? absPath,
                    kind: kind,
                    added: stats.added,
                    removed: stats.removed,
                    hunks: hunks,
                    rawPatch: section,
                    lastModifiedAt: fileModificationDate(at: absPath) ?? Date(),
                    editCount: 1,
                    isStreaming: false,
                    truncated: false,
                    callIDs: []
                )
            )
        }

        return results
    }

    private func splitGitDiffSections(_ rawDiff: String) -> [String] {
        let normalized = rawDiff.replacingOccurrences(of: "\r\n", with: "\n")
        let lines = normalized.split(separator: "\n", omittingEmptySubsequences: false)
        var sections: [String] = []
        var current: [Substring] = []

        for line in lines {
            if line.hasPrefix("diff --git ") {
                if !current.isEmpty {
                    sections.append(current.joined(separator: "\n"))
                    current.removeAll(keepingCapacity: true)
                }
            }
            if !current.isEmpty || line.hasPrefix("diff --git ") {
                current.append(line)
            }
        }
        if !current.isEmpty {
            let joined = current.joined(separator: "\n").trimmingCharacters(in: CharacterSet(charactersIn: "\n"))
            if !joined.isEmpty {
                sections.append(joined)
            }
        }
        return sections
    }

    private func extractPath(fromGitDiffSection section: String) -> String? {
        let lines = section.split(separator: "\n", omittingEmptySubsequences: false)
        var oldPath: String?
        var newPath: String?
        var renameTo: String?

        for line in lines {
            if line.hasPrefix("@@ ") { break }
            if line.hasPrefix("rename to ") {
                renameTo = unquoteGitPath(String(line.dropFirst("rename to ".count)))
            } else if line.hasPrefix("+++ ") {
                let candidate = String(line.dropFirst(4))
                if candidate != "/dev/null" {
                    newPath = stripDiffPrefix(unquoteGitPath(candidate), prefix: "b/")
                }
            } else if line.hasPrefix("--- ") {
                let candidate = String(line.dropFirst(4))
                if candidate != "/dev/null" {
                    oldPath = stripDiffPrefix(unquoteGitPath(candidate), prefix: "a/")
                }
            }
        }

        if let renameTo, !renameTo.isEmpty { return renameTo }
        if let newPath, !newPath.isEmpty { return newPath }
        if let oldPath, !oldPath.isEmpty { return oldPath }

        // Fallback to `diff --git a/<path> b/<path>` header (e.g. mode-only or empty binary diffs).
        if let first = lines.first, first.hasPrefix("diff --git ") {
            let rest = String(first.dropFirst("diff --git ".count))
            if let range = rest.range(of: " b/", options: .backwards) {
                return unquoteGitPath(String(rest[range.upperBound...]))
            }
        }
        return nil
    }

    private func extractChangeKind(fromGitDiffSection section: String) -> PCodeFileChangeKind {
        let lines = section.split(separator: "\n", omittingEmptySubsequences: false)
        for line in lines {
            if line.hasPrefix("@@ ") { break }
            if line.hasPrefix("new file mode ") || line == "--- /dev/null" {
                return .added
            }
            if line.hasPrefix("deleted file mode ") || line == "+++ /dev/null" {
                return .deleted
            }
            if line.hasPrefix("rename from ") || line.hasPrefix("rename to ") {
                return .renamed
            }
        }
        return .modified
    }

    private func stripDiffPrefix(_ path: String, prefix: String) -> String {
        if path.hasPrefix(prefix) {
            return String(path.dropFirst(prefix.count))
        }
        return path
    }

    private func unquoteGitPath(_ raw: String) -> String {
        // Strip trailing tab timestamp if present.
        let cleaned = raw.split(separator: "\t", maxSplits: 1, omittingEmptySubsequences: false).first.map(String.init) ?? raw
        guard cleaned.hasPrefix("\""), cleaned.hasSuffix("\""), cleaned.count >= 2 else {
            return cleaned
        }
        let inner = cleaned.dropFirst().dropLast()
        return inner
            .replacingOccurrences(of: "\\\"", with: "\"")
            .replacingOccurrences(of: "\\\\", with: "\\")
    }

    private func mergeFileDiffs(_ lhs: PCodeFileDiff, _ rhs: PCodeFileDiff) -> PCodeFileDiff {
        let combinedPatch = [lhs.rawPatch, rhs.rawPatch]
            .filter { !$0.isEmpty }
            .joined(separator: "\n")
        let combinedHunks = lhs.hunks + rhs.hunks.enumerated().map { idx, hunk in
            PCodeDiffHunk(
                id: "m-\(idx)-\(hunk.id)",
                header: hunk.header,
                oldStart: hunk.oldStart,
                oldCount: hunk.oldCount,
                newStart: hunk.newStart,
                newCount: hunk.newCount,
                lines: hunk.lines.map { line in
                    PCodeDiffLine(
                        id: "m-\(idx)-\(line.id)",
                        kind: line.kind,
                        oldLineNumber: line.oldLineNumber,
                        newLineNumber: line.newLineNumber,
                        text: line.text,
                        intralineSpans: line.intralineSpans
                    )
                }
            )
        }
        return PCodeFileDiff(
            path: lhs.path,
            absolutePath: rhs.absolutePath ?? lhs.absolutePath,
            kind: lhs.kind == .added ? .added : rhs.kind,
            added: lhs.added + rhs.added,
            removed: lhs.removed + rhs.removed,
            hunks: combinedHunks,
            rawPatch: combinedPatch,
            lastModifiedAt: max(lhs.lastModifiedAt, rhs.lastModifiedAt),
            editCount: lhs.editCount + rhs.editCount,
            isStreaming: false,
            truncated: lhs.truncated || rhs.truncated,
            callIDs: lhs.callIDs + rhs.callIDs
        )
    }

    // MARK: - Synthetic untracked diffs

    private func makeSyntheticUntrackedDiff(
        repoRelativePath: String,
        displayPath: String,
        absolutePath: String
    ) -> PCodeFileDiff? {
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: absolutePath, isDirectory: &isDirectory),
              !isDirectory.boolValue
        else {
            return nil
        }

        guard let handle = FileHandle(forReadingAtPath: absolutePath) else {
            return nil
        }
        defer { try? handle.close() }

        let readLimit = max(0, maxUntrackedFileBytes) + 1
        guard let rawData = try? handle.read(upToCount: readLimit) else {
            return nil
        }

        // Skip binary files using git's NUL-byte heuristic on the initial 8 KiB prefix.
        let probeLength = min(rawData.count, 8192)
        if rawData.prefix(probeLength).contains(0) {
            return nil
        }

        let truncated = rawData.count > maxUntrackedFileBytes
        let dataToDecode: Data = truncated ? rawData.prefix(maxUntrackedFileBytes) : rawData

        guard let text = decodeUTF8WithOptionalTruncation(dataToDecode, allowTrimTrailing: truncated) else {
            return nil
        }

        let modDate = fileModificationDate(at: absolutePath) ?? Date()

        if text.isEmpty {
            let patch = """
            diff --git a/\(repoRelativePath) b/\(repoRelativePath)
            new file mode 100644
            --- /dev/null
            +++ b/\(repoRelativePath)
            """
            return PCodeFileDiff(
                path: displayPath,
                absolutePath: absolutePath,
                kind: .added,
                added: 0,
                removed: 0,
                hunks: [],
                rawPatch: patch,
                lastModifiedAt: modDate,
                editCount: 1,
                isStreaming: false,
                truncated: truncated
            )
        }

        let normalizedText = text
            .replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
        var lines = normalizedText
            .split(separator: "\n", omittingEmptySubsequences: false)
            .map(String.init)
        let endsWithNewline = normalizedText.hasSuffix("\n")
        if endsWithNewline && lines.last == "" {
            lines.removeLast()
        }

        var patchLines: [String] = [
            "diff --git a/\(repoRelativePath) b/\(repoRelativePath)",
            "new file mode 100644",
            "--- /dev/null",
            "+++ b/\(repoRelativePath)",
            "@@ -0,0 +1,\(lines.count) @@",
        ]
        patchLines.reserveCapacity(patchLines.count + lines.count + 1)
        for line in lines {
            patchLines.append("+\(line)")
        }
        if !endsWithNewline && !truncated {
            patchLines.append("\\ No newline at end of file")
        }

        let rawPatch = patchLines.joined(separator: "\n")
        let hunks = parseHunks(rawPatch)
        let stats = PCodeFallbackPatchParser.countLineStats(in: hunks)

        return PCodeFileDiff(
            path: displayPath,
            absolutePath: absolutePath,
            kind: .added,
            added: stats.added > 0 ? stats.added : lines.count,
            removed: 0,
            hunks: hunks,
            rawPatch: rawPatch,
            lastModifiedAt: modDate,
            editCount: 1,
            isStreaming: false,
            truncated: truncated
        )
    }

    private func decodeUTF8WithOptionalTruncation(_ data: Data, allowTrimTrailing: Bool) -> String? {
        if let text = String(data: data, encoding: .utf8) {
            return text
        }
        guard allowTrimTrailing else { return nil }
        // Truncating mid-UTF-8 multi-byte character leaves up to 3 trailing bytes invalid.
        for trim in 1...min(3, data.count) {
            if let text = String(data: data.dropLast(trim), encoding: .utf8) {
                return text
            }
        }
        return nil
    }

    // MARK: - Subprocess execution

    private final class ProcessCancellationBox: @unchecked Sendable {
        private let lock = NSLock()
        private var process: Process?
        private var cancelled = false

        func attach(_ process: Process) -> Bool {
            lock.lock()
            defer { lock.unlock() }
            if cancelled { return false }
            self.process = process
            return true
        }

        func detach() {
            lock.lock()
            process = nil
            lock.unlock()
        }

        func cancel() {
            lock.lock()
            cancelled = true
            let running = process
            lock.unlock()
            if let running, running.isRunning {
                running.terminate()
            }
        }

        var isCancelled: Bool {
            lock.lock()
            defer { lock.unlock() }
            return cancelled
        }
    }

    private func runGit(_ arguments: [String], in directory: String) async throws -> GitCommandOutput {
        try Task.checkCancellation()

        let executablePath = gitExecutablePath
        let box = ProcessCancellationBox()

        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                DispatchQueue.global(qos: .userInitiated).async {
                    if box.isCancelled {
                        continuation.resume(throwing: CancellationError())
                        return
                    }

                    let process = Process()
                    process.executableURL = URL(fileURLWithPath: executablePath)
                    process.arguments = arguments
                    process.currentDirectoryURL = URL(fileURLWithPath: directory)

                    var env = ProcessInfo.processInfo.environment
                    env["GIT_TERMINAL_PROMPT"] = "0"
                    env["GIT_OPTIONAL_LOCKS"] = "0"
                    process.environment = env

                    let stdoutPipe = Pipe()
                    let stderrPipe = Pipe()
                    process.standardOutput = stdoutPipe
                    process.standardError = stderrPipe
                    process.standardInput = FileHandle.nullDevice

                    guard box.attach(process) else {
                        continuation.resume(throwing: CancellationError())
                        return
                    }

                    do {
                        try process.run()
                    } catch {
                        box.detach()
                        continuation.resume(throwing: error)
                        return
                    }

                    if box.isCancelled {
                        process.terminate()
                    }

                    // Drain stdout and stderr concurrently off-thread to prevent pipe-buffer deadlocks.
                    let group = DispatchGroup()
                    final class DataBox: @unchecked Sendable {
                        var data = Data()
                    }
                    let stdoutBox = DataBox()
                    let stderrBox = DataBox()

                    group.enter()
                    DispatchQueue.global(qos: .userInitiated).async {
                        stdoutBox.data = (try? stdoutPipe.fileHandleForReading.readToEnd()) ?? Data()
                        try? stdoutPipe.fileHandleForReading.close()
                        group.leave()
                    }

                    group.enter()
                    DispatchQueue.global(qos: .userInitiated).async {
                        stderrBox.data = (try? stderrPipe.fileHandleForReading.readToEnd()) ?? Data()
                        try? stderrPipe.fileHandleForReading.close()
                        group.leave()
                    }

                    process.waitUntilExit()
                    box.detach()
                    group.wait()

                    if box.isCancelled {
                        continuation.resume(throwing: CancellationError())
                        return
                    }

                    continuation.resume(
                        returning: GitCommandOutput(
                            status: process.terminationStatus,
                            stdout: stdoutBox.data,
                            stderr: stderrBox.data
                        )
                    )
                }
            }
        } onCancel: {
            box.cancel()
        }
    }

    // MARK: - Filesystem utilities

    private func isExistingDirectory(_ path: String) -> Bool {
        var isDir: ObjCBool = false
        return FileManager.default.fileExists(atPath: path, isDirectory: &isDir) && isDir.boolValue
    }

    private func joinPath(_ root: String, _ relative: String) -> String {
        URL(fileURLWithPath: root).appendingPathComponent(relative).standardizedFileURL.path
    }

    private func fileModificationDate(at path: String) -> Date? {
        (try? FileManager.default.attributesOfItem(atPath: path))?[.modificationDate] as? Date
    }

    private func trimmedError(_ raw: String, fallback: String) -> String {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? fallback : trimmed
    }
}
