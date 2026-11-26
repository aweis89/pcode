import Foundation
import Testing
@testable import Ghostty

struct PCodeDiffParserTests {
    // MARK: - Hunk Header & Single-File Parsing

    @Test func parsesUnifiedHunkHeaderWithAndWithoutCounts() {
        let full = PCodeDiffParser.parseHunkHeader("@@ -10,4 +12,6 @@ func refresh(token: String)")
        #expect(full?.oldStart == 10)
        #expect(full?.oldCount == 4)
        #expect(full?.newStart == 12)
        #expect(full?.newCount == 6)
        #expect(full?.context == "func refresh(token: String)")

        let omittedCounts = PCodeDiffParser.parseHunkHeader("@@ -1 +1,2 @@")
        #expect(omittedCounts?.oldStart == 1)
        #expect(omittedCounts?.oldCount == 1)
        #expect(omittedCounts?.newStart == 1)
        #expect(omittedCounts?.newCount == 2)
        #expect(omittedCounts?.context == "")

        let zeroOld = PCodeDiffParser.parseHunkHeader("@@ -0,0 +1,5 @@")
        #expect(zeroOld?.oldStart == 0)
        #expect(zeroOld?.oldCount == 0)
        #expect(zeroOld?.newStart == 1)
        #expect(zeroOld?.newCount == 5)

        #expect(PCodeDiffParser.parseHunkHeader("not a header") == nil)
    }

    @Test func parsesSingleFilePatchWithLineNumbersAndNoNewlineMarker() {
        let patch = """
        --- a/src/Auth.swift
        +++ b/src/Auth.swift
        @@ -40,4 +40,5 @@ func authenticate() {
         let token = loadToken()
        -return nil
        +let refreshed = refresh(token)
        +return refreshed
         // end
        \\ No newline at end of file
        """

        let files = PCodeDiffParser.parseFiles(patch, baseDirectory: "/repo")
        #expect(files.count == 1)

        let file = files[0]
        #expect(file.path == "src/Auth.swift")
        #expect(file.absolutePath == "/repo/src/Auth.swift")
        #expect(file.kind == .modified)
        #expect(file.added == 2)
        #expect(file.removed == 1)
        #expect(file.hunks.count == 1)

        let lines = file.hunks[0].lines
        #expect(lines.count == 6)

        #expect(lines[0].kind == .context)
        #expect(lines[0].oldLineNumber == 40)
        #expect(lines[0].newLineNumber == 40)
        #expect(lines[0].text == "let token = loadToken()")

        #expect(lines[1].kind == .deleted)
        #expect(lines[1].oldLineNumber == 41)
        #expect(lines[1].newLineNumber == nil)
        #expect(lines[1].text == "return nil")

        #expect(lines[2].kind == .added)
        #expect(lines[2].oldLineNumber == nil)
        #expect(lines[2].newLineNumber == 41)
        #expect(lines[2].text == "let refreshed = refresh(token)")

        #expect(lines[3].kind == .added)
        #expect(lines[3].oldLineNumber == nil)
        #expect(lines[3].newLineNumber == 42)
        #expect(lines[3].text == "return refreshed")

        #expect(lines[4].kind == .context)
        #expect(lines[4].oldLineNumber == 42)
        #expect(lines[4].newLineNumber == 43)

        #expect(lines[5].kind == .meta)
        #expect(lines[5].oldLineNumber == nil)
        #expect(lines[5].newLineNumber == nil)
        #expect(lines[5].text.contains("No newline at end of file"))
    }

    @Test func parsesHunkOnlyPatchAndCRLFLineEndings() {
        let crlfPatch = "@@ -1,2 +1,2 @@\r\n-let a = 1\r\n+let a = 2\r\n let b = 3\r\n"
        let hunks = PCodeDiffParser.parseHunks(crlfPatch)
        #expect(hunks.count == 1)
        #expect(hunks[0].lines.count == 3)
        #expect(hunks[0].lines[0].text == "let a = 1")
        #expect(hunks[0].lines[1].text == "let a = 2")
        #expect(hunks[0].lines[2].text == "let b = 3")
    }

    // MARK: - Multi-File Git Diff Parsing

