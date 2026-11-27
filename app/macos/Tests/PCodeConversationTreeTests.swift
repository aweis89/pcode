import AppKit
import Foundation
import Testing
@testable import Ghostty

struct PCodeConversationTreeTests {
    private static func jsonl(_ records: [[String: Any]]) -> Data {
        var out = Data()
        for record in records {
            if let data = try? JSONSerialization.data(withJSONObject: record) {
                out.append(data)
                out.append(0x0A)
            }
        }
        return out
    }

    @Test func parsesLinearJournalWithToolsAndEdits() {
        let data = Self.jsonl([
            ["kind": "turn_started", "run_id": "t1", "parent_id": NSNull(), "prompt": "Hello world"],
            ["kind": "ToolStarted", "run_id": "t1", "call_id": "c1", "name": "read_file"],
            ["kind": "ToolStarted", "run_id": "t1", "call_id": "c2", "parent_call_id": "c1", "name": "nested"],
            ["kind": "EditCompleted", "run_id": "t1", "call_id": "e1", "path": "a.swift"],
            ["kind": "Message", "run_id": "t1", "markdown": "Done editing."],
            ["kind": "turn_completed", "run_id": "t1"],
            ["kind": "turn_started", "run_id": "t2", "parent_id": "t1", "prompt": "Second question"],
            ["kind": "Message", "run_id": "t2", "markdown": "Second answer."],
            ["kind": "turn_completed", "run_id": "t2"],
        ])

        let tree = PCodeConversationTree.parse(journalData: data)
        #expect(tree.turnCount == 2)
        #expect(tree.activeID == "t2")
        #expect(tree.activePath() == ["t1", "t2"])
        #expect(!tree.hasBranches)
        #expect(tree.branchTipCount == 1)

        let t1 = tree.nodesByID["t1"]
        #expect(t1?.toolCount == 1)
        #expect(t1?.editCount == 1)
        #expect(t1?.status == .completed)
        #expect(t1?.response == "Done editing.")

        let rows = tree.flattenedRows()
        #expect(rows.count == 2)
        #expect(rows[0].turnNumber == 1)
        #expect(rows[0].indentLevel == 0)
        #expect(rows[0].isOnActivePath)
        #expect(!rows[0].isActiveHead)
        #expect(rows[1].turnNumber == 2)
        #expect(rows[1].isActiveHead)
    }

    @Test func parsesBranchesTreeSelectedAsidesAndCompaction() {
        let data = Self.jsonl([
            ["kind": "turn_started", "run_id": "t1", "parent_id": NSNull(), "prompt": "Root prompt"],
            ["kind": "Message", "run_id": "t1", "markdown": "Root reply"],
            ["kind": "turn_completed", "run_id": "t1"],
            // Branch A
            ["kind": "turn_started", "run_id": "t2a", "parent_id": "t1", "prompt": "Branch A step 1"],
            ["kind": "turn_completed", "run_id": "t2a"],
            ["kind": "turn_started", "run_id": "t3a", "parent_id": "t2a", "prompt": "Branch A step 2"],
            ["kind": "turn_completed", "run_id": "t3a"],
            // /btw aside off t1 (must not advance activeID)
            ["kind": "turn_started", "run_id": "s1", "parent_id": "t1", "prompt": "Quick side?", "side": ["thread": "x"]],
            ["kind": "Message", "run_id": "s1", "markdown": "Side answer"],
            ["kind": "turn_completed", "run_id": "s1"],
            // Fork Branch B from t1
            ["kind": "tree_selected", "node_id": "t1"],
            ["kind": "turn_started", "run_id": "t2b", "parent_id": "t1", "prompt": "Branch B step 1"],
            ["kind": "turn_completed", "run_id": "t2b"],
            // Compaction on Branch B
            ["kind": "compaction_checkpoint", "node_id": "cmp1", "parent_id": "t2b", "focus": "keep auth", "before": 50000, "after": 8000],
        ])

        let tree = PCodeConversationTree.parse(journalData: data)
        #expect(tree.activeID == "cmp1")
        #expect(tree.activePath() == ["t1", "t2b", "cmp1"])
        #expect(tree.hasBranches)
        #expect(tree.branchTipCount == 3)
        #expect(tree.nodesByID["s1"]?.kind == .aside)
        #expect(tree.nodesByID["cmp1"]?.kind == .compaction)
        #expect(tree.nodesByID["cmp1"]?.response.contains("50000") == true)

        // Full rows include all 6 nodes, with inactive branch root collapsible.
        let allRows = tree.flattenedRows()
        #expect(allRows.count == 6)
        let row2a = allRows.first { $0.id == "t2a" }
        #expect(row2a?.isOnActivePath == false)
        #expect(row2a?.isCollapsibleBranchRoot == true)
        #expect(row2a?.isForkChild == true)

        // Collapsing t2a hides t3a.
        let collapsedRows = tree.flattenedRows(collapsedBranchRoots: ["t2a"])
        #expect(collapsedRows.count == 5)
        #expect(collapsedRows.first { $0.id == "t2a" }?.collapsedDescendantCount == 1)

        // Active-only filter leaves only the active path.
        let activeOnly = tree.flattenedRows(activeBranchOnly: true)
        #expect(activeOnly.map(\.id) == ["t1", "t2b", "cmp1"])

        // Hiding asides & compactions filters them out.
        let filtered = tree.flattenedRows(includeAsides: false, includeCompactions: false)
        #expect(!filtered.contains { $0.id == "s1" || $0.id == "cmp1" })

        // Search query matches prompt or response text.
        let searched = tree.flattenedRows(searchQuery: "side answer")
        #expect(searched.map(\.id) == ["s1"])
    }

