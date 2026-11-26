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
}
