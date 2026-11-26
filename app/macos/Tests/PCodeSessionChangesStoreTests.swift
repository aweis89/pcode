import Foundation
import Testing
@testable import Ghostty

struct PCodeSessionChangesStoreTests {
    // MARK: - Streaming & Ordering

    @Test @MainActor
    func editPreviewToCompletedLifecycleAndOrdering() {
        let store = PCodeSessionChangesStore(sessionDirectory: "/tmp/demo-project")
        let t1 = Date(timeIntervalSince1970: 1_000)
        let t2 = Date(timeIntervalSince1970: 1_010)
        let t3 = Date(timeIntervalSince1970: 1_020)
        let t4 = Date(timeIntervalSince1970: 1_030)

        // First file completed at t1
        store.apply(
            .editCompleted(
                callID: "call-1",
                parentCallID: "",
                path: "src/Alpha.swift",
                operation: "edit",
                patch: """
                @@ -1,2 +1,2 @@
                 let a = 1
                -let b = 2
                +let b = 3
                """,
                added: 1,
                removed: 1,
                truncated: false
            ),
            now: t1
        )
        #expect(store.sessionFiles.map(\.path) == ["src/Alpha.swift"])
        #expect(store.sessionFiles[0].isStreaming == false)
        #expect(store.sessionFiles[0].editCount == 1)

        // Non-file code preview (`kind == "code"`) is ignored
        store.apply(
            .editPreview(callID: "edit-preview:9", path: "run_code", text: "print(1)", kind: "code"),
            now: t2
        )
        #expect(store.sessionFiles.count == 1)

        // Streaming preview for Beta.swift moves it to the top with isStreaming = true
        store.apply(
            .editPreview(
                callID: "edit-preview:0",
                path: "src/Beta.swift",
                text: "-oldLine\n+newLine",
                kind: "edit"
            ),
            now: t2
        )
        #expect(store.sessionFiles.map(\.path) == ["src/Beta.swift", "src/Alpha.swift"])
        #expect(store.sessionFiles[0].isStreaming == true)
        #expect(store.sessionFiles[0].lastModifiedAt == t2)
        #expect(!store.sessionFiles[0].hunks.isEmpty)

        // Completing Beta.swift with a different runtime callID settles the streaming flag
        store.apply(
            .editCompleted(
                callID: "call-2",
                parentCallID: "",
                path: "src/Beta.swift",
                operation: "edit",
                patch: """
                @@ -1,1 +1,1 @@
                -oldLine
                +newLine
                """,
                added: 1,
                removed: 1,
                truncated: false
            ),
            now: t3
        )
        #expect(store.sessionFiles.map(\.path) == ["src/Beta.swift", "src/Alpha.swift"])
        #expect(store.sessionFiles[0].isStreaming == false)
        #expect(store.sessionFiles[0].editCount == 1)
        #expect(store.sessionFiles[0].lastModifiedAt == t3)

        // Previewing Alpha.swift again bumps it back above Beta.swift
        store.apply(
            .editPreview(callID: "edit-preview:1", path: "src/Alpha.swift", text: "+extra", kind: "edit"),
            now: t4
        )
        #expect(store.sessionFiles.map(\.path) == ["src/Alpha.swift", "src/Beta.swift"])
        #expect(store.sessionFiles[0].isStreaming == true)

        // Empty-path preview clears the active preview without removing completed edits
        store.apply(
            .editPreview(callID: "edit-preview:1", path: "", text: "", kind: "edit"),
            now: t4
        )
        #expect(store.sessionFiles[0].path == "src/Alpha.swift")
        #expect(store.sessionFiles[0].isStreaming == false)
        #expect(store.sessionFiles[0].editCount == 1)
    }

    // MARK: - Aggregation & Single Edit Lookup

