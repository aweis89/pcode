import Foundation
import Testing
@testable import Ghostty

struct PCodeInspectorTabsTests {
    // MARK: - PCodeInspectorAgentsStore

    @Test @MainActor
    func delegateTaskLifecycleAndOrdering() {
        let store = PCodeInspectorAgentsStore()
        let t1 = Date(timeIntervalSince1970: 1_000)
        let t2 = Date(timeIntervalSince1970: 1_010)
        let t3 = Date(timeIntervalSince1970: 1_020)
        let t4 = Date(timeIntervalSince1970: 1_030)

        #expect(store.agents.isEmpty)
        #expect(store.activeCount == 0)

        // Start first sub-agent
        store.apply(
            .toolStarted(
                callID: "agent-1",
                parentCallID: "",
                name: "delegate_task",
                detail: "w1 · Audit auth flow",
                command: "",
                purpose: "Audit auth flow"
            ),
            now: t1
        )

        #expect(store.agents.count == 1)
        #expect(store.activeCount == 1)
        #expect(store.agents[0].id == "agent-1")
        #expect(store.agents[0].workerID == "w1")
        #expect(store.agents[0].role == "Audit auth flow")
        #expect(store.agents[0].status == .running)
        #expect(store.agents[0].startedAt == t1)

        // Start second sub-agent via invoke_subagent
        store.apply(
            .toolStarted(
                callID: "agent-2",
                parentCallID: "",
                name: "invoke_subagent",
                detail: "Inspect test flakes",
                command: "",
                purpose: ""
            ),
            now: t2
        )

        #expect(store.activeCount == 2)
        // Oldest → newest chronological order (latest active agent at bottom)
        #expect(store.agents.map(\.id) == ["agent-1", "agent-2"])

        // Finish agent-2 with a Markdown report; active agent-1 stays at the bottom after completed agent-2
        store.apply(
            .toolSummary(
                callID: "agent-2",
                parentCallID: "",
                name: "invoke_subagent",
                detail: "Inspect test flakes",
                command: "",
                failed: false,
                elapsedSeconds: 4.5,
                error: "",
                result: "## Findings\n- Fixed race in `AuthTests`",
                isBackgroundJob: false
            ),
            now: t3
        )

        #expect(store.activeCount == 1)
        #expect(store.agents.map(\.id) == ["agent-2", "agent-1"])
        let completed = store.agent(for: "agent-2")
        #expect(completed?.status == .completed)
        #expect(completed?.elapsedSeconds == 4.5)
        #expect(completed?.reportMarkdown == "## Findings\n- Fixed race in `AuthTests`")

        // Fail agent-1; both settled, most recently updated (agent-1 at t4) appears at bottom
        store.apply(
            .toolSummary(
                callID: "agent-1",
                parentCallID: "",
                name: "delegate_task",
                detail: "w1 · Audit auth flow",
                command: "",
                failed: true,
                elapsedSeconds: 12.0,
                error: "Timed out waiting for sub-agent",
                result: nil,
                isBackgroundJob: false
            ),
            now: t4
        )

        #expect(store.activeCount == 0)
        #expect(store.agents.map(\.id) == ["agent-2", "agent-1"])
        let failed = store.agent(for: "agent-1")
        #expect(failed?.status == .failed)
        #expect(failed?.error == "Timed out waiting for sub-agent")
    }

