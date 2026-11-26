import Foundation

/// A paired row for side-by-side diff rendering.
struct PCodeSideBySideRow: Identifiable, Equatable, Hashable, Sendable {
    let id: String
    let left: PCodeDiffLine?
    let right: PCodeDiffLine?
    /// True when this row represents a hunk-level metadata line (e.g. `\ No newline at end of file`).
    let isMeta: Bool

    init(id: String, left: PCodeDiffLine?, right: PCodeDiffLine?, isMeta: Bool = false) {
        self.id = id
        self.left = left
        self.right = right
        self.isMeta = isMeta
    }
}

/// Unified diff parser, intraline word-diff engine, side-by-side pairing, and on-disk context expansion.
enum PCodeDiffParser {
    // MARK: - Hunk Header Parsing

    struct ParsedHunkHeader: Equatable, Sendable {
        let oldStart: Int
        let oldCount: Int
        let newStart: Int
        let newCount: Int
        let context: String
    }

    /// Parse a unified diff hunk header such as `@@ -10,4 +12,6 @@ func foo()`.
    /// Omitted counts default to `1` per the unified diff specification.
    static func parseHunkHeader(_ line: String) -> ParsedHunkHeader? {
        guard line.hasPrefix("@@ ") else { return nil }
        guard let closingRange = line.range(of: " @@", range: line.index(line.startIndex, offsetBy: 3)..<line.endIndex) else {
            return nil
        }

        let rangeSpec = line[line.index(line.startIndex, offsetBy: 3)..<closingRange.lowerBound]
            .trimmingCharacters(in: .whitespaces)
        let parts = rangeSpec.split(separator: " ", omittingEmptySubsequences: true)
        guard parts.count >= 2,
              parts[0].hasPrefix("-"),
              parts[1].hasPrefix("+"),
              let (oldStart, oldCount) = parseRangeSpec(parts[0].dropFirst()),
              let (newStart, newCount) = parseRangeSpec(parts[1].dropFirst())
        else {
            return nil
        }

        let trailing = line[closingRange.upperBound...].trimmingCharacters(in: .whitespaces)
        return ParsedHunkHeader(
            oldStart: oldStart,
            oldCount: oldCount,
            newStart: newStart,
            newCount: newCount,
            context: trailing
        )
    }

    private static func parseRangeSpec(_ spec: Substring) -> (Int, Int)? {
        if let comma = spec.firstIndex(of: ",") {
            guard let start = Int(spec[..<comma]),
                  let count = Int(spec[spec.index(after: comma)...]),
                  start >= 0, count >= 0
            else {
                return nil
            }
            return (start, count)
        }
        guard let start = Int(spec), start >= 0 else { return nil }
        return (start, 1)
    }

    // MARK: - Binary Detection

    /// Returns true if the patch represents a binary file diff rather than text hunks.
    static func isBinaryPatch(_ patch: String) -> Bool {
        let lines = splitLines(patch)
        for line in lines {
            if line.hasPrefix("GIT binary patch") || line.hasPrefix("Binary files ") {
                return true
            }
            if line.hasPrefix("@@ ") {
                return false
            }
        }
        return false
    }

    // MARK: - Single-File Hunk Parsing