    @Test @MainActor
    func multipleEditsAggregateAndPreserveSingleEditLookup() {
        let store = PCodeSessionChangesStore(sessionDirectory: "/workspace/repo")
        let t1 = Date(timeIntervalSince1970: 2_000)
        let t2 = Date(timeIntervalSince1970: 2_005)

        let patch1 = """
        @@ -1,2 +1,3 @@
         struct Auth {}
        +let token = "abc"
        """
        let patch2 = """
        @@ -1,2 +1,2 @@
        -let token = "abc"
        +let token = "xyz"
        """

        store.apply(
            .editCompleted(
                callID: "c1",
                parentCallID: "",
                path: "/workspace/repo/Sources/Auth.swift",
                operation: "create",
                patch: patch1,
                added: 3,
                removed: 0,
                truncated: false
            ),
            now: t1
        )

        // Capture edit 0's hunk and line IDs before a second edit arrives
        #expect(store.sessionFiles.count == 1)
        let firstEditHunkIDs = store.sessionFiles[0].hunks.map(\.id)
        let firstEditLineIDs = store.sessionFiles[0].hunks.flatMap { $0.lines.map(\.id) }
        #expect(firstEditHunkIDs.count == 1)

        store.apply(
            .editCompleted(
                callID: "c2",
                parentCallID: "",
                path: "./Sources/Auth.swift",
                operation: "edit",
                patch: patch2,
                added: 1,
                removed: 1,
                truncated: true
            ),
            now: t2
        )

        #expect(store.sessionFiles.count == 1)
        let aggregated = store.sessionFiles[0]
        #expect(aggregated.path == "Sources/Auth.swift")
        #expect(aggregated.absolutePath == "/workspace/repo/Sources/Auth.swift")
        #expect(aggregated.kind == .added)
        #expect(aggregated.editCount == 2)
        #expect(aggregated.added == 4)
        #expect(aggregated.removed == 1)
        #expect(aggregated.truncated == true)
        #expect(aggregated.callIDs == ["c1", "c2"])
        #expect(aggregated.lastModifiedAt == t2)
        #expect(aggregated.hunks.count == 2)
        // Edit 0's hunk and line IDs remain stable when a second edit is added
        #expect(aggregated.hunks[0].id == firstEditHunkIDs[0])
        #expect(aggregated.hunks[0].lines.map(\.id) == firstEditLineIDs)
        // Combined hunk IDs remain unique for SwiftUI ForEach
        #expect(Set(aggregated.hunks.map(\.id)).count == 2)

        // Single-edit scope retrieves each edit individually
        let single1 = store.files(for: .singleEdit(callID: "c1"))
        #expect(single1.count == 1)
        #expect(single1[0].added == 3)
        #expect(single1[0].removed == 0)
        #expect(single1[0].kind == .added)
        #expect(single1[0].rawPatch == patch1)

        let single2 = store.files(for: .singleEdit(callID: "c2"))
        #expect(single2.count == 1)
        #expect(single2[0].added == 1)
        #expect(single2[0].removed == 1)
        #expect(single2[0].kind == .modified)
        #expect(single2[0].truncated == true)
        #expect(single2[0].rawPatch == patch2)

        #expect(store.files(for: .singleEdit(callID: "missing")).isEmpty)
    }

    // MARK: - Sidebar Cap & Stats

    @Test @MainActor
    func sidebarFilesCapOverflowAndTotalStats() {
        let store = PCodeSessionChangesStore(sessionDirectory: "/tmp/proj")
        let baseTime = Date(timeIntervalSince1970: 3_000)

        for i in 1...8 {
            store.apply(
                .editCompleted(
                    callID: "call-\(i)",
                    parentCallID: "",
                    path: "file\(i).swift",
                    operation: "edit",
                    patch: "@@ -1,1 +1,1 @@\n-old\n+new",
                    added: i,
                    removed: 1,
                    truncated: false
                ),
                now: baseTime.addingTimeInterval(TimeInterval(i))
            )
        }

        let capped = store.sidebarFiles(limit: 6)
        #expect(capped.visible.count == 6)
        #expect(capped.overflowCount == 2)
        #expect(capped.visible.first?.path == "file8.swift")
        #expect(capped.visible.last?.path == "file3.swift")

        let all = store.sidebarFiles(limit: 10)
        #expect(all.visible.count == 8)
        #expect(all.overflowCount == 0)

        let stats = store.sessionStats
        #expect(stats.added == 36) // 1+2+...+8
        #expect(stats.removed == 8)
        #expect(store.totalStats(for: .session) == stats)
    }