    @Test func parsesMultiFileGitDiffWithNewDeletedRenamedAndModifiedFiles() {
        let patch = """
        diff --git a/Sources/NewFile.swift b/Sources/NewFile.swift
        new file mode 100644
        index 0000000..1111111
        --- /dev/null
        +++ b/Sources/NewFile.swift
        @@ -0,0 +1,2 @@
        +struct NewFile {}
        +let version = 1
        diff --git a/Sources/OldFile.swift b/Sources/OldFile.swift
        deleted file mode 100644
        index 2222222..0000000
        --- a/Sources/OldFile.swift
        +++ /dev/null
        @@ -1,2 +0,0 @@
        -struct OldFile {}
        -let legacy = true
        diff --git a/Sources/Before.swift b/Sources/After.swift
        similarity index 88%
        rename from Sources/Before.swift
        rename to Sources/After.swift
        --- a/Sources/Before.swift
        +++ b/Sources/After.swift
        @@ -1,2 +1,2 @@
        -struct Before {}
        +struct After {}
         let shared = true
        diff --git "a/Docs/My Notes.md" "b/Docs/My Notes.md"
        index 3333333..4444444 100644
        --- "a/Docs/My Notes.md"
        +++ "b/Docs/My Notes.md"
        @@ -1 +1 @@
        -Hello
        +Hello world
        """

        let files = PCodeDiffParser.parseFiles(patch, baseDirectory: "/workspace")
        #expect(files.count == 4)

        #expect(files[0].path == "Sources/NewFile.swift")
        #expect(files[0].kind == .added)
        #expect(files[0].added == 2)
        #expect(files[0].removed == 0)

        #expect(files[1].path == "Sources/OldFile.swift")
        #expect(files[1].kind == .deleted)
        #expect(files[1].added == 0)
        #expect(files[1].removed == 2)

        #expect(files[2].path == "Sources/After.swift")
        #expect(files[2].kind == .renamed)
        #expect(files[2].added == 1)
        #expect(files[2].removed == 1)

        #expect(files[3].path == "Docs/My Notes.md")
        #expect(files[3].absolutePath == "/workspace/Docs/My Notes.md")
        #expect(files[3].kind == .modified)
        #expect(files[3].added == 1)
        #expect(files[3].removed == 1)
    }

    @Test func handlesEmptyAndBinaryFilesGracefully() {
        #expect(PCodeDiffParser.parseFiles("").isEmpty)
        #expect(PCodeDiffParser.parseHunks("").isEmpty)

        let patch = """
        diff --git a/Assets/logo.png b/Assets/logo.png
        new file mode 100644
        index 0000000..9999999
        Binary files /dev/null and b/Assets/logo.png differ
        diff --git a/empty.txt b/empty.txt
        new file mode 100644
        index 0000000..e69de29
        """

        let files = PCodeDiffParser.parseFiles(patch)
        #expect(files.count == 2)

        #expect(files[0].path == "Assets/logo.png")
        #expect(files[0].kind == .added)
        #expect(files[0].hunks.isEmpty)
        #expect(PCodeDiffParser.isBinaryPatch(files[0].rawPatch))

        #expect(files[1].path == "empty.txt")
        #expect(files[1].kind == .added)
        #expect(files[1].hunks.isEmpty)
        #expect(!PCodeDiffParser.isBinaryPatch(files[1].rawPatch))
    }

    // MARK: - Intraline Word-Diff Spans

    @Test func computesExactUTF16IntralineWordDiffSpans() {
        let old = "return refresh(oldToken, retry: false)"
        let new = "return refresh(newToken, retry: true)"
        let spans = PCodeDiffParser.computeIntralineSpans(old: old, new: new)

        let oldNS = old as NSString
        let newNS = new as NSString
        let oldChanged = spans.old.map { oldNS.substring(with: NSRange(location: $0.range.lowerBound, length: $0.range.count)) }
        let newChanged = spans.new.map { newNS.substring(with: NSRange(location: $0.range.lowerBound, length: $0.range.count)) }

        #expect(oldChanged == ["oldToken", "false"])
        #expect(newChanged == ["newToken", "true"])

        // Identical strings produce no spans.
        let identical = PCodeDiffParser.computeIntralineSpans(old: "same", new: "same")
        #expect(identical.old.isEmpty)
        #expect(identical.new.isEmpty)
    }