    /// Parse unified diff hunks from a single-file patch (with or without file headers).
    static func parseHunks(
        _ patch: String,
        idPrefix: String = "",
        computeIntraline: Bool = true
    ) -> [PCodeDiffHunk] {
        let rawLines = splitLines(patch)
        guard !rawLines.isEmpty else { return [] }

        var hunks: [PCodeDiffHunk] = []
        var index = 0

        while index < rawLines.count {
            let line = rawLines[index]
            guard let header = parseHunkHeader(line) else {
                index += 1
                continue
            }

            let hunkIndex = hunks.count
            let hunkID = idPrefix.isEmpty
                ? "hunk-\(hunkIndex)-\(header.oldStart)-\(header.newStart)"
                : "\(idPrefix)#hunk-\(hunkIndex)-\(header.oldStart)-\(header.newStart)"

            var oldCursor = header.oldStart
            var newCursor = header.newStart
            var oldSeen = 0
            var newSeen = 0
            var parsedLines: [PCodeDiffLine] = []

            index += 1
            while index < rawLines.count {
                let bodyLine = rawLines[index]

                // Stop when a new hunk or new file header begins.
                if bodyLine.hasPrefix("@@ ") && parseHunkHeader(bodyLine) != nil {
                    break
                }
                if bodyLine.hasPrefix("diff --git ") {
                    break
                }
                if bodyLine.hasPrefix("--- ") && oldSeen >= header.oldCount && newSeen >= header.newCount {
                    break
                }

                let lineID = "\(hunkID)-L\(parsedLines.count)"

                if bodyLine.hasPrefix("+") {
                    let text = String(bodyLine.dropFirst())
                    parsedLines.append(PCodeDiffLine(
                        id: lineID,
                        kind: .added,
                        oldLineNumber: nil,
                        newLineNumber: newCursor,
                        text: text
                    ))
                    newCursor += 1
                    newSeen += 1
                } else if bodyLine.hasPrefix("-") {
                    let text = String(bodyLine.dropFirst())
                    parsedLines.append(PCodeDiffLine(
                        id: lineID,
                        kind: .deleted,
                        oldLineNumber: oldCursor,
                        newLineNumber: nil,
                        text: text
                    ))
                    oldCursor += 1
                    oldSeen += 1
                } else if bodyLine.hasPrefix(" ") {
                    let text = String(bodyLine.dropFirst())
                    parsedLines.append(PCodeDiffLine(
                        id: lineID,
                        kind: .context,
                        oldLineNumber: oldCursor,
                        newLineNumber: newCursor,
                        text: text
                    ))
                    oldCursor += 1
                    newCursor += 1
                    oldSeen += 1
                    newSeen += 1
                } else if bodyLine.hasPrefix("\\ ") {
                    parsedLines.append(PCodeDiffLine(
                        id: lineID,
                        kind: .meta,
                        oldLineNumber: nil,
                        newLineNumber: nil,
                        text: bodyLine
                    ))
                } else if bodyLine.isEmpty {
                    // Some patch generators strip the leading space on blank context lines.
                    if oldSeen < header.oldCount && newSeen < header.newCount {
                        parsedLines.append(PCodeDiffLine(
                            id: lineID,
                            kind: .context,
                            oldLineNumber: oldCursor,
                            newLineNumber: newCursor,
                            text: ""
                        ))
                        oldCursor += 1
                        newCursor += 1
                        oldSeen += 1
                        newSeen += 1
                    } else if oldSeen >= header.oldCount && newSeen >= header.newCount {
                        // Trailing blank separator after hunk completion.
                        index += 1
                        break
                    }
                } else {
                    // Unrecognized line after hunk counts are satisfied terminates the hunk.
                    if oldSeen >= header.oldCount && newSeen >= header.newCount {
                        break
                    }
                    parsedLines.append(PCodeDiffLine(
                        id: lineID,
                        kind: .meta,
                        oldLineNumber: nil,
                        newLineNumber: nil,
                        text: bodyLine
                    ))
                }

                index += 1
            }

            let finalLines = computeIntraline ? applyIntralineSpans(to: parsedLines) : parsedLines
            hunks.append(PCodeDiffHunk(
                id: hunkID,
                header: line,
                oldStart: header.oldStart,
                oldCount: header.oldCount,
                newStart: header.newStart,
                newCount: header.newCount,
                lines: finalLines
            ))
        }

        return hunks
    }

    // MARK: - Multi-File & Single-File Patch Parsing

    /// Parse a single-file patch where the file path is already known (e.g., from `editPreview` / `editCompleted`).
    static func parseFile(
        _ patch: String,
        path: String,
        absolutePath: String? = nil,
        kind: PCodeFileChangeKind? = nil,
        isStreaming: Bool = false,
        truncated: Bool = false,
        callIDs: [String] = [],
        editCount: Int = 1,
        lastModifiedAt: Date = Date(),
        computeIntraline: Bool = true
    ) -> PCodeFileDiff {
        let hunks = parseHunks(patch, idPrefix: path, computeIntraline: computeIntraline)
        let (added, removed) = countChanges(in: hunks)
        let resolvedKind = kind ?? inferChangeKind(from: splitLines(patch))
        return PCodeFileDiff(
            path: path,
            absolutePath: absolutePath,
            kind: resolvedKind,
            added: added,
            removed: removed,
            hunks: hunks,
            rawPatch: patch,
            lastModifiedAt: lastModifiedAt,
            editCount: editCount,
            isStreaming: isStreaming,
            truncated: truncated,
            callIDs: callIDs
        )
    }

    /// Parse a multi-file (or single-file) unified diff into `[PCodeFileDiff]`.
    static func parseFiles(
        _ patch: String,
        baseDirectory: String? = nil,
        computeIntraline: Bool = true,
        timestamp: Date = Date()
    ) -> [PCodeFileDiff] {
        let rawLines = splitLines(patch)
        guard !rawLines.isEmpty else { return [] }

        let sections = splitFileSections(rawLines)
        var results: [PCodeFileDiff] = []
        results.reserveCapacity(sections.count)

        for sectionLines in sections {
            guard let fileDiff = parseFileSection(
                sectionLines,
                baseDirectory: baseDirectory,
                computeIntraline: computeIntraline,
                timestamp: timestamp
            ) else {
                continue
            }
            results.append(fileDiff)
        }

        return results
    }

