import Foundation
import Testing
@testable import Ghostty

struct PCodeInspectorModelsTests {
    @Test func diffSelectionFormatsSingleAndMultiLineReferences() {
        let single = PCodeDiffSelection(path: "src/auth.swift", startLine: 42, endLine: 42)
        #expect(single.promptReference == "@src/auth.swift:L42")

        let reversed = PCodeDiffSelection(path: "src/auth.swift", startLine: 58, endLine: 42)
        #expect(reversed.startLine == 42)
        #expect(reversed.endLine == 58)
        #expect(reversed.promptReference == "@src/auth.swift:L42-L58")
    }

    @Test func fileDiffInfersLanguageAndFirstChangedLine() {
        let hunk = PCodeDiffHunk(
            id: "h1",
            header: "@@ -10,3 +10,4 @@",
            oldStart: 10,
            oldCount: 3,
            newStart: 10,
            newCount: 4,
            lines: [
                PCodeDiffLine(id: "l1", kind: .context, oldLineNumber: 10, newLineNumber: 10, text: "let a = 1"),
                PCodeDiffLine(id: "l2", kind: .added, oldLineNumber: nil, newLineNumber: 11, text: "let b = 2"),
            ]
        )
        let diff = PCodeFileDiff(path: "app/macos/PCode/Auth.swift", hunks: [hunk])
        #expect(diff.language == "swift")
        #expect(diff.basename == "Auth.swift")
        #expect(diff.parentDirectory == "app/macos/PCode")
        #expect(diff.firstChangedLine == 11)
    }

    @Test func inspectorStateResponsiveAndNavigationHelpers() {
        #expect(PCodeInspectorState.shouldUseOverlay(containerWidth: 1000))
        #expect(!PCodeInspectorState.shouldUseOverlay(containerWidth: 1200))

        let state = PCodeInspectorState()
        #expect(!state.isOpen)
        #expect(state.diffScope == .session)

        state.toggleCollapsed(filePath: "a.swift")
        #expect(state.collapsedFiles.contains("a.swift"))

        state.openChanges(filePath: "a.swift", scope: .workingTree)
        #expect(state.isOpen)
        #expect(state.activeTab == .changes)
        #expect(state.diffScope == .workingTree)
        #expect(state.focusedFilePath == "a.swift")
        #expect(!state.collapsedFiles.contains("a.swift"))

        state.toggleExpanded()
        #expect(state.isExpanded)
        state.close()
        #expect(!state.isOpen)
        #expect(!state.isExpanded)
    }

    @Test func changeKindMapsOperationStrings() {
        #expect(PCodeFileChangeKind(operation: "write").badge == "A")
        #expect(PCodeFileChangeKind(operation: "create").badge == "A")
        #expect(PCodeFileChangeKind(operation: "delete").badge == "D")
        #expect(PCodeFileChangeKind(operation: "rename").badge == "R")
        #expect(PCodeFileChangeKind(operation: "edit").badge == "M")
    }
}