    @Test @MainActor
    func nestedChildToolAttachmentAndOrphanBuffering() {
        let store = PCodeInspectorAgentsStore()
        let t1 = Date(timeIntervalSince1970: 2_000)
        let t2 = Date(timeIntervalSince1970: 2_005)
        let t3 = Date(timeIntervalSince1970: 2_010)

        // Child tool arrives BEFORE parent agent starts (orphan buffering)
        store.apply(
            .toolStarted(
                callID: "agent-1:child-1",
                parentCallID: "agent-1",
                name: "read_file",
                detail: "Sources/Auth.swift",
                command: "",
                purpose: ""
            ),
            now: t1
        )
        #expect(store.agents.isEmpty)

        // Parent agent starts and flushes buffered orphan
        store.apply(
            .toolStarted(
                callID: "agent-1",
                parentCallID: "",
                name: "delegate_task",
                detail: "Review auth module",
                command: "",
                purpose: "Review auth module"
            ),
            now: t1
        )

        #expect(store.agents.count == 1)
        #expect(store.agents[0].nestedCalls.count == 1)
        #expect(store.agents[0].nestedCalls[0].name == "read_file")
        #expect(store.agents[0].nestedCalls[0].isRunning == true)

        // Child settles via toolSummary
        store.apply(
            .toolSummary(
                callID: "agent-1:child-1",
                parentCallID: "agent-1",
                name: "read_file",
                detail: "Sources/Auth.swift",
                command: "",
                failed: false,
                elapsedSeconds: 0.2,
                error: "",
                result: "file contents",
                isBackgroundJob: false
            ),
            now: t2
        )

        // Second child starts, streams output, and settles with non-zero exit code
        store.apply(
            .toolStarted(
                callID: "agent-1:child-2",
                parentCallID: "agent-1",
                name: "shell",
                detail: "rg token src/",
                command: "rg token src/",
                purpose: ""
            ),
            now: t2
        )
        store.apply(
            .commandOutput(callID: "agent-1:child-2", command: "rg token src/", output: "scanning…"),
            now: t2
        )
        #expect(store.agents[0].nestedCalls.count == 2)
        #expect(store.agents[0].nestedCalls[1].output == "scanning…")

        store.apply(
            .toolSummary(
                callID: "agent-1:child-2",
                parentCallID: "agent-1",
                name: "shell",
                detail: "rg token src/",
                command: "rg token src/",
                failed: false,
                elapsedSeconds: 0.4,
                error: "",
                result: "",
                isBackgroundJob: false,
                exitCode: 1
            ),
            now: t3
        )

        let nested = store.agents[0].nestedCalls
        #expect(nested.count == 2)
        #expect(nested[0].isRunning == false)
        #expect(nested[0].detail == "Sources/Auth.swift")
        #expect(nested[1].isRunning == false)
        #expect(nested[1].exitedNonZero == true)
        #expect(nested[1].exitCode == 1)
    }

    @Test @MainActor
    func agentsHistoryReplayAndReset() {
        let store = PCodeInspectorAgentsStore()
        let t1 = Date(timeIntervalSince1970: 3_000)

        // Seed stale live state that history should replace
        store.apply(
            .toolStarted(
                callID: "stale-agent",
                parentCallID: "",
                name: "delegate_task",
                detail: "Old task",
                command: "",
                purpose: "Old task"
            ),
            now: t1
        )
        #expect(store.agents.count == 1)

        // History replay: child ToolSummary arrives before parent delegate_task ToolSummary,
        // plus an unclosed ToolStarted that history replay should settle cleanly.
        let historyEvents: [PCodeHostEvent] = [
            .toolSummary(
                callID: "hist-agent:c1",
                parentCallID: "hist-agent",
                name: "search_files",
                detail: "AuthManager",
                command: "",
                failed: false,
                elapsedSeconds: 0.3,
                error: "",
                result: "3 matches",
                isBackgroundJob: false
            ),
            .toolSummary(
                callID: "hist-agent",
                parentCallID: "",
                name: "delegate_task",
                detail: "Locate AuthManager callers",
                command: "",
                failed: false,
                elapsedSeconds: 3.2,
                error: "",
                result: "Found 3 callers.",
                isBackgroundJob: false
            ),
            .toolStarted(
                callID: "unclosed-agent",
                parentCallID: "",
                name: "delegate_task",
                detail: "Interrupted worker",
                command: "",
                purpose: "Interrupted worker"
            ),
        ]

        store.apply(.history(historyEvents), now: t1)

        #expect(store.agent(for: "stale-agent") == nil)
        #expect(store.agents.count == 2)
        #expect(store.activeCount == 0)

        let replayed = store.agent(for: "hist-agent")
        #expect(replayed?.status == .completed)
        #expect(replayed?.nestedCalls.count == 1)
        #expect(replayed?.nestedCalls[0].name == "search_files")
        #expect(replayed?.reportMarkdown == "Found 3 callers.")

        let unclosed = store.agent(for: "unclosed-agent")
        #expect(unclosed?.status == .completed)

        // Reset clears all state
        store.apply(.conversationReset, now: t1)
        #expect(store.agents.isEmpty)
        #expect(store.activeCount == 0)
    }