    @Test func treeSelectedNullSelectsConversationStart() {
        let data = Self.jsonl([
            ["kind": "turn_started", "run_id": "t1", "parent_id": NSNull(), "prompt": "First"],
            ["kind": "turn_completed", "run_id": "t1"],
            ["kind": "tree_selected", "node_id": NSNull()],
        ])
        let tree = PCodeConversationTree.parse(journalData: data)
        #expect(tree.activeID == nil)
        #expect(tree.activePath().isEmpty)
    }

    @Test @MainActor func liveTreeStoreTracksStreamingTurnAndBusyState() {
        let store = PCodeConversationTreeStore()
        store.apply(.sessionState(PCodeHostSessionDelta(busy: true)))
        #expect(store.isBusy)

        store.apply(.turnStarted(prompt: "Live prompt", isSide: false, runID: "live-1"))
        #expect(store.tree.turnCount == 1)
        #expect(store.tree.activeID == "live-1")
        #expect(store.tree.nodesByID["live-1"]?.status == .running)

        store.apply(.toolStarted(callID: "c1", parentCallID: "", name: "shell", detail: "", command: "ls", purpose: ""))
        store.apply(.message(markdown: "All done"))
        store.apply(.turnEnded(status: .complete))
        store.apply(.sessionState(PCodeHostSessionDelta(busy: false)))

        #expect(!store.isBusy)
        #expect(store.tree.nodesByID["live-1"]?.status == .completed)
        #expect(store.tree.nodesByID["live-1"]?.toolCount == 1)
        #expect(store.tree.nodesByID["live-1"]?.response == "All done")
    }

    @Test @MainActor func transcriptScrollToTurnAndVisibleTurnSpy() {
        let typography = PCodeTypography.shared
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let scrollView = PCodeTranscriptScrollView(typography: typography, theme: theme)
        scrollView.frame = NSRect(x: 0, y: 0, width: 640, height: 320)
        scrollView.layoutSubtreeIfNeeded()

        let doc = scrollView.transcriptDocumentView
        var observed: [String?] = []
        doc.onVisibleTurnChanged = { observed.append($0) }

        doc.reloadHistory {
            for i in 1...6 {
                doc.beginStreamingTurn(prompt: "Prompt \(i)", isSide: false, turnID: "turn-\(i)", forcePin: false, reveal: false)
                doc.beginStreamingMessage(id: "m-\(i)")
                doc.appendStreamingDelta(String(repeating: "Line of response text for turn \(i).\n\n", count: 8), reveal: false)
                doc.finishStreamingMessage(reveal: false)
            }
        }

        #expect(doc.scrollToTurn("turn-2"))
        #expect(doc.currentVisibleTurnID() == "turn-2")
        #expect(observed.last == "turn-2")

        #expect(doc.scrollToTurn("turn-5"))
        #expect(doc.currentVisibleTurnID() == "turn-5")
        #expect(!doc.scrollToTurn("nonexistent"))
    }