    @Test func computesUTF16SafeSpansWithEmojiAndUnicode() {
        let old = "let badge = \"🎉 old\""
        let new = "let badge = \"🎉 new\""
        let spans = PCodeDiffParser.computeIntralineSpans(old: old, new: new)

        let oldNS = old as NSString
        let newNS = new as NSString
        #expect(spans.old.count == 1)
        #expect(spans.new.count == 1)
        #expect(oldNS.substring(with: NSRange(location: spans.old[0].range.lowerBound, length: spans.old[0].range.count)) == "old")
        #expect(newNS.substring(with: NSRange(location: spans.new[0].range.lowerBound, length: spans.new[0].range.count)) == "new")
    }

    @Test func populatesIntralineSpansOnAdjacentHunkLinesOnly() {
        let patch = """
        @@ -1,3 +1,4 @@
        -let count = 1
        +let count = 2
         let unchanged = true
        +let pureAddition = 3
        """
        let hunks = PCodeDiffParser.parseHunks(patch)
        #expect(hunks.count == 1)

        let lines = hunks[0].lines
        #expect(lines[0].kind == .deleted)
        #expect(lines[1].kind == .added)
        #expect(!lines[0].intralineSpans.isEmpty)
        #expect(!lines[1].intralineSpans.isEmpty)

        // Standalone added line has no deleted counterpart so no intraline spans.
        #expect(lines[3].kind == .added)
        #expect(lines[3].intralineSpans.isEmpty)
    }

    // MARK: - Side-by-Side Pairing

    @Test func pairsSideBySideRowsAndPadsUnequalChangeBlocks() {
        let patch = """
        @@ -1,3 +1,4 @@
         let a = 1
        -let b = 2
        +let b = 20
        +let c = 30
         let d = 4
        """
        let hunks = PCodeDiffParser.parseHunks(patch)
        let rows = PCodeDiffParser.sideBySideRows(for: hunks[0])

        #expect(rows.count == 4)
        #expect(rows[0].left?.text == "let a = 1")
        #expect(rows[0].right?.text == "let a = 1")
        #expect(rows[1].left?.text == "let b = 2")
        #expect(rows[1].right?.text == "let b = 20")
        #expect(rows[2].left == nil)
        #expect(rows[2].right?.text == "let c = 30")
        #expect(rows[3].left?.text == "let d = 4")
        #expect(rows[3].right?.text == "let d = 4")
    }

    // MARK: - Context Expansion from Disk

    @Test func expandsHiddenContextLinesFromDisk() throws {
        let tmpDir = FileManager.default.temporaryDirectory
        let fileURL = tmpDir.appendingPathComponent("pcode-diff-expand-\(UUID().uuidString).txt")
        let sampleLines = (1...30).map { "line \($0)" }
        try (sampleLines.joined(separator: "\n") + "\n").write(to: fileURL, atomically: true, encoding: .utf8)
        defer { try? FileManager.default.removeItem(at: fileURL) }

        // Forward expansion between hunks (lines 6..10).
        let mid = PCodeDiffParser.expandContext(
            filePath: fileURL.path,
            afterLine: 5,
            beforeLine: 25,
            limit: 5
        )
        #expect(mid.count == 5)
        #expect(mid.first?.newLineNumber == 6)
        #expect(mid.first?.text == "line 6")
        #expect(mid.last?.newLineNumber == 10)
        #expect(mid.last?.text == "line 10")

        // Upward expansion above hunk 1 (lines 7..10 when beforeLine is 11).
        let top = PCodeDiffParser.expandContext(
            filePath: fileURL.path,
            afterLine: 0,
            beforeLine: 11,
            limit: 4,
            fromBottom: true
        )
        #expect(top.map(\.newLineNumber) == [7, 8, 9, 10])
        #expect(top.last?.text == "line 10")

        // Expansion to EOF.
        let tail = PCodeDiffParser.expandContext(
            filePath: fileURL.path,
            afterLine: 27,
            beforeLine: nil,
            limit: 10
        )
        #expect(tail.map(\.newLineNumber) == [28, 29, 30])

        // Non-existent file returns empty.
        #expect(PCodeDiffParser.expandContext(filePath: "/nonexistent/pcode/file.txt", afterLine: 0, beforeLine: 5, limit: 5).isEmpty)
    }