    // MARK: - History Replay & Reset

    @Test @MainActor
    func historyReplayAndConversationReset() {
        let store = PCodeSessionChangesStore(sessionDirectory: "/tmp/proj")
        let t0 = Date(timeIntervalSince1970: 4_000)

        // Seed stale state that history must wipe
        store.apply(
            .editCompleted(
                callID: "stale",
                parentCallID: "",
                path: "Stale.swift",
                operation: "edit",
                patch: "",
                added: 5,
                removed: 2,
                truncated: false
            ),
            now: t0
        )
        #expect(store.sessionFiles.count == 1)

        // Replay history containing two completed edits and one dangling preview
        let events: [PCodeHostEvent] = [
            .editCompleted(
                callID: "h1",
                parentCallID: "",
                path: "First.swift",
                operation: "edit",
                patch: "@@ -1,1 +1,1 @@\n-a\n+b",
                added: 1,
                removed: 1,
                truncated: false
            ),
            .editCompleted(
                callID: "h2",
                parentCallID: "",
                path: "Second.swift",
                operation: "create",
                patch: "@@ -0,0 +1,2 @@\n+line1\n+line2",
                added: 2,
                removed: 0,
                truncated: false
            ),
            .editPreview(
                callID: "edit-preview:0",
                path: "Unfinished.swift",
                text: "+temp",
                kind: "edit"
            ),
        ]

        store.apply(.history(events), now: t0.addingTimeInterval(10))

        // Most recently replayed completed edit comes first; stale and dangling preview are gone
        #expect(store.sessionFiles.map(\.path) == ["Second.swift", "First.swift"])
        #expect(store.files(for: .singleEdit(callID: "stale")).isEmpty)
        #expect(store.files(for: .singleEdit(callID: "h1")).count == 1)
        #expect(store.files(for: .singleEdit(callID: "h2")).count == 1)

        // Conversation reset clears everything
        store.apply(.conversationReset, now: t0.addingTimeInterval(20))
        #expect(store.sessionFiles.isEmpty)
        #expect(store.files(for: .singleEdit(callID: "h1")).isEmpty)
        #expect(store.sessionStats == (0, 0))
    }

    // MARK: - Injectable Hunk Parser

    @Test @MainActor
    func usesInjectableParseHunksClosure() {
        let sentinelHunk = PCodeDiffHunk(
            id: "custom-hunk",
            header: "@@ -1,1 +1,1 @@",
            oldStart: 1,
            oldCount: 1,
            newStart: 1,
            newCount: 1,
            lines: [PCodeDiffLine(id: "custom-line", kind: .added, newLineNumber: 1, text: "parsed")]
        )
        let store = PCodeSessionChangesStore(
            sessionDirectory: "/tmp/proj",
            parseHunks: { patch in patch.isEmpty ? [] : [sentinelHunk] }
        )

        store.apply(
            .editCompleted(
                callID: "c1",
                parentCallID: "",
                path: "Custom.swift",
                operation: "edit",
                patch: "anything",
                added: 1,
                removed: 0,
                truncated: false
            )
        )

        let sessionHunks = store.sessionFiles.first?.hunks ?? []
        #expect(sessionHunks.count == 1)
        #expect(sessionHunks[0].id == "e0-0-custom-hunk")
        #expect(sessionHunks[0].lines.map(\.text) == ["parsed"])
        #expect(store.files(for: .singleEdit(callID: "c1")).first?.hunks == [sentinelHunk])
    }