    private static func splitFileSections(_ lines: [String]) -> [[String]] {
        let hasDiffGit = lines.contains { $0.hasPrefix("diff --git ") }
        if hasDiffGit {
            var sections: [[String]] = []
            var current: [String] = []
            for line in lines {
                if line.hasPrefix("diff --git ") {
                    if !current.isEmpty {
                        sections.append(current)
                    }
                    current = [line]
                } else if !current.isEmpty {
                    current.append(line)
                }
            }
            if !current.isEmpty {
                sections.append(current)
            }
            return sections
        }

        // Standard multi-file unified diff without `diff --git` (delimited by `--- ` / `+++ ` / `@@ `).
        var sections: [[String]] = []
        var current: [String] = []
        var i = 0
        while i < lines.count {
            let line = lines[i]
            let isFileHeaderStart = line.hasPrefix("--- ")
                && (i + 1 < lines.count && lines[i + 1].hasPrefix("+++ "))
                && (i + 2 >= lines.count || lines[i + 2].hasPrefix("@@ "))
            if isFileHeaderStart && !current.isEmpty {
                sections.append(current)
                current = [line]
            } else {
                current.append(line)
            }
            i += 1
        }
        if !current.isEmpty {
            sections.append(current)
        }
        return sections
    }

    private static func parseFileSection(
        _ lines: [String],
        baseDirectory: String?,
        computeIntraline: Bool,
        timestamp: Date
    ) -> PCodeFileDiff? {
        var oldPath: String?
        var newPath: String?
        var renameFrom: String?
        var renameTo: String?
        var isNewFile = false
        var isDeletedFile = false
        var isRenamed = false
        var isCopied = false
        var hasMetadataOrHunk = false

        for line in lines {
            if line.hasPrefix("@@ ") && parseHunkHeader(line) != nil {
                hasMetadataOrHunk = true
                break
            } else if line.hasPrefix("diff --git ") {
                hasMetadataOrHunk = true
                if let (aPath, bPath) = parseDiffGitHeader(line) {
                    oldPath = aPath
                    newPath = bPath
                }
            } else if line.hasPrefix("new file mode ") {
                isNewFile = true
                hasMetadataOrHunk = true
            } else if line.hasPrefix("deleted file mode ") {
                isDeletedFile = true
                hasMetadataOrHunk = true
            } else if line.hasPrefix("rename from ") {
                renameFrom = unquoteGitPath(String(line.dropFirst("rename from ".count)))
                isRenamed = true
                hasMetadataOrHunk = true
            } else if line.hasPrefix("rename to ") {
                renameTo = unquoteGitPath(String(line.dropFirst("rename to ".count)))
                isRenamed = true
                hasMetadataOrHunk = true
            } else if line.hasPrefix("copy from ") || line.hasPrefix("copy to ") {
                if line.hasPrefix("copy to ") {
                    renameTo = unquoteGitPath(String(line.dropFirst("copy to ".count)))
                }
                isCopied = true
                hasMetadataOrHunk = true
            } else if line.hasPrefix("--- ") {
                hasMetadataOrHunk = true
                let parsed = parseHeaderFilePath(String(line.dropFirst(4)), stripPrefix: "a/")
                if parsed == nil {
                    isNewFile = true
                } else {
                    oldPath = parsed
                }
            } else if line.hasPrefix("+++ ") {
                hasMetadataOrHunk = true
                let parsed = parseHeaderFilePath(String(line.dropFirst(4)), stripPrefix: "b/")
                if parsed == nil {
                    isDeletedFile = true
                } else {
                    newPath = parsed
                }
            } else if line.hasPrefix("Binary files ") || line.hasPrefix("GIT binary patch") {
                hasMetadataOrHunk = true
                if line.hasPrefix("Binary files "),
                   oldPath == nil && newPath == nil,
                   let (binOld, binNew) = parseBinaryFilesLine(line)
                {
                    oldPath = binOld
                    newPath = binNew
                    if binOld == nil { isNewFile = true }
                    if binNew == nil { isDeletedFile = true }
                }
            }
        }

        guard hasMetadataOrHunk else { return nil }

        let kind: PCodeFileChangeKind
        if isRenamed {
            kind = .renamed
        } else if isNewFile || isCopied {
            kind = .added
        } else if isDeletedFile {
            kind = .deleted
        } else {
            kind = .modified
        }

        let resolvedPath: String
        if let to = renameTo, !to.isEmpty {
            resolvedPath = to
        } else if kind == .deleted, let old = oldPath, !old.isEmpty {
            resolvedPath = old
        } else if let new = newPath, !new.isEmpty {
            resolvedPath = new
        } else if let old = oldPath, !old.isEmpty {
            resolvedPath = old
        } else if let from = renameFrom, !from.isEmpty {
            resolvedPath = from
        } else {
            return nil
        }

        let rawSection = lines.joined(separator: "\n")
        let hunks = parseHunks(rawSection, idPrefix: resolvedPath, computeIntraline: computeIntraline)
        let (added, removed) = countChanges(in: hunks)
        let absolutePath = resolveAbsolutePath(resolvedPath, baseDirectory: baseDirectory)

        return PCodeFileDiff(
            path: resolvedPath,
            absolutePath: absolutePath,
            kind: kind,
            added: added,
            removed: removed,
            hunks: hunks,
            rawPatch: rawSection,
            lastModifiedAt: timestamp
        )
    }