    // MARK: - File Navigation Helper

    @Test func navigatesNextAndPreviousChangedFilesWithWrapping() {
        let diffs = [
            PCodeFileDiff(path: "a.swift"),
            PCodeFileDiff(path: "b.swift"),
            PCodeFileDiff(path: "c.swift"),
        ]

        #expect(PCodeDiffNavigation.nextFilePath(in: [], current: nil) == nil)
        #expect(PCodeDiffNavigation.nextFilePath(in: diffs, current: nil) == "a.swift")
        #expect(PCodeDiffNavigation.nextFilePath(in: diffs, current: "a.swift") == "b.swift")
        #expect(PCodeDiffNavigation.nextFilePath(in: diffs, current: "c.swift") == "a.swift")

        #expect(PCodeDiffNavigation.previousFilePath(in: diffs, current: nil) == "c.swift")
        #expect(PCodeDiffNavigation.previousFilePath(in: diffs, current: "b.swift") == "a.swift")
        #expect(PCodeDiffNavigation.previousFilePath(in: diffs, current: "a.swift") == "c.swift")
    }

    @Test @MainActor func navigationStateHelpersUpdateFocusAndUncollapse() {
        let diffs = [
            PCodeFileDiff(path: "a.swift"),
            PCodeFileDiff(path: "b.swift"),
        ]
        let state = PCodeInspectorState()
        state.collapsedFiles = ["a.swift", "b.swift"]

        let first = PCodeDiffNavigation.selectNextFile(in: diffs, state: state)
        #expect(first == "a.swift")
        #expect(state.focusedFilePath == "a.swift")
        #expect(!state.collapsedFiles.contains("a.swift"))

        let second = PCodeDiffNavigation.selectNextFile(in: diffs, state: state)
        #expect(second == "b.swift")
        #expect(state.focusedFilePath == "b.swift")
        #expect(!state.collapsedFiles.contains("b.swift"))

        let back = PCodeDiffNavigation.selectPreviousFile(in: diffs, state: state)
        #expect(back == "a.swift")
        #expect(state.focusedFilePath == "a.swift")
    }

    // MARK: - Syntax Store Bounded FIFO Eviction

    private final class CountingSyntaxHighlighter: PCodeSyntaxHighlighter, @unchecked Sendable {
        var allowSyncHit = true
        var syncLookupCounts: [String: Int] = [:]

        func cachedTokens(code: String, language: String?) -> [PCodeSyntaxToken]? {
            syncLookupCounts[code, default: 0] += 1
            guard allowSyncHit else { return nil }
            return [PCodeSyntaxToken(content: code, kind: .plain)]
        }

        func tokenize(code: String, language: String?) async -> [PCodeSyntaxToken] {
            [PCodeSyntaxToken(content: code, kind: .plain)]
        }
    }

    @Test @MainActor func diffSyntaxStoreEvictsOldestEntriesFIFOAtCapacity() {
        let highlighter = CountingSyntaxHighlighter()
        let store = PCodeDiffSyntaxStore(highlighter: highlighter)
        let cap = PCodeDiffSyntaxStore.maxCacheEntries

        // Fill past capacity by 1 entry (0...cap).
        for idx in 0...cap {
            let result = store.lineTokens(for: ["let v\(idx) = \(idx)"], language: "swift")
            #expect(result?.count == 1)
        }

        // Disable highlighter sync hits so only store-cached entries return non-nil.
        highlighter.allowSyncHit = false

        // Entries 1...cap must still be cached (not wiped by a full removeAll()).
        for idx in 1...cap {
            let code = "let v\(idx) = \(idx)"
            let before = highlighter.syncLookupCounts[code, default: 0]
            let cached = store.lineTokens(for: [code], language: "swift")
            #expect(cached?.count == 1)
            #expect(highlighter.syncLookupCounts[code, default: 0] == before)
        }

        // Oldest entry (0) must have been evicted by FIFO.
        let evicted = store.lineTokens(for: ["let v0 = 0"], language: "swift")
        #expect(evicted == nil)
    }
}