    @Test @MainActor
    func defaultParserPopulatesIntralineSpans() async throws {
        let store = PCodeSessionChangesStore(sessionDirectory: "/tmp/proj")
        store.apply(
            .editCompleted(
                callID: "c1",
                parentCallID: "",
                path: "Sample.swift",
                operation: "edit",
                patch: """
                @@ -1,1 +1,1 @@
                -let greeting = "hello"
                +let greeting = "world"
                """,
                added: 1,
                removed: 1,
                truncated: false
            )
        )

        let sessionLines = store.sessionFiles.first?.hunks.first?.lines ?? []
        let deletedSession = try #require(sessionLines.first { $0.kind == .deleted })
        let addedSession = try #require(sessionLines.first { $0.kind == .added })
        #expect(!deletedSession.intralineSpans.isEmpty)
        #expect(!addedSession.intralineSpans.isEmpty)

        // Default PCodeGitDiffProvider also uses PCodeDiffParser and populates intraline spans
        let repoURL = try makeTempDirectory(prefix: "pcode-intraline")
        defer { try? FileManager.default.removeItem(at: repoURL) }
        try runGitSync(["init", "-b", "main"], in: repoURL)
        try runGitSync(["config", "user.email", "test@example.com"], in: repoURL)
        try runGitSync(["config", "user.name", "Test User"], in: repoURL)
        try "let count = 1\n".write(to: repoURL.appendingPathComponent("counter.swift"), atomically: true, encoding: .utf8)
        try runGitSync(["add", "counter.swift"], in: repoURL)
        try runGitSync(["commit", "-m", "initial"], in: repoURL)
        try "let count = 2\n".write(to: repoURL.appendingPathComponent("counter.swift"), atomically: true, encoding: .utf8)

        let provider = PCodeGitDiffProvider()
        let result = await provider.fetchWorkingTree(in: repoURL.path)
        let gitLines = result.files.first?.hunks.first?.lines ?? []
        let deletedGit = try #require(gitLines.first { $0.kind == .deleted })
        let addedGit = try #require(gitLines.first { $0.kind == .added })
        #expect(!deletedGit.intralineSpans.isEmpty)
        #expect(!addedGit.intralineSpans.isEmpty)
    }

    @Test @MainActor
    func emptyEditPreviewDoesNotCreateEmptyFileEntry() {
        let store = PCodeSessionChangesStore(sessionDirectory: "/tmp/proj")
        let t1 = Date(timeIntervalSince1970: 5_000)
        let t2 = Date(timeIntervalSince1970: 5_005)
        let t3 = Date(timeIntervalSince1970: 5_010)

        // Empty preview for a brand-new file should not create an empty streaming entry
        store.apply(
            .editPreview(callID: "edit-preview:0", path: "src/New.swift", text: "", kind: "edit"),
            now: t1
        )
        #expect(store.sessionFiles.isEmpty)
        #expect(store.singleEdit(for: "edit-preview:0") == nil)

        // Once non-empty preview text arrives, the streaming entry appears with hunks
        store.apply(
            .editPreview(callID: "edit-preview:0", path: "src/New.swift", text: "+let x = 1", kind: "edit"),
            now: t2
        )
        #expect(store.sessionFiles.count == 1)
        #expect(store.sessionFiles[0].isStreaming == true)
        #expect(!store.sessionFiles[0].hunks.isEmpty)

        // Empty preview for a file that already has a completed edit marks it streaming without losing hunks
        store.apply(
            .editCompleted(
                callID: "call-1",
                parentCallID: "",
                path: "src/Existing.swift",
                operation: "edit",
                patch: "@@ -1,1 +1,1 @@\n-old\n+new",
                added: 1,
                removed: 1,
                truncated: false
            ),
            now: t2
        )
        store.apply(
            .editPreview(callID: "edit-preview:1", path: "src/Existing.swift", text: "", kind: "edit"),
            now: t3
        )
        let existing = store.sessionFiles.first { $0.path == "src/Existing.swift" }
        #expect(existing?.isStreaming == true)
        #expect(existing?.hunks.isEmpty == false)
    }