    private static func inferChangeKind(from lines: [String]) -> PCodeFileChangeKind {
        for line in lines {
            if line.hasPrefix("@@ ") { break }
            if line.hasPrefix("rename from ") || line.hasPrefix("rename to ") { return .renamed }
            if line.hasPrefix("new file mode ") || line == "--- /dev/null" { return .added }
            if line.hasPrefix("deleted file mode ") || line == "+++ /dev/null" { return .deleted }
        }
        return .modified
    }

    private static func countChanges(in hunks: [PCodeDiffHunk]) -> (added: Int, removed: Int) {
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

    private static func resolveAbsolutePath(_ path: String, baseDirectory: String?) -> String? {
        if path.hasPrefix("/") {
            return path
        }
        guard let base = baseDirectory, !base.isEmpty else { return nil }
        return (base as NSString).appendingPathComponent(path)
    }

    /// Parse `diff --git a/path b/path` including quoted paths and paths containing spaces.
    private static func parseDiffGitHeader(_ line: String) -> (String?, String?)? {
        let rest = String(line.dropFirst("diff --git ".count))
        guard !rest.isEmpty else { return nil }

        // Handle quoted git paths: `"a/foo bar" "b/foo bar"` (either or both sides may be quoted).
        if rest.hasPrefix("\"") {
            let tokens = parseGitHeaderTokens(rest)
            guard tokens.count >= 2 else { return nil }
            let a = stripGitPrefix(tokens[0], prefix: "a/")
            let b = stripGitPrefix(tokens[1], prefix: "b/")
            return (a, b)
        }

        // Unquoted paths: look for `" b/"` delimiter (preferring balanced split if path contains `" b/"`).
        if let range = rest.range(of: " b/", options: .backwards) {
            let leftRaw = String(rest[..<range.lowerBound])
            let rightRaw = String(rest[rest.index(after: range.lowerBound)...])
            let a = stripGitPrefix(unquoteGitPath(leftRaw), prefix: "a/")
            let b = stripGitPrefix(unquoteGitPath(rightRaw), prefix: "b/")
            return (a, b)
        }

        let parts = rest.split(separator: " ", maxSplits: 1)
        guard parts.count == 2 else { return nil }
        let a = stripGitPrefix(unquoteGitPath(String(parts[0])), prefix: "a/")
        let b = stripGitPrefix(unquoteGitPath(String(parts[1])), prefix: "b/")
        return (a, b)
    }

    private static func parseGitHeaderTokens(_ text: String) -> [String] {
        var tokens: [String] = []
        var idx = text.startIndex
        while idx < text.endIndex {
            while idx < text.endIndex && text[idx] == " " {
                idx = text.index(after: idx)
            }
            guard idx < text.endIndex else { break }
            if text[idx] == "\"" {
                var raw = "\""
                idx = text.index(after: idx)
                var escaped = false
                while idx < text.endIndex {
                    let ch = text[idx]
                    raw.append(ch)
                    idx = text.index(after: idx)
                    if escaped {
                        escaped = false
                    } else if ch == "\\" {
                        escaped = true
                    } else if ch == "\"" {
                        break
                    }
                }
                tokens.append(unquoteGitPath(raw))
            } else {
                let start = idx
                while idx < text.endIndex && text[idx] != " " {
                    idx = text.index(after: idx)
                }
                tokens.append(String(text[start..<idx]))
            }
        }
        return tokens
    }

    /// Parse `--- a/foo.swift\t2026-...` or `+++ /dev/null`. Returns `nil` for `/dev/null`.
    private static func parseHeaderFilePath(_ raw: String, stripPrefix: String) -> String? {
        // Strip optional tab-delimited timestamp used by `diff -u`.
        let beforeTab: String
        if let tabIdx = raw.firstIndex(of: "\t") {
            beforeTab = String(raw[..<tabIdx])
        } else {
            beforeTab = raw
        }
        let unquoted = unquoteGitPath(beforeTab.trimmingCharacters(in: .whitespaces))
        if unquoted == "/dev/null" || unquoted.isEmpty {
            return nil
        }
        return stripGitPrefix(unquoted, prefix: stripPrefix)
    }

    private static func parseBinaryFilesLine(_ line: String) -> (String?, String?)? {
        // Format: `Binary files a/foo.png and b/foo.png differ`
        let prefix = "Binary files "
        let suffix = " differ"
        guard line.hasPrefix(prefix), line.hasSuffix(suffix) else { return nil }
        let inner = String(line.dropFirst(prefix.count).dropLast(suffix.count))
        guard let sep = inner.range(of: " and ") else { return nil }
        let leftRaw = unquoteGitPath(String(inner[..<sep.lowerBound]))
        let rightRaw = unquoteGitPath(String(inner[sep.upperBound...]))
        let left = leftRaw == "/dev/null" ? nil : stripGitPrefix(leftRaw, prefix: "a/")
        let right = rightRaw == "/dev/null" ? nil : stripGitPrefix(rightRaw, prefix: "b/")
        return (left, right)
    }

    private static func stripGitPrefix(_ path: String, prefix: String) -> String {
        if path.hasPrefix(prefix) {
            return String(path.dropFirst(prefix.count))
        }
        return path
    }

    /// Decode git C-style quoted paths (`"a/dir/file\tname.swift"`).
    private static func unquoteGitPath(_ raw: String) -> String {
        guard raw.count >= 2, raw.hasPrefix("\""), raw.hasSuffix("\"") else {
            return raw
        }
        let inner = raw.dropFirst().dropLast()
        var bytes: [UInt8] = []
        bytes.reserveCapacity(inner.utf8.count)
        let utf8 = Array(inner.utf8)
        var i = 0
        while i < utf8.count {
            let b = utf8[i]
            if b == UInt8(ascii: "\\"), i + 1 < utf8.count {
                let next = utf8[i + 1]
                switch next {
                case UInt8(ascii: "\\"):
                    bytes.append(UInt8(ascii: "\\")); i += 2
                case UInt8(ascii: "\""):
                    bytes.append(UInt8(ascii: "\"")); i += 2
                case UInt8(ascii: "n"):
                    bytes.append(UInt8(ascii: "\n")); i += 2
                case UInt8(ascii: "t"):
                    bytes.append(UInt8(ascii: "\t")); i += 2
                case UInt8(ascii: "r"):
                    bytes.append(UInt8(ascii: "\r")); i += 2
                case UInt8(ascii: "0")...UInt8(ascii: "7"):
                    // Up to 3 octal digits representing a raw UTF-8 byte.
                    var val = Int(next - UInt8(ascii: "0"))
                    var consumed = 2
                    if i + 2 < utf8.count, utf8[i + 2] >= UInt8(ascii: "0"), utf8[i + 2] <= UInt8(ascii: "7") {
                        val = val * 8 + Int(utf8[i + 2] - UInt8(ascii: "0"))
                        consumed = 3
                        if i + 3 < utf8.count, utf8[i + 3] >= UInt8(ascii: "0"), utf8[i + 3] <= UInt8(ascii: "7") {
                            val = val * 8 + Int(utf8[i + 3] - UInt8(ascii: "0"))
                            consumed = 4
                        }
                    }
                    bytes.append(UInt8(truncatingIfNeeded: val))
                    i += consumed
                default:
                    bytes.append(next); i += 2
                }
            } else {
                bytes.append(b)
                i += 1
            }
        }
        return String(decoding: bytes, as: UTF8.self)
    }

    // MARK: - Intraline Word-Diff Algorithm

    private struct DiffToken {
        let text: String
        let utf16Range: Range<Int>
        let isWhitespace: Bool
    }

    /// Compute UTF-16 intraline word-diff spans highlighting the changed tokens between `old` and `new`.
    static func computeIntralineSpans(
        old: String,
        new: String
    ) -> (old: [PCodeIntralineSpan], new: [PCodeIntralineSpan]) {
        if old == new {
            return ([], [])
        }
        if old.isEmpty {
            let len = new.utf16.count
            return ([], len > 0 ? [PCodeIntralineSpan(range: 0..<len)] : [])
        }
        if new.isEmpty {
            let len = old.utf16.count
            return (len > 0 ? [PCodeIntralineSpan(range: 0..<len)] : [], [])
        }

        let oldTokens = tokenizeForIntraline(old)
        let newTokens = tokenizeForIntraline(new)

        // Trim common token prefix and suffix so LCS only runs on the changed core.
        var prefix = 0
        let minCount = min(oldTokens.count, newTokens.count)
        while prefix < minCount && oldTokens[prefix].text == newTokens[prefix].text {
            prefix += 1
        }

        var oldEnd = oldTokens.count
        var newEnd = newTokens.count
        while oldEnd > prefix && newEnd > prefix && oldTokens[oldEnd - 1].text == newTokens[newEnd - 1].text {
            oldEnd -= 1
            newEnd -= 1
        }

        let oldCore = Array(oldTokens[prefix..<oldEnd])
        let newCore = Array(newTokens[prefix..<newEnd])

        if oldCore.isEmpty {
            return ([], coalesceRanges(newCore.map(\.utf16Range)))
        }
        if newCore.isEmpty {
            return (coalesceRanges(oldCore.map(\.utf16Range)), [])
        }

        // Guard against quadratic blow-up on extremely long minified lines.
        let m = oldCore.count
        let n = newCore.count
        if m * n > 200_000 {
            let oldSpan = oldCore.first!.utf16Range.lowerBound..<oldCore.last!.utf16Range.upperBound
            let newSpan = newCore.first!.utf16Range.lowerBound..<newCore.last!.utf16Range.upperBound
            return ([PCodeIntralineSpan(range: oldSpan)], [PCodeIntralineSpan(range: newSpan)])
        }

        // Standard bottom-up LCS length table over token arrays.
        let cols = n + 1
        var dp = [Int](repeating: 0, count: (m + 1) * cols)
        for i in stride(from: m - 1, through: 0, by: -1) {
            let rowOffset = i * cols
            let nextRowOffset = (i + 1) * cols
            let oldText = oldCore[i].text
            for j in stride(from: n - 1, through: 0, by: -1) {
                if oldText == newCore[j].text {
                    dp[rowOffset + j] = dp[nextRowOffset + j + 1] + 1
                } else {
                    dp[rowOffset + j] = max(dp[nextRowOffset + j], dp[rowOffset + j + 1])
                }
            }
        }

        var oldChanged: [Range<Int>] = []
        var newChanged: [Range<Int>] = []
        var i = 0
        var j = 0
        while i < m && j < n {
            if oldCore[i].text == newCore[j].text {
                i += 1
                j += 1
            } else if dp[(i + 1) * cols + j] >= dp[i * cols + (j + 1)] {
                oldChanged.append(oldCore[i].utf16Range)
                i += 1
            } else {
                newChanged.append(newCore[j].utf16Range)
                j += 1
            }
        }
        while i < m {
            oldChanged.append(oldCore[i].utf16Range)
            i += 1
        }
        while j < n {
            newChanged.append(newCore[j].utf16Range)
            j += 1
        }

        return (coalesceRanges(oldChanged), coalesceRanges(newChanged))
    }

    /// Pair adjacent deleted and added line runs within a hunk and populate `intralineSpans`.
    private static func applyIntralineSpans(to lines: [PCodeDiffLine]) -> [PCodeDiffLine] {
        var result = lines
        var i = 0
        while i < result.count {
            guard result[i].kind == .deleted else {
                i += 1
                continue
            }

            let delStart = i
            while i < result.count && result[i].kind == .deleted {
                i += 1
            }
            let delEnd = i

            let addStart = i
            while i < result.count && result[i].kind == .added {
                i += 1
            }
            let addEnd = i

            let delCount = delEnd - delStart
            let addCount = addEnd - addStart
            guard delCount > 0 && addCount > 0 else { continue }

            let pairCount = min(delCount, addCount)
            for offset in 0..<pairCount {
                let delIdx = delStart + offset
                let addIdx = addStart + offset
                let oldLine = result[delIdx]
                let newLine = result[addIdx]

                // When counts differ, only pair lines that share at least one non-whitespace token
                // so unrelated insertions/deletions don't get spurious intraline highlights.
                if delCount != addCount && !sharesMeaningfulToken(oldLine.text, newLine.text) {
                    continue
                }

                let (oldSpans, newSpans) = computeIntralineSpans(old: oldLine.text, new: newLine.text)
                if !oldSpans.isEmpty {
                    result[delIdx] = PCodeDiffLine(
                        id: oldLine.id,
                        kind: oldLine.kind,
                        oldLineNumber: oldLine.oldLineNumber,
                        newLineNumber: oldLine.newLineNumber,
                        text: oldLine.text,
                        intralineSpans: oldSpans
                    )
                }
                if !newSpans.isEmpty {
                    result[addIdx] = PCodeDiffLine(
                        id: newLine.id,
                        kind: newLine.kind,
                        oldLineNumber: newLine.oldLineNumber,
                        newLineNumber: newLine.newLineNumber,
                        text: newLine.text,
                        intralineSpans: newSpans
                    )
                }
            }
        }
        return result
    }

    private static func sharesMeaningfulToken(_ a: String, _ b: String) -> Bool {
        let aWords = Set(tokenizeForIntraline(a).filter { !$0.isWhitespace }.map(\.text))
        guard !aWords.isEmpty else { return false }
        for tok in tokenizeForIntraline(b) where !tok.isWhitespace {
            if aWords.contains(tok.text) {
                return true
            }
        }
        return false
    }

    private static func tokenizeForIntraline(_ string: String) -> [DiffToken] {
        var tokens: [DiffToken] = []
        var utf16Offset = 0
        var idx = string.startIndex

        while idx < string.endIndex {
            let ch = string[idx]
            if isWordCharacter(ch) {
                let start = idx
                var len = 0
                while idx < string.endIndex && isWordCharacter(string[idx]) {
                    len += string[idx].utf16.count
                    idx = string.index(after: idx)
                }
                let text = String(string[start..<idx])
                tokens.append(DiffToken(text: text, utf16Range: utf16Offset..<(utf16Offset + len), isWhitespace: false))
                utf16Offset += len
            } else if ch.isWhitespace {
                let start = idx
                var len = 0
                while idx < string.endIndex && string[idx].isWhitespace {
                    len += string[idx].utf16.count
                    idx = string.index(after: idx)
                }
                let text = String(string[start..<idx])
                tokens.append(DiffToken(text: text, utf16Range: utf16Offset..<(utf16Offset + len), isWhitespace: true))
                utf16Offset += len
            } else {
                // Single punctuation/symbol characters keep delimiters like `(`, `)`, `,` aligned.
                let len = ch.utf16.count
                let next = string.index(after: idx)
                tokens.append(DiffToken(text: String(ch), utf16Range: utf16Offset..<(utf16Offset + len), isWhitespace: false))
                utf16Offset += len
                idx = next
            }
        }
        return tokens
    }

    private static func isWordCharacter(_ ch: Character) -> Bool {
        ch == "_" || ch.isLetter || ch.isNumber
    }

    private static func coalesceRanges(_ ranges: [Range<Int>]) -> [PCodeIntralineSpan] {
        guard !ranges.isEmpty else { return [] }
        var merged: [Range<Int>] = []
        for range in ranges where !range.isEmpty {
            if let last = merged.last, last.upperBound == range.lowerBound {
                merged[merged.count - 1] = last.lowerBound..<range.upperBound
            } else {
                merged.append(range)
            }
        }
        return merged.map { PCodeIntralineSpan(range: $0, emphasized: true) }
    }

    // MARK: - Side-by-Side Row Pairing

    /// Convert a unified hunk into paired rows suitable for side-by-side rendering.
    static func sideBySideRows(for hunk: PCodeDiffHunk) -> [PCodeSideBySideRow] {
        var rows: [PCodeSideBySideRow] = []
        let lines = hunk.lines
        var i = 0

        while i < lines.count {
            let line = lines[i]
            switch line.kind {
            case .context:
                rows.append(PCodeSideBySideRow(
                    id: "\(hunk.id)-sbs-\(rows.count)",
                    left: line,
                    right: line
                ))
                i += 1

            case .meta:
                rows.append(PCodeSideBySideRow(
                    id: "\(hunk.id)-sbs-\(rows.count)",
                    left: line,
                    right: nil,
                    isMeta: true
                ))
                i += 1

            case .deleted:
                var deleted: [PCodeDiffLine] = []
                while i < lines.count && lines[i].kind == .deleted {
                    deleted.append(lines[i])
                    i += 1
                }
                var added: [PCodeDiffLine] = []
                while i < lines.count && lines[i].kind == .added {
                    added.append(lines[i])
                    i += 1
                }
                let count = max(deleted.count, added.count)
                for offset in 0..<count {
                    rows.append(PCodeSideBySideRow(
                        id: "\(hunk.id)-sbs-\(rows.count)",
                        left: offset < deleted.count ? deleted[offset] : nil,
                        right: offset < added.count ? added[offset] : nil
                    ))
                }

            case .added:
                rows.append(PCodeSideBySideRow(
                    id: "\(hunk.id)-sbs-\(rows.count)",
                    left: nil,
                    right: line
                ))
                i += 1
            }
        }

        return rows
    }

    // MARK: - On-Disk Context Expansion

    /// Load hidden context lines from disk between `afterLine` and `beforeLine` (1-based, exclusive).
    ///
    /// - Parameters:
    ///   - filePath: Absolute path to the file on disk.
    ///   - afterLine: Last visible 1-based line number before the gap (`0` when expanding above line 1).
    ///   - beforeLine: First visible 1-based line number after the gap (`nil` to expand down to EOF).
    ///   - limit: Maximum number of lines to return (default `20`).
    ///   - fromBottom: When `true`, returns up to `limit` lines closest to `beforeLine` instead of `afterLine`.
    ///   - oldLineOffset: Delta (`oldLine - newLine`) applied to `oldLineNumber` for aligned unified display.
    static func expandContext(
        filePath: String,
        afterLine: Int,
        beforeLine: Int? = nil,
        limit: Int = 20,
        fromBottom: Bool = false,
        oldLineOffset: Int = 0
    ) -> [PCodeDiffLine] {
        guard limit > 0,
              !filePath.isEmpty,
              let data = FileManager.default.contents(atPath: filePath),
              let contents = String(data: data, encoding: .utf8)
        else {
            return []
        }

        let fileLines = splitFileLinesPreservingContent(contents)
        guard !fileLines.isEmpty else { return [] }

        let firstAvailable = max(1, afterLine + 1)
        let lastAvailable: Int
        if let beforeLine {
            lastAvailable = min(fileLines.count, beforeLine - 1)
        } else {
            lastAvailable = fileLines.count
        }

        guard firstAvailable <= lastAvailable else { return [] }

        let sliceStart: Int
        let sliceEnd: Int
        if fromBottom {
            sliceEnd = lastAvailable
            sliceStart = max(firstAvailable, lastAvailable - limit + 1)
        } else {
            sliceStart = firstAvailable
            sliceEnd = min(lastAvailable, firstAvailable + limit - 1)
        }

        var result: [PCodeDiffLine] = []
        result.reserveCapacity(sliceEnd - sliceStart + 1)
        for lineNumber in sliceStart...sliceEnd {
            let text = fileLines[lineNumber - 1]
            let oldNumber = max(1, lineNumber + oldLineOffset)
            result.append(PCodeDiffLine(
                id: "expanded-\(filePath.hashValue)-\(lineNumber)",
                kind: .context,
                oldLineNumber: oldNumber,
                newLineNumber: lineNumber,
                text: text
            ))
        }
        return result
    }

    // MARK: - Line Splitting Helpers

    private static func splitLines(_ text: String) -> [String] {
        guard !text.isEmpty else { return [] }
        let normalized = text.replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
        var lines = normalized.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        if lines.last == "" {
            lines.removeLast()
        }
        return lines
    }

    private static func splitFileLinesPreservingContent(_ text: String) -> [String] {
        guard !text.isEmpty else { return [] }
        let normalized = text.replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
        var lines = normalized.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        if normalized.hasSuffix("\n"), lines.last == "" {
            lines.removeLast()
        }
        return lines
    }
}

/// File navigation helper supporting `[` / `]` next and previous file traversal.
enum PCodeDiffNavigation {
    /// Return the path of the next changed file after `current` (wrapping to the first by default).
    static func nextFilePath(
        in diffs: [PCodeFileDiff],
        current: String?,
        wrap: Bool = true
    ) -> String? {
        guard !diffs.isEmpty else { return nil }
        guard let current, let idx = diffs.firstIndex(where: { $0.path == current }) else {
            return diffs.first?.path
        }
        let nextIdx = idx + 1
        if nextIdx < diffs.count {
            return diffs[nextIdx].path
        }
        return wrap ? diffs.first?.path : diffs.last?.path
    }