    @Test func hidingCompactionPreservesSubsequentTurns() {
        let data = Self.jsonl([
            ["kind": "turn_started", "run_id": "t1", "parent_id": NSNull(), "prompt": "Before compaction"],
            ["kind": "turn_completed", "run_id": "t1"],
            ["kind": "compaction_checkpoint", "node_id": "cmp1", "parent_id": "t1", "focus": "", "before": 40000, "after": 6000],
            ["kind": "turn_started", "run_id": "t2", "parent_id": "cmp1", "prompt": "After compaction"],
            ["kind": "turn_completed", "run_id": "t2"],
        ])
        let tree = PCodeConversationTree.parse(journalData: data)
        #expect(tree.activePath() == ["t1", "cmp1", "t2"])

        let withoutCompactions = tree.flattenedRows(includeCompactions: false)
        #expect(withoutCompactions.map(\.id) == ["t1", "t2"])
        #expect(withoutCompactions.last?.isActiveHead == true)
    }

    @Test @MainActor func reconcilePromptTurnIDsBackfillsLivePrompts() {
        let typography = PCodeTypography.shared
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let scrollView = PCodeTranscriptScrollView(typography: typography, theme: theme)
        scrollView.frame = NSRect(x: 0, y: 0, width: 640, height: 320)
        scrollView.layoutSubtreeIfNeeded()

        let doc = scrollView.transcriptDocumentView
        doc.beginStreamingTurn(prompt: "Turn 1", isSide: false, turnID: nil, forcePin: false, reveal: false)
        doc.beginStreamingTurn(prompt: "Turn 2", isSide: false, turnID: nil, forcePin: false, reveal: false)
        #expect(!doc.scrollToTurn("uuid-2"))

        doc.reconcilePromptTurnIDs(["uuid-1", "uuid-2"])
        #expect(doc.scrollToTurn("uuid-2"))
        #expect(doc.currentVisibleTurnID() == "uuid-2")
    }