    @Test @MainActor
    func setSessionDirectoryClearsStaleGitStateInSessionScope() async throws {
        let repoURL = try makeTempDirectory(prefix: "pcode-dirchange")
        defer { try? FileManager.default.removeItem(at: repoURL) }
        try runGitSync(["init", "-b", "main"], in: repoURL)
        try runGitSync(["config", "user.email", "test@example.com"], in: repoURL)
        try runGitSync(["config", "user.name", "Test User"], in: repoURL)
        try "hello\n".write(to: repoURL.appendingPathComponent("a.txt"), atomically: true, encoding: .utf8)
        try runGitSync(["add", "a.txt"], in: repoURL)
        try runGitSync(["commit", "-m", "initial"], in: repoURL)
        try runGitSync(["checkout", "-b", "feat/test"], in: repoURL)
        try "hello\nworld\n".write(to: repoURL.appendingPathComponent("a.txt"), atomically: true, encoding: .utf8)

        let store = PCodeSessionChangesStore(sessionDirectory: repoURL.path)
        await store.refreshGit(for: .workingTree)
        await store.refreshGit(for: .branch)
        #expect(store.hasGitRepo == true)
        #expect(!store.workingTreeFiles.isEmpty)
        #expect(!store.branchFiles.isEmpty)
        #expect(store.baseBranchName == "main")

        // Switch back to .session scope, then change directory: stale git state must be cleared
        store.setActiveScope(.session, triggerRefresh: false)
        store.setSessionDirectory("/tmp/another-workspace")
        #expect(store.workingTreeFiles.isEmpty)
        #expect(store.branchFiles.isEmpty)
        #expect(store.baseBranchName == nil)
        #expect(store.gitError == nil)
        #expect(store.hasGitRepo == false)
        #expect(store.isLoadingGit == false)
    }

    @Test @MainActor
    func cancellingRefreshGitClearsLoadingState() async throws {
        let tempDir = try makeTempDirectory(prefix: "pcode-cancelgit")
        defer { try? FileManager.default.removeItem(at: tempDir) }

        let slowGit = tempDir.appendingPathComponent("slow-git.sh")
        try "#!/bin/sh\nexec /bin/sleep 10\n".write(to: slowGit, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: slowGit.path)

        let provider = PCodeGitDiffProvider(gitExecutablePath: slowGit.path)
        let store = PCodeSessionChangesStore(sessionDirectory: tempDir.path, gitProvider: provider)

        let refreshTask = Task { @MainActor in
            await store.refreshGit(for: .workingTree)
        }

        // Wait briefly for refreshGit to enter loading state, then cancel the caller task
        try await Task.sleep(nanoseconds: 50_000_000)
        #expect(store.isLoadingGit == true)
        refreshTask.cancel()
        await refreshTask.value
        #expect(store.isLoadingGit == false)
    }

    // MARK: - PCodeGitDiffProvider

    @Test
    func gitDiffProviderNonGitDirectoryReturnsHasGitRepoFalse() async throws {
        let tempDir = try makeTempDirectory(prefix: "pcode-nongit")
        defer { try? FileManager.default.removeItem(at: tempDir) }

        let provider = PCodeGitDiffProvider()
        let workingTree = await provider.fetchWorkingTree(in: tempDir.path)
        #expect(workingTree.hasGitRepo == false)
        #expect(workingTree.files.isEmpty)
        #expect(workingTree.error == nil)

        let branch = await provider.fetchBranchDiff(in: tempDir.path)
        #expect(branch.hasGitRepo == false)
        #expect(branch.files.isEmpty)
        #expect(branch.baseBranchName == nil)
        #expect(branch.error == nil)
    }