    /// Return the path of the previous changed file before `current` (wrapping to the last by default).
    static func previousFilePath(
        in diffs: [PCodeFileDiff],
        current: String?,
        wrap: Bool = true
    ) -> String? {
        guard !diffs.isEmpty else { return nil }
        guard let current, let idx = diffs.firstIndex(where: { $0.path == current }) else {
            return diffs.last?.path
        }
        let prevIdx = idx - 1
        if prevIdx >= 0 {
            return diffs[prevIdx].path
        }
        return wrap ? diffs.last?.path : diffs.first?.path
    }

    /// Advance `state.focusedFilePath` to the next file and uncollapse it.
    @MainActor
    @discardableResult
    static func selectNextFile(in diffs: [PCodeFileDiff], state: PCodeInspectorState, wrap: Bool = true) -> String? {
        guard let next = nextFilePath(in: diffs, current: state.focusedFilePath, wrap: wrap) else { return nil }
        state.collapsedFiles.remove(next)
        state.focusedFilePath = next
        return next
    }

    /// Move `state.focusedFilePath` to the previous file and uncollapse it.
    @MainActor
    @discardableResult
    static func selectPreviousFile(in diffs: [PCodeFileDiff], state: PCodeInspectorState, wrap: Bool = true) -> String? {
        guard let prev = previousFilePath(in: diffs, current: state.focusedFilePath, wrap: wrap) else { return nil }
        state.collapsedFiles.remove(prev)
        state.focusedFilePath = prev
        return prev
    }
}