    @Test @MainActor
    func agentActionPromptsUseComposerInsertion() {
        let withWorkerID = PCodeInspectorAgentItem(
            id: "call-1",
            workerID: "w3",
            role: "Code Reviewer",
            status: .running
        )
        #expect(
            PCodeInspectorAgentsView.stopActionPrompt(for: withWorkerID)
                == PCodeComposerInsertion.workerStopPrompt(id: "w3", role: "Code Reviewer")
        )
        #expect(PCodeInspectorAgentsView.stopActionPrompt(for: withWorkerID) == "Stop worker w3 (Code Reviewer).")
        #expect(
            PCodeInspectorAgentsView.messageActionPrompt(for: withWorkerID)
                == "Message worker w3 (Code Reviewer): "
        )
        #expect(
            PCodeInspectorAgentsView.messageActionPrompt(for: withWorkerID, body: "check edge cases")
                == PCodeComposerInsertion.workerMessagePrompt(
                    id: "w3",
                    role: "Code Reviewer",
                    message: "check edge cases"
                )
        )
        #expect(
            PCodeInspectorAgentsView.answerActionPrompt(for: withWorkerID, body: "use SQLite")
                == PCodeComposerInsertion.workerAnswerPrompt(
                    id: "w3",
                    role: "Code Reviewer",
                    answer: "use SQLite"
                )
        )

        let roleOnly = PCodeInspectorAgentItem(
            id: "call-2",
            workerID: nil,
            role: "Database Debugger",
            status: .waiting,
            pendingQuestion: "Proceed with migration?"
        )
        #expect(
            PCodeInspectorAgentsView.stopActionPrompt(for: roleOnly)
                == "Stop worker (Database Debugger)."
        )
        #expect(
            PCodeInspectorAgentsView.answerActionPrompt(for: roleOnly)
                == "Answer worker (Database Debugger): "
        )

        let idOnly = PCodeInspectorAgentItem(
            id: "call-3",
            workerID: "w4",
            role: "",
            status: .running
        )
        #expect(PCodeInspectorAgentsView.stopActionPrompt(for: idOnly) == "Stop worker w4.")
        #expect(PCodeInspectorAgentsView.messageActionPrompt(for: idOnly, body: "status?") == "Message worker w4: status?")

        let unnamed = PCodeInspectorAgentItem(
            id: "call-4",
            workerID: nil,
            role: "",
            status: .running
        )
        #expect(PCodeInspectorAgentsView.stopActionPrompt(for: unnamed) == "Stop the worker.")
    }

    @Test @MainActor
    func workerStatusEventUpdatesWorkerIDAndPendingQuestion() {
        let store = PCodeInspectorAgentsStore()
        let t0 = Date(timeIntervalSince1970: 1_700_000_000)

        store.apply(
            .toolStarted(
                callID: "call-w",
                parentCallID: "",
                name: "delegate_task",
                detail: "Code Reviewer",
                command: "",
                purpose: "Reviewing diff"
            ),
            now: t0
        )

        store.apply(
            .workerStatus(
                callID: "call-w",
                workerID: "w7",
                question: "Keep backwards compatibility?",
                answered: nil,
                status: "waiting"
            ),
            now: t0.addingTimeInterval(2)
        )

        var agent = store.agent(for: "call-w")
        #expect(agent?.workerID == "w7")
        #expect(agent?.status == .waiting)
        #expect(agent?.pendingQuestion == "Keep backwards compatibility?")

        store.apply(
            .workerStatus(
                callID: "call-w",
                workerID: "w7",
                question: nil,
                answered: true,
                status: "running"
            ),
            now: t0.addingTimeInterval(4)
        )

        agent = store.agent(for: "call-w")
        #expect(agent?.status == .running)
        #expect(agent?.pendingQuestion == nil)

        // Timed-out / cancelled question (`answered: false`) also clears `.waiting`
        store.apply(
            .workerStatus(
                callID: "call-w",
                workerID: "w7",
                question: "Timed out question?",
                answered: nil,
                status: "waiting"
            ),
            now: t0.addingTimeInterval(5)
        )
        #expect(store.agent(for: "call-w")?.status == .waiting)

        store.apply(
            .workerStatus(
                callID: "call-w",
                workerID: "w7",
                question: nil,
                answered: false,
                status: nil
            ),
            now: t0.addingTimeInterval(6)
        )
        agent = store.agent(for: "call-w")
        #expect(agent?.status == .running)
        #expect(agent?.pendingQuestion == nil)
    }

    @Test
    func reportResolverStripsHeadersLoadsSpilledFileAndSlicesLines() throws {
        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("pcode-spill-test-\(UUID().uuidString)", isDirectory: true)
        let runDir = tempRoot.appendingPathComponent("run-123", isDirectory: true)
        try FileManager.default.createDirectory(at: runDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tempRoot) }

        let fullLines = (1...80).map { "Line \($0)" }.joined(separator: "\n")
        let spillFile = runDir.appendingPathComponent("call_abc.0", isDirectory: false)
        try ("[worker w9]\n" + fullLines).write(to: spillFile, atomically: true, encoding: .utf8)

        let rawSpilled = """
        [Tool output too large (17,007 chars); stored to handle 'run-123/call_abc.0'. Read it with read_tool_result(handle='run-123/call_abc.0', offset=0, limit=200, from_end=False, pattern=None).]
        [worker w9]
        Preview only
        """

        let parsed = try #require(PCodeAgentReportResolver.parseSpill(rawSpilled))
        #expect(parsed.handle == "run-123/call_abc.0")
        #expect(parsed.previewBody == "Preview only")

        let loaded = try #require(
            PCodeAgentReportResolver.loadSpilledReport(handle: parsed.handle, baseDirectory: tempRoot)
        )
        #expect(loaded == fullLines)
        #expect(!loaded.contains("[worker w9]"))

        // Reject path traversal and symlinks
        #expect(PCodeAgentReportResolver.loadSpilledReport(handle: "../etc/passwd", baseDirectory: tempRoot) == nil)
        let symlinkPath = runDir.appendingPathComponent("link.0", isDirectory: false)
        try FileManager.default.createSymbolicLink(at: symlinkPath, withDestinationURL: spillFile)
        #expect(PCodeAgentReportResolver.loadSpilledReport(handle: "run-123/link.0", baseDirectory: tempRoot) == nil)

        // 3-level nested worker header stripping
        #expect(PCodeAgentReportResolver.stripWorkerHeader("[worker w1.2.3]\nHello") == "Hello")

        // Progressive line slicing
        let firstSlice = PCodeAgentReportResolver.sliceLines(loaded, lineLimit: 40)
        #expect(firstSlice.isTruncated)
        #expect(firstSlice.shownLines == 40)
        #expect(firstSlice.totalLines == 80)
        #expect(firstSlice.remainingLines == 40)

        let fullSlice = PCodeAgentReportResolver.sliceLines(loaded, lineLimit: 0)
        #expect(!fullSlice.isTruncated)
        #expect(fullSlice.shownLines == 80)
    }

    @Test @MainActor
    func nestedSubAgentsFormHierarchyWithoutDuplicatingParentToolCalls() {
        let store = PCodeInspectorAgentsStore()
        let t0 = Date(timeIntervalSince1970: 2_000)

        store.apply(
            .toolStarted(
                callID: "root-1",
                parentCallID: "",
                name: "delegate_task",
                detail: "w1 · Coordinator",
                command: "",
                purpose: "Coordinate refactor"
            ),
            now: t0
        )
        store.apply(
            .toolStarted(
                callID: "child-1",
                parentCallID: "root-1",
                name: "delegate_task",
                detail: "w1.1 · Leaf worker",
                command: "",
                purpose: "Run leaf check"
            ),
            now: t0.addingTimeInterval(1)
        )
        store.apply(
            .toolStarted(
                callID: "leaf-tool",
                parentCallID: "child-1",
                name: "read_file",
                detail: "README.md",
                command: "",
                purpose: ""
            ),
            now: t0.addingTimeInterval(2)
        )

        #expect(store.rootAgents.map(\.id) == ["root-1"])
        #expect(store.childAgents(of: "root-1").map(\.id) == ["child-1"])
        #expect(store.ancestorIDs(of: "child-1") == ["root-1"])
        // Nested sub-agent is rendered as a child agent card, not duplicated as a plain tool row.
        #expect(store.agent(for: "root-1")?.nestedCalls.isEmpty == true)
        #expect(store.agent(for: "child-1")?.nestedCalls.count == 1)
    }

    // MARK: - PCodeInspectorCommandsStore

    @Test @MainActor
    func shellLifecycleStreamingOutputAndOrdering() {
        let store = PCodeInspectorCommandsStore()
        let t1 = Date(timeIntervalSince1970: 4_000)
        let t2 = Date(timeIntervalSince1970: 4_005)
        let t3 = Date(timeIntervalSince1970: 4_010)
        let t4 = Date(timeIntervalSince1970: 4_015)

        // Non-command tools (e.g. read_file, delegate_task) are ignored
        store.apply(
            .toolStarted(
                callID: "read-1",
                parentCallID: "",
                name: "read_file",
                detail: "README.md",
                command: "",
                purpose: ""
            ),
            now: t1
        )
        #expect(store.commands.isEmpty)

        // Start shell command
        store.apply(
            .toolStarted(
                callID: "cmd-1",
                parentCallID: "",
                name: "shell",
                detail: "swift test",
                command: "swift test",
                purpose: ""
            ),
            now: t1
        )
        #expect(store.commands.count == 1)
        #expect(store.runningCount == 1)
        #expect(store.commands[0].status == .running)
        #expect(store.commands[0].command == "swift test")

        // Live streaming commandOutput updates output in place
        store.apply(
            .commandOutput(callID: "cmd-1", command: "swift test", output: "Compiling...\nTest Case 1 passed"),
            now: t2
        )
        #expect(store.commands[0].output == "Compiling...\nTest Case 1 passed")
        #expect(store.commands[0].isRunning == true)

        // Start a second command; both running, newest first
        store.apply(
            .toolStarted(
                callID: "cmd-2",
                parentCallID: "",
                name: "run_command",
                detail: "git status",
                command: "git status",
                purpose: ""
            ),
            now: t3
        )
        #expect(store.runningCount == 2)
        #expect(store.commands.map(\.id) == ["cmd-2", "cmd-1"])

        // Settle cmd-2; running cmd-1 moves back ahead of settled cmd-2
        store.apply(
            .toolSummary(
                callID: "cmd-2",
                parentCallID: "",
                name: "run_command",
                detail: "git status",
                command: "git status",
                failed: false,
                elapsedSeconds: 0.15,
                error: "",
                result: "On branch master",
                isBackgroundJob: false,
                exitCode: 0
            ),
            now: t4
        )
        #expect(store.runningCount == 1)
        #expect(store.commands.map(\.id) == ["cmd-1", "cmd-2"])
        #expect(store.command(for: "cmd-2")?.status == .succeeded)
        #expect(store.command(for: "cmd-2")?.output == "On branch master")
    }

    @Test @MainActor
    func commandExitCodeNonZeroVsFailedAndBackgroundJobs() {
        let store = PCodeInspectorCommandsStore()
        let t1 = Date(timeIntervalSince1970: 5_000)
        let t2 = Date(timeIntervalSince1970: 5_010)
        let t3 = Date(timeIntervalSince1970: 5_020)

        // Exited non-zero (failed == false, exitCode == 1) -> .exitedNonZero
        store.apply(
            .toolSummary(
                callID: "grep-1",
                parentCallID: "",
                name: "shell",
                detail: "rg nonexistent src/",
                command: "rg nonexistent src/",
                failed: false,
                elapsedSeconds: 0.1,
                error: "",
                result: "",
                isBackgroundJob: false,
                exitCode: 1
            ),
            now: t1
        )
        #expect(store.command(for: "grep-1")?.status == .exitedNonZero)
        #expect(store.command(for: "grep-1")?.exitCode == 1)

        // Failed tool (failed == true, exitCode == 137) -> .failed
        store.apply(
            .toolSummary(
                callID: "kill-1",
                parentCallID: "",
                name: "shell",
                detail: "sleep 999",
                command: "sleep 999",
                failed: true,
                elapsedSeconds: 2.0,
                error: "Killed by signal 9",
                result: nil,
                isBackgroundJob: false,
                exitCode: 137
            ),
            now: t2
        )
        #expect(store.command(for: "kill-1")?.status == .failed)
        #expect(store.command(for: "kill-1")?.exitCode == 137)
        #expect(store.command(for: "kill-1")?.output == "Killed by signal 9")

        // Background job started via start_command stays running across turnEnded until summary arrives
        store.apply(
            .toolStarted(
                callID: "job_42",
                parentCallID: "",
                name: "start_command",
                detail: "npm run dev",
                command: "npm run dev",
                purpose: ""
            ),
            now: t2
        )
        #expect(store.command(for: "job_42")?.isBackgroundJob == true)
        #expect(store.command(for: "job_42")?.isRunning == true)
        #expect(store.runningCount == 1)

        store.apply(.turnEnded(status: .complete), now: t2)
        #expect(store.command(for: "job_42")?.isRunning == true)

        // Background job completes (JobFinished -> toolSummary with isBackgroundJob: true)
        store.apply(
            .toolSummary(
                callID: "job_42",
                parentCallID: "",
                name: "start_command",
                detail: "npm run dev",
                command: "npm run dev",
                failed: false,
                elapsedSeconds: 18.0,
                error: "",
                result: "Server exited cleanly",
                isBackgroundJob: true,
                exitCode: 0
            ),
            now: t3
        )
        #expect(store.runningCount == 0)
        #expect(store.command(for: "job_42")?.status == .succeeded)
        #expect(store.command(for: "job_42")?.isBackgroundJob == true)
        #expect(store.command(for: "job_42")?.output == "Server exited cleanly")
    }

    @Test @MainActor
    func watchedBackgroundJobProtocolDropOutputAndEmptyCallIDMatching() {
        // Host WATCHED_PREFIX ("job:") recognition and registryJobID stripping
        #expect(PCodeInspectorCommandsStore.looksLikeBackgroundJob(name: "", callID: "job:j3") == true)
        #expect(PCodeInspectorCommandsStore.looksLikeBackgroundJob(name: "shell", callID: "call-1") == false)
        #expect(PCodeInspectorCommandsStore.registryJobID(for: "job:j3") == "j3")
        #expect(PCodeInspectorCommandsStore.registryJobID(for: "j3") == "j3")
        #expect(PCodeInspectorCommandsStore.registryJobID(for: "job_42") == "job_42")

        let store = PCodeInspectorCommandsStore()
        let t1 = Date(timeIntervalSince1970: 5_500)
        let t2 = Date(timeIntervalSince1970: 5_510)
        let t3 = Date(timeIntervalSince1970: 5_520)

        // Watched background job streams CommandOutput with callID "job:j3"
        store.apply(
            .commandOutput(callID: "job:j3", command: "npm run dev", output: "Listening on :3000"),
            now: t1
        )
        #expect(store.commands.count == 1)
        #expect(store.runningCount == 1)
        #expect(store.command(for: "job:j3")?.isBackgroundJob == true)
        #expect(store.command(for: "job:j3")?.status == .running)

        // Unwatching emits dropOutput("job:j3") while the job is still running — must NOT settle it
        store.apply(.dropOutput(callID: "job:j3"), now: t1)
        #expect(store.runningCount == 1)
        #expect(store.command(for: "job:j3")?.status == .running)

        // A start_command background job also ignores dropOutput while running
        store.apply(
            .toolStarted(
                callID: "call-bg-1",
                parentCallID: "",
                name: "start_command",
                detail: "pytest -w",
                command: "pytest -w",
                purpose: ""
            ),
            now: t1
        )
        store.apply(.dropOutput(callID: "call-bg-1"), now: t1)
        #expect(store.command(for: "call-bg-1")?.status == .running)
        #expect(store.runningCount == 2)

        // JobFinished arrives as toolSummary with empty callID and isBackgroundJob == true:
        // must match the existing running "job:j3" row instead of creating a synthetic cmd-N row.
        store.apply(
            .toolSummary(
                callID: "",
                parentCallID: "",
                name: "shell",
                detail: "npm run dev → j3 · exited 0",
                command: "npm run dev",
                failed: false,
                elapsedSeconds: 12.5,
                error: "",
                result: "Server exited cleanly\n[j3 · exited 0 · 12.5s]",
                isBackgroundJob: true,
                exitCode: 0
            ),
            now: t2
        )
        #expect(store.commands.count == 2)
        #expect(store.runningCount == 1)
        #expect(store.command(for: "job:j3")?.status == .succeeded)
        #expect(store.command(for: "job:j3")?.isBackgroundJob == true)
        #expect(store.command(for: "job:j3")?.output == "Server exited cleanly\n[j3 · exited 0 · 12.5s]")

        // Two concurrent watched jobs with the same command disambiguate by bare job ID in detail
        store.apply(
            .commandOutput(callID: "job:j1", command: "make watch", output: "watching 1"),
            now: t2
        )
        store.apply(
            .commandOutput(callID: "job:j10", command: "make watch", output: "watching 10"),
            now: t2
        )
        store.apply(
            .toolSummary(
                callID: "",
                parentCallID: "",
                name: "shell",
                detail: "make watch → j10 · exited 0",
                command: "make watch",
                failed: false,
                elapsedSeconds: 4.0,
                error: "",
                result: "done 10",
                isBackgroundJob: true,
                exitCode: 0
            ),
            now: t3
        )
        #expect(store.command(for: "job:j10")?.status == .succeeded)
        #expect(store.command(for: "job:j1")?.status == .running)

        // Empty-callID JobFinished with no matching running job falls back to a synthetic cmd-N row
        store.apply(
            .toolSummary(
                callID: "",
                parentCallID: "",
                name: "shell",
                detail: "sleep 1 → j99 · exited 0",
                command: "sleep 1",
                failed: false,
                elapsedSeconds: 1.0,
                error: "",
                result: "[j99 · exited 0 · 1.0s]",
                isBackgroundJob: true,
                exitCode: 0
            ),
            now: t3
        )
        #expect(store.command(for: "cmd-1")?.status == .succeeded)
        #expect(store.command(for: "cmd-1")?.isBackgroundJob == true)
    }

    @Test @MainActor
    func shellResultDropOutputHistoryAndReset() {
        let store = PCodeInspectorCommandsStore()
        let t1 = Date(timeIntervalSince1970: 6_000)
        let t2 = Date(timeIntervalSince1970: 6_005)

        // `!command` shell mode streams commandOutput, emits dropOutput, then shellResult
        store.apply(
            .commandOutput(callID: "shell_mode_7", command: "ls -la", output: "total 8\nREADME.md"),
            now: t1
        )
        #expect(store.runningCount == 1)
        #expect(store.command(for: "shell_mode_7")?.isRunning == true)

        store.apply(.dropOutput(callID: "shell_mode_7"), now: t1)
        #expect(store.runningCount == 0)
        #expect(store.command(for: "shell_mode_7")?.status == .succeeded)

        store.apply(
            .shellResult(command: "ls -la", output: "total 8\nREADME.md\nPackage.swift", failed: true, elapsedSeconds: 0.4),
            now: t2
        )
        #expect(store.commands.count == 1)
        #expect(store.command(for: "shell_mode_7")?.status == .failed)
        #expect(store.command(for: "shell_mode_7")?.output == "total 8\nREADME.md\nPackage.swift")

        // Standalone shellResult without preceding commandOutput creates a new record
        store.apply(
            .shellResult(command: "pwd", output: "/workspace", failed: false, elapsedSeconds: 0.05),
            now: t2
        )
        #expect(store.commands.count == 2)

        // History replaces existing state and settles any unclosed commands
        store.apply(
            .history([
                .toolSummary(
                    callID: "hist-cmd",
                    parentCallID: "",
                    name: "shell",
                    detail: "pytest -q",
                    command: "pytest -q",
                    failed: false,
                    elapsedSeconds: 2.3,
                    error: "",
                    result: "12 passed",
                    isBackgroundJob: false,
                    exitCode: 0
                ),
                .toolStarted(
                    callID: "hist-open",
                    parentCallID: "",
                    name: "shell",
                    detail: "make lint",
                    command: "make lint",
                    purpose: ""
                ),
            ]),
            now: t2
        )
        #expect(store.commands.count == 2)
        #expect(store.runningCount == 0)
        #expect(store.command(for: "hist-cmd")?.output == "12 passed")
        #expect(store.command(for: "hist-open")?.status == .succeeded)

        // Reset clears all commands
        store.apply(.conversationReset, now: t2)
        #expect(store.commands.isEmpty)
        #expect(store.runningCount == 0)
    }

    @Test
    func commandPromptReferenceFormattingAndTruncation() {
        let item = PCodeInspectorCommandItem(
            id: "c1",
            command: "pytest -q tests/test_auth.py",
            output: "line1\nline2\nline3\nline4\nline5",
            status: .exitedNonZero,
            exitCode: 1
        )
        let full = PCodeInspectorCommandsView.promptReference(for: item, maxLines: 10)
        #expect(full.contains("Command `pytest -q tests/test_auth.py` (exit 1):"))
        #expect(full.contains("line1\nline2\nline3\nline4\nline5"))
        #expect(!full.contains("truncated"))

        let truncated = PCodeInspectorCommandsView.promptReference(for: item, maxLines: 2)
        #expect(truncated.contains("… (truncated)"))
        #expect(truncated.contains("line4\nline5"))
        #expect(!truncated.contains("line1"))
    }
}