    @Test @MainActor
    func gitDiffProviderWorkingTreeAndBranchDiffInTempRepo() async throws {
        let repoURL = try makeTempDirectory(prefix: "pcode-gitrepo")
        defer { try? FileManager.default.removeItem(at: repoURL) }

        try runGitSync(["init", "-b", "main"], in: repoURL)
        try runGitSync(["config", "user.email", "test@example.com"], in: repoURL)
        try runGitSync(["config", "user.name", "Test User"], in: repoURL)

        // 1. Unborn HEAD: staged file + untracked text file + untracked binary file
        try "hello\n".write(to: repoURL.appendingPathComponent("tracked.txt"), atomically: true, encoding: .utf8)
        try runGitSync(["add", "tracked.txt"], in: repoURL)
        try "untracked line 1\nuntracked line 2\n".write(
            to: repoURL.appendingPathComponent("notes.txt"),
            atomically: true,
            encoding: .utf8
        )
        try Data([0x00, 0xFF, 0x01, 0x02]).write(to: repoURL.appendingPathComponent("blob.bin"))

        let provider = PCodeGitDiffProvider()
        let unbornResult = await provider.fetchWorkingTree(in: repoURL.path)
        #expect(unbornResult.hasGitRepo == true)
        #expect(unbornResult.error == nil)
        let unbornPaths = unbornResult.files.map(\.path)
        #expect(unbornPaths.contains("tracked.txt"))
        #expect(unbornPaths.contains("notes.txt"))
        #expect(!unbornPaths.contains("blob.bin")) // Binary untracked file skipped

        let untrackedDiff = try #require(unbornResult.files.first { $0.path == "notes.txt" })
        #expect(untrackedDiff.kind == .added)
        #expect(untrackedDiff.added == 2)
        #expect(untrackedDiff.removed == 0)
        #expect(!untrackedDiff.hunks.isEmpty)

        // Remove scratch files and make initial commit on main
        try FileManager.default.removeItem(at: repoURL.appendingPathComponent("notes.txt"))
        try FileManager.default.removeItem(at: repoURL.appendingPathComponent("blob.bin"))
        try runGitSync(["commit", "-m", "initial"], in: repoURL)

        // 2. Create feature branch, add a committed change, then make working tree edits
        try runGitSync(["checkout", "-b", "feature/inspector"], in: repoURL)
        try "branch commit content\n".write(
            to: repoURL.appendingPathComponent("feature.swift"),
            atomically: true,
            encoding: .utf8
        )
        try runGitSync(["add", "feature.swift"], in: repoURL)
        try runGitSync(["commit", "-m", "add feature"], in: repoURL)

        // Modify tracked file and add a new untracked file in working tree
        try "hello\nworld\n".write(
            to: repoURL.appendingPathComponent("tracked.txt"),
            atomically: true,
            encoding: .utf8
        )
        try "draft\n".write(
            to: repoURL.appendingPathComponent("draft.txt"),
            atomically: true,
            encoding: .utf8
        )

        // Working tree sees modified tracked.txt and untracked draft.txt, but NOT already-committed feature.swift
        let wtResult = await provider.fetchWorkingTree(in: repoURL.path)
        #expect(wtResult.hasGitRepo == true)
        #expect(wtResult.error == nil)
        #expect(wtResult.files.map(\.path) == ["draft.txt", "tracked.txt"])
        let modifiedTracked = try #require(wtResult.files.first { $0.path == "tracked.txt" })
        #expect(modifiedTracked.kind == .modified)
        #expect(modifiedTracked.added == 1)
        #expect(modifiedTracked.removed == 0)

        // Branch diff detects `main` via merge-base and includes committed feature.swift + working tree + untracked
        let branchResult = await provider.fetchBranchDiff(in: repoURL.path)
        #expect(branchResult.hasGitRepo == true)
        #expect(branchResult.error == nil)
        #expect(branchResult.baseBranchName == "main")
        #expect(branchResult.files.map(\.path) == ["draft.txt", "feature.swift", "tracked.txt"])

        // Store integration updates published properties cleanly
        let store = PCodeSessionChangesStore(sessionDirectory: repoURL.path)
        await store.refreshGit(for: .workingTree)
        #expect(store.hasGitRepo == true)
        #expect(store.isLoadingGit == false)
        #expect(store.gitError == nil)
        #expect(store.files(for: .workingTree).map(\.path) == ["draft.txt", "tracked.txt"])

        await store.refreshGit(for: .branch)
        #expect(store.baseBranchName == "main")
        #expect(store.files(for: .branch).map(\.path) == ["draft.txt", "feature.swift", "tracked.txt"])
    }