    @Test func activeSpineStaysOnLaneZeroAndBranchesPeelOffWithContinuousTones() {
        let data = Self.jsonl([
            ["kind": "turn_started", "run_id": "t1", "parent_id": NSNull(), "prompt": "Root"],
            ["kind": "turn_completed", "run_id": "t1"],
            // Inactive branch off t1 (2 turns)
            ["kind": "turn_started", "run_id": "t2a", "parent_id": "t1", "prompt": "Alt 1"],
            ["kind": "turn_completed", "run_id": "t2a"],
            ["kind": "turn_started", "run_id": "t3a", "parent_id": "t2a", "prompt": "Alt 2"],
            ["kind": "turn_completed", "run_id": "t3a"],
            // /btw off t1
            ["kind": "turn_started", "run_id": "s1", "parent_id": "t1", "prompt": "Aside?", "side": ["thread": "a"]],
            ["kind": "turn_completed", "run_id": "s1"],
            // Active branch off t1
            ["kind": "tree_selected", "node_id": "t1"],
            ["kind": "turn_started", "run_id": "t2b", "parent_id": "t1", "prompt": "Main 2"],
            ["kind": "turn_completed", "run_id": "t2b"],
            ["kind": "turn_started", "run_id": "t3b", "parent_id": "t2b", "prompt": "Main 3"],
            ["kind": "turn_completed", "run_id": "t3b"],
        ])

        let tree = PCodeConversationTree.parse(journalData: data)
        let rows = tree.flattenedRows()
        #expect(rows.map(\.id) == ["t1", "t2a", "t3a", "s1", "t2b", "t3b"])

        // Active spine stays at lane 0 throughout.
        #expect(rows[0].indentLevel == 0)
        #expect(rows[0].outgoingLaneTone == .active)
        #expect(rows[0].visibleChildCount == 3)

        // Inactive branch root peels off lane 0 -> lane 1, while active trunk passes through lane 0.
        #expect(rows[1].id == "t2a")
        #expect(rows[1].indentLevel == 1)
        #expect(rows[1].parentTurnNumber == 1)
        #expect(rows[1].incomingLaneTone == nil)
        #expect(rows[1].outgoingLaneTone == .inactive)
        #expect(rows[1].railColumns == [.branch(trunkAbove: .active, trunkBelow: .active, arm: .inactive)])

        // Linear child on inactive branch stays on lane 1 with continuous gray vertical + active lane 0 pass-through.
        #expect(rows[2].id == "t3a")
        #expect(rows[2].indentLevel == 1)
        #expect(rows[2].incomingLaneTone == .inactive)
        #expect(rows[2].outgoingLaneTone == nil)
        #expect(rows[2].railColumns == [.vertical(tone: .active)])

        // /btw aside peels off lane 0 with purple arm, active trunk still passes through lane 0.
        #expect(rows[3].id == "s1")
        #expect(rows[3].indentLevel == 1)
        #expect(rows[3].railColumns == [.branch(trunkAbove: .active, trunkBelow: .active, arm: .aside)])

        // Active continuation stays on lane 0 with unbroken active vertical.
        #expect(rows[4].id == "t2b")
        #expect(rows[4].indentLevel == 0)
        #expect(rows[4].incomingLaneTone == .active)
        #expect(rows[4].outgoingLaneTone == .active)
        #expect(rows[4].railColumns.isEmpty)

        // Active HEAD terminates lane 0 cleanly.
        #expect(rows[5].id == "t3b")
        #expect(rows[5].indentLevel == 0)
        #expect(rows[5].incomingLaneTone == .active)
        #expect(rows[5].outgoingLaneTone == nil)
    }

    @Test func headWithOnlyInactiveChildrenPeelsAllChildrenOffInInactiveTone() {
        let data = Self.jsonl([
            ["kind": "turn_started", "run_id": "t1", "parent_id": NSNull(), "prompt": "Root"],
            ["kind": "turn_completed", "run_id": "t1"],
            ["kind": "turn_started", "run_id": "t2a", "parent_id": "t1", "prompt": "Branch A"],
            ["kind": "turn_completed", "run_id": "t2a"],
            ["kind": "tree_selected", "node_id": "t1"],
            ["kind": "turn_started", "run_id": "t2b", "parent_id": "t1", "prompt": "Branch B"],
            ["kind": "turn_completed", "run_id": "t2b"],
            ["kind": "tree_selected", "node_id": "t1"],
        ])

        let tree = PCodeConversationTree.parse(journalData: data)
        let rows = tree.flattenedRows()
        #expect(rows.map(\.id) == ["t1", "t2a", "t2b"])
        #expect(rows[0].isActiveHead)
        #expect(rows[0].outgoingLaneTone == .inactive)
        #expect(rows[1].indentLevel == 1)
        #expect(rows[1].railColumns == [.branch(trunkAbove: .inactive, trunkBelow: .inactive, arm: .inactive)])
        #expect(rows[2].indentLevel == 1)
        #expect(rows[2].railColumns == [.branch(trunkAbove: .inactive, trunkBelow: nil, arm: .inactive)])
    }