    // MARK: - Test helpers

    private func makeTempDirectory(prefix: String) throws -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("\(prefix)-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    @discardableResult
    private func runGitSync(_ arguments: [String], in directory: URL) throws -> String {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/git")
        process.arguments = arguments
        process.currentDirectoryURL = directory
        let outPipe = Pipe()
        let errPipe = Pipe()
        process.standardOutput = outPipe
        process.standardError = errPipe
        try process.run()
        let outData = try outPipe.fileHandleForReading.readToEnd() ?? Data()
        let errData = try errPipe.fileHandleForReading.readToEnd() ?? Data()
        process.waitUntilExit()
        if process.terminationStatus != 0 {
            let stderr = String(decoding: errData, as: UTF8.self)
            throw NSError(
                domain: "PCodeSessionChangesStoreTests",
                code: Int(process.terminationStatus),
                userInfo: [NSLocalizedDescriptionKey: "git \(arguments.joined(separator: " ")) failed: \(stderr)"]
            )
        }
        return String(decoding: outData, as: UTF8.self)
    }

    @Test @MainActor
    func synthesizesSessionDiffsFromEditAndWriteToolCallsOnHistoryReplay() throws {
        let store = PCodeSessionChangesStore(sessionDirectory: "/repo")
        let writeArgs = #"{"Path":"/repo/src/NewFile.swift","Content":"let a = 1\nlet b = 2\n"}"#
        let editArgs = #"{"path":"/repo/src/App.swift","target":"let x = 1\nlet y = 2\nlet z = 3","replacement":"let x = 1\nlet y = 99\nlet z = 3"}"#
        let failedEditArgs = #"{"path":"/repo/src/Bad.swift","target":"a","replacement":"b"}"#
        let nativeEditArgs = #"{"path":"/repo/src/Native.swift","target":"old","replacement":"new"}"#

        store.apply(.history([
            .toolStarted(callID: "w1", parentCallID: "", name: "write_file", detail: "src/NewFile.swift", command: "", purpose: "", arguments: writeArgs),
            .toolSummary(callID: "w1", parentCallID: "", name: "write_file", detail: "src/NewFile.swift", command: "", failed: false, elapsedSeconds: 0.1, error: "", result: "Edited /repo/src/NewFile.swift", isBackgroundJob: false, exitCode: nil),

            .toolStarted(callID: "e1", parentCallID: "", name: "edit_file", detail: "src/App.swift", command: "", purpose: "", arguments: editArgs),
            .toolSummary(callID: "e1", parentCallID: "", name: "edit_file", detail: "src/App.swift", command: "", failed: false, elapsedSeconds: 0.2, error: "", result: "Made 1 replacement in /repo/src/App.swift starting at line 40 (+1/-1 lines).", isBackgroundJob: false, exitCode: nil),

            // Failed edit should not appear
            .toolStarted(callID: "f1", parentCallID: "", name: "edit_file", detail: "src/Bad.swift", command: "", purpose: "", arguments: failedEditArgs),
            .toolSummary(callID: "f1", parentCallID: "", name: "edit_file", detail: "src/Bad.swift", command: "", failed: true, elapsedSeconds: 0.1, error: "target not found", result: nil, isBackgroundJob: false, exitCode: nil),

            // Native edit with explicit EditCompleted should not be double-counted on ToolSummary
            .toolStarted(callID: "n1", parentCallID: "", name: "edit_file", detail: "src/Native.swift", command: "", purpose: "", arguments: nativeEditArgs),
            .editCompleted(callID: "n1", parentCallID: "", path: "src/Native.swift", operation: "edit", patch: "@@ -1,1 +1,1 @@\n-old\n+new", added: 1, removed: 1, truncated: false),
            .toolSummary(callID: "n1", parentCallID: "", name: "edit_file", detail: "src/Native.swift", command: "", failed: false, elapsedSeconds: 0.1, error: "", result: "ok", isBackgroundJob: false, exitCode: nil),
        ]))

        #expect(store.sessionFiles.count == 3)
        let paths = Set(store.sessionFiles.map(\.path))
        #expect(paths == Set(["src/NewFile.swift", "src/App.swift", "src/Native.swift"]))

        let appDiff = try #require(store.sessionFiles.first { $0.path == "src/App.swift" })
        #expect(appDiff.added == 1)
        #expect(appDiff.removed == 1)
        #expect(appDiff.hunks.count == 1)
        #expect(appDiff.hunks[0].oldStart == 40)
        #expect(appDiff.hunks[0].lines.contains { $0.kind == .added && !$0.intralineSpans.isEmpty })

        let newFileDiff = try #require(store.sessionFiles.first { $0.path == "src/NewFile.swift" })
        #expect(newFileDiff.kind == .added)
        #expect(newFileDiff.added == 2)
        #expect(newFileDiff.removed == 0)

        let nativeDiff = try #require(store.sessionFiles.first { $0.path == "src/Native.swift" })
        #expect(nativeDiff.editCount == 1)
    }

    @Test @MainActor
    func sessionStreamEditsAreAppendOnlyChronologicalWhileSidebarRemainsCombined() throws {
        let store = PCodeSessionChangesStore(sessionDirectory: "/repo")
        let t1 = Date(timeIntervalSince1970: 1_700_000_001)
        let t2 = Date(timeIntervalSince1970: 1_700_000_002)
        let t3 = Date(timeIntervalSince1970: 1_700_000_003)

        store.apply(
            .editCompleted(callID: "c1", parentCallID: "", path: "src/A.swift", operation: "edit", patch: "@@ -1,1 +1,1 @@\n-a1\n+a2", added: 1, removed: 1, truncated: false),
            now: t1
        )
        store.apply(
            .editCompleted(callID: "c2", parentCallID: "", path: "src/B.swift", operation: "edit", patch: "@@ -1,1 +1,1 @@\n-b1\n+b2", added: 1, removed: 1, truncated: false),
            now: t2
        )
        store.apply(
            .editCompleted(callID: "c3", parentCallID: "", path: "src/A.swift", operation: "edit", patch: "@@ -5,1 +5,1 @@\n-a3\n+a4", added: 1, removed: 1, truncated: false),
            now: t3
        )

        // Stream view: append-only chronological (oldest → newest), distinct IDs even for repeated file
        #expect(store.sessionStreamEdits.map(\.path) == ["src/A.swift", "src/B.swift", "src/A.swift"])
        #expect(Set(store.sessionStreamEdits.map(\.id)).count == 3)

        // Combined Files / sidebar view: deduplicated newest-first
        #expect(store.sessionFiles.map(\.path) == ["src/A.swift", "src/B.swift"])

        // Selecting a file from the left sidebar switches This Session to Files mode
        let state = PCodeInspectorState()
        state.sessionDiffViewMode = .stream
        state.openChanges(filePath: "src/A.swift", scope: .session)
        #expect(state.sessionDiffViewMode == .files)
        #expect(state.focusedFilePath == "src/A.swift")
    }
}