    @Test func nestedForksMultipleRootsToneTransitionsAndSearchFlattening() {
        let data = Self.jsonl([
            // Inactive root 1 with nested fork
            ["kind": "turn_started", "run_id": "r1", "parent_id": NSNull(), "prompt": "Old root"],
            ["kind": "turn_completed", "run_id": "r1"],
            ["kind": "turn_started", "run_id": "r1a", "parent_id": "r1", "prompt": "Sub branch A"],
            ["kind": "turn_completed", "run_id": "r1a"],
            ["kind": "tree_selected", "node_id": "r1"],
            ["kind": "turn_started", "run_id": "r1b", "parent_id": "r1", "prompt": "Sub branch B"],
            ["kind": "turn_completed", "run_id": "r1b"],
            // Active root 2 with compaction and a fork off compaction
            ["kind": "tree_selected", "node_id": NSNull()],
            ["kind": "turn_started", "run_id": "r2", "parent_id": NSNull(), "prompt": "Active root"],
            ["kind": "turn_completed", "run_id": "r2"],
            ["kind": "compaction_checkpoint", "node_id": "c1", "parent_id": "r2", "focus": "ctx", "before": 20000, "after": 4000],
            ["kind": "turn_started", "run_id": "after_c_inactive", "parent_id": "c1", "prompt": "Fork off compaction"],
            ["kind": "turn_completed", "run_id": "after_c_inactive"],
            ["kind": "tree_selected", "node_id": "c1"],
            ["kind": "turn_started", "run_id": "s_on_c", "parent_id": "c1", "prompt": "Aside on compaction", "side": ["thread": "t"]],
            ["kind": "turn_completed", "run_id": "s_on_c"],
        ])

        let tree = PCodeConversationTree.parse(journalData: data)
        let rows = tree.flattenedRows()
        #expect(rows.map(\.id) == ["r1", "r1a", "r1b", "r2", "c1", "after_c_inactive", "s_on_c"])

        // Inactive root peels off Conversation start (parentTurnNumber == nil, parentKind == nil).
        #expect(rows[0].indentLevel == 1)
        #expect(rows[0].parentTurnNumber == nil)
        #expect(rows[0].parentKind == nil)
        #expect(rows[0].railColumns == [.branch(trunkAbove: .active, trunkBelow: .active, arm: .inactive)])

        // Nested fork inside inactive root peels to lane 2.
        #expect(rows[1].id == "r1a")
        #expect(rows[1].indentLevel == 2)
        #expect(rows[1].parentTurnNumber == 1)
        #expect(rows[1].parentKind == .turn)
        #expect(rows[1].railColumns == [
            .vertical(tone: .active),
            .branch(trunkAbove: .inactive, trunkBelow: .inactive, arm: .inactive),
        ])

        // Second child continues in-lane on lane 1.
        #expect(rows[2].id == "r1b")
        #expect(rows[2].indentLevel == 1)
        #expect(rows[2].incomingLaneTone == .inactive)
        #expect(rows[2].railColumns == [.vertical(tone: .active)])

        // Fork off compaction records parentKind == .compaction and transitions trunk from inactive -> aside.
        let forkOffCompaction = rows.first { $0.id == "after_c_inactive" }
        #expect(forkOffCompaction?.parentKind == .compaction)
        #expect(forkOffCompaction?.railColumns == [.branch(trunkAbove: .inactive, trunkBelow: .aside, arm: .inactive)])

        let asideOnCompaction = rows.first { $0.id == "s_on_c" }
        #expect(asideOnCompaction?.railColumns == [.branch(trunkAbove: .aside, trunkBelow: nil, arm: .aside)])

        // Collapsing r1 hides descendants but keeps active lane-0 trunkBelow intact.
        let collapsed = tree.flattenedRows(collapsedBranchRoots: ["r1"])
        #expect(collapsed.first?.id == "r1")
        #expect(collapsed.first?.outgoingLaneTone == nil)
        #expect(collapsed.first?.railColumns == [.branch(trunkAbove: .active, trunkBelow: .active, arm: .inactive)])

        // Search flattens rails and finds matches even inside collapsed branches.
        let searched = tree.flattenedRows(collapsedBranchRoots: ["r1"], searchQuery: "Sub branch A")
        #expect(searched.count == 1)
        #expect(searched[0].id == "r1a")
        #expect(searched[0].indentLevel == 0)
        #expect(searched[0].railColumns.isEmpty)
        #expect(searched[0].incomingLaneTone == nil)
        #expect(searched[0].outgoingLaneTone == nil)
    }
}
