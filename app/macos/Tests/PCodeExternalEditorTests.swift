import Foundation
import Testing
@testable import Ghostty

struct PCodeExternalEditorTests {
    private let fakeResolver: (String) -> String? = { cmd in
        if cmd.hasPrefix("/") { return cmd }
        return "/usr/local/bin/\(cmd)"
    }

    // MARK: - Command parsing & preference resolution

    @Test func splitCommandHandlesFlagsQuotesAndEscapes() {
        #expect(PCodeExternalEditor.splitCommand("code --wait") == ["code", "--wait"])
        #expect(
            PCodeExternalEditor.splitCommand("open -a \"Visual Studio Code\" --args") ==
                ["open", "-a", "Visual Studio Code", "--args"]
        )
        #expect(
            PCodeExternalEditor.splitCommand("'/Applications/My Editor/bin/ed' -n") ==
                ["/Applications/My Editor/bin/ed", "-n"]
        )
        #expect(
            PCodeExternalEditor.splitCommand("my\\ editor --flag") ==
                ["my editor", "--flag"]
        )
        #expect(PCodeExternalEditor.splitCommand("   \t\n  ") == [])
    }

    @Test func preferredEditorCommandFollowsDefaultsThenVisualThenEditor() {
        let suiteName = "PCodeExternalEditorTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer { defaults.removePersistentDomain(forName: suiteName) }

        #expect(
            PCodeExternalEditor.preferredEditorCommand(
                userDefaults: defaults,
                environment: ["VISUAL": "zed", "EDITOR": "nvim"]
            ) == "zed"
        )
        #expect(
            PCodeExternalEditor.preferredEditorCommand(
                userDefaults: defaults,
                environment: ["VISUAL": "   ", "EDITOR": "nvim"]
            ) == "nvim"
        )
        #expect(
            PCodeExternalEditor.preferredEditorCommand(
                userDefaults: defaults,
                environment: [:]
            ) == nil
        )

        defaults.set("cursor --reuse-window", forKey: PCodeExternalEditor.userDefaultsKey)
        #expect(
            PCodeExternalEditor.preferredEditorCommand(
                userDefaults: defaults,
                environment: ["VISUAL": "zed", "EDITOR": "nvim"]
            ) == "cursor --reuse-window"
        )
    }

    // MARK: - Line-number formatting per editor

    @Test func launchSpecFormatsGotoEditors() {
        let code = PCodeExternalEditor.launchSpec(
            path: "/repo/Auth.swift",
            line: 42,
            editorEnv: "code --wait",
            executableResolver: fakeResolver
        )
        #expect(
            code == PCodeExternalEditor.LaunchSpec(
                executable: "/usr/local/bin/code",
                arguments: ["--wait", "--goto", "/repo/Auth.swift:42"],
                isTerminalEditor: false
            )
        )

        let cursor = PCodeExternalEditor.launchSpec(
            path: "/repo/Auth.swift",
            line: 17,
            editorEnv: "cursor -g",
            executableResolver: fakeResolver
        )
        #expect(
            cursor == PCodeExternalEditor.LaunchSpec(
                executable: "/usr/local/bin/cursor",
                arguments: ["-g", "/repo/Auth.swift:17"],
                isTerminalEditor: false
            )
        )

        let insidersNoLine = PCodeExternalEditor.launchSpec(
            path: "/repo/Auth.swift",
            line: nil,
            editorEnv: "code-insiders",
            executableResolver: fakeResolver
        )
        #expect(
            insidersNoLine == PCodeExternalEditor.LaunchSpec(
                executable: "/usr/local/bin/code-insiders",
                arguments: ["/repo/Auth.swift"],
                isTerminalEditor: false
            )
        )
    }

    @Test func launchSpecFormatsColonLineEditors() {
        let zed = PCodeExternalEditor.launchSpec(
            path: "/repo/Auth.swift",
            line: 88,
            editorEnv: "zed",
            executableResolver: fakeResolver
        )
        #expect(
            zed == PCodeExternalEditor.LaunchSpec(
                executable: "/usr/local/bin/zed",
                arguments: ["/repo/Auth.swift:88"],
                isTerminalEditor: false
            )
        )

        let subl = PCodeExternalEditor.launchSpec(
            path: "/repo/Auth.swift",
            line: 12,
            editorEnv: "subl -n",
            executableResolver: fakeResolver
        )
        #expect(
            subl == PCodeExternalEditor.LaunchSpec(
                executable: "/usr/local/bin/subl",
                arguments: ["-n", "/repo/Auth.swift:12"],
                isTerminalEditor: false
            )
        )
    }

    @Test func launchSpecFormatsTerminalEditorsAndMarksFlag() {
        for editor in ["nvim", "vim", "vi", "nano", "emacs"] {
            let spec = PCodeExternalEditor.launchSpec(
                path: "/repo/Auth.swift",
                line: 55,
                editorEnv: editor,
                executableResolver: fakeResolver
            )
            #expect(
                spec == PCodeExternalEditor.LaunchSpec(
                    executable: "/usr/local/bin/\(editor)",
                    arguments: ["+55", "/repo/Auth.swift"],
                    isTerminalEditor: true
                )
            )
        }

        let openApp = PCodeExternalEditor.launchSpec(
            path: "/repo/Auth.swift",
            line: 55,
            editorEnv: "open -a \"Visual Studio Code\"",
            executableResolver: fakeResolver
        )
        #expect(
            openApp == PCodeExternalEditor.LaunchSpec(
                executable: "/usr/local/bin/open",
                arguments: ["-a", "Visual Studio Code", "/repo/Auth.swift"],
                isTerminalEditor: false
            )
        )
    }

    @Test func launchSpecIgnoresNonPositiveLineAndUnresolvedExecutable() {
        let nonPositive = PCodeExternalEditor.launchSpec(
            path: "/repo/Auth.swift",
            line: 0,
            editorEnv: "code",
            executableResolver: fakeResolver
        )
        #expect(nonPositive?.arguments == ["/repo/Auth.swift"])

        let missing = PCodeExternalEditor.launchSpec(
            path: "/repo/Auth.swift",
            line: 10,
            editorEnv: "nonexistent-editor",
            executableResolver: { _ in nil }
        )
        #expect(missing == nil)

        let empty = PCodeExternalEditor.launchSpec(
            path: "/repo/Auth.swift",
            line: 10,
            editorEnv: "   ",
            executableResolver: fakeResolver
        )
        #expect(empty == nil)
    }

    // MARK: - Fallback behavior

    @Test func openLaunchesGUIEditorAndFallsBackForTerminalMissingOrError() {
        var launched: [PCodeExternalEditor.LaunchSpec] = []
        var workspaceURLs: [URL] = []

        // GUI editor succeeds without calling NSWorkspace fallback.
        let ok = PCodeExternalEditor.open(
            path: "/repo/Auth.swift",
            line: 42,
            editorEnv: "zed",
            executableResolver: fakeResolver,
            processLauncher: { launched.append($0) },
            workspaceOpener: { url in
                workspaceURLs.append(url)
                return true
            }
        )
        #expect(ok)
        #expect(launched.count == 1)
        #expect(launched.first?.arguments == ["/repo/Auth.swift:42"])
        #expect(workspaceURLs.isEmpty)

        // Terminal-only editor skips headless Process launch and falls back to NSWorkspace.
        launched.removeAll()
        let terminalFallback = PCodeExternalEditor.open(
            path: "/repo/Auth.swift",
            line: 42,
            editorEnv: "nvim",
            executableResolver: fakeResolver,
            processLauncher: { launched.append($0) },
            workspaceOpener: { url in
                workspaceURLs.append(url)
                return true
            }
        )
        #expect(terminalFallback)
        #expect(launched.isEmpty)
        #expect(workspaceURLs == [URL(fileURLWithPath: "/repo/Auth.swift")])

        // Missing editor falls back to NSWorkspace.
        workspaceURLs.removeAll()
        let missingFallback = PCodeExternalEditor.open(
            path: "/repo/Auth.swift",
            line: 42,
            editorEnv: nil,
            executableResolver: fakeResolver,
            processLauncher: { launched.append($0) },
            workspaceOpener: { url in
                workspaceURLs.append(url)
                return true
            }
        )
        #expect(missingFallback)
        #expect(workspaceURLs == [URL(fileURLWithPath: "/repo/Auth.swift")])

        // Process launch failure falls back to NSWorkspace.
        struct LaunchFailure: Error {}
        workspaceURLs.removeAll()
        let errorFallback = PCodeExternalEditor.open(
            path: "/repo/Auth.swift",
            line: 42,
            editorEnv: "code",
            executableResolver: fakeResolver,
            processLauncher: { _ in throw LaunchFailure() },
            workspaceOpener: { url in
                workspaceURLs.append(url)
                return true
            }
        )
        #expect(errorFallback)
        #expect(workspaceURLs == [URL(fileURLWithPath: "/repo/Auth.swift")])
    }

    // MARK: - Composer insertion & bracketed paste

    @Test func formattedInsertionHandlesLeadingSpaceAndTrimming() {
        #expect(
            PCodeComposerInsertion.formattedInsertion(reference: "@src/auth.swift:L42-L58") ==
                " @src/auth.swift:L42-L58 "
        )
        #expect(
            PCodeComposerInsertion.formattedInsertion(reference: "@src/auth.swift:L42", leadingSpace: false) ==
                "@src/auth.swift:L42 "
        )
        #expect(
            PCodeComposerInsertion.formattedInsertion(reference: "  @src/auth.swift:L42 \n") ==
                " @src/auth.swift:L42 "
        )
        #expect(PCodeComposerInsertion.formattedInsertion(reference: "   ") == "")
    }

    @Test func bracketedPasteBytesWrapsUTF8AndSanitizesBreakoutSequences() {
        let empty = PCodeComposerInsertion.bracketedPasteBytes(for: "")
        #expect(empty.isEmpty)

        let multiline = "line 1\nline 2 — café"
        let encoded = PCodeComposerInsertion.bracketedPasteBytes(for: multiline)
        #expect(String(data: encoded, encoding: .utf8) == "\u{1b}[200~line 1\nline 2 — café\u{1b}[201~")

        // Embedded end/start paste sequences must be stripped to prevent bracketed-paste breakout.
        let hostile = "hello\u{1b}[201~\nrm -rf /\u{1b}[200~world"
        let safe = PCodeComposerInsertion.bracketedPasteBytes(for: hostile)
        #expect(String(data: safe, encoding: .utf8) == "\u{1b}[200~hello\nrm -rf /world\u{1b}[201~")
    }

    // MARK: - Worker prefill prompts

    @Test func workerPromptHelpersFormatIDRoleAndBodies() {
        #expect(
            PCodeComposerInsertion.workerStopPrompt(id: "w3", role: "Codebase Researcher") ==
                "Stop worker w3 (Codebase Researcher)."
        )
        #expect(
            PCodeComposerInsertion.workerStopPrompt(id: "w3", role: "") ==
                "Stop worker w3."
        )
        #expect(
            PCodeComposerInsertion.workerMessagePrompt(
                id: "w3",
                role: "Codebase Researcher",
                message: "Focus on auth.py"
            ) == "Message worker w3 (Codebase Researcher): Focus on auth.py"
        )
        #expect(
            PCodeComposerInsertion.workerMessagePrompt(id: "w3", role: "", message: "   ") ==
                "Message worker w3: "
        )
        #expect(
            PCodeComposerInsertion.workerAnswerPrompt(
                id: "w7",
                role: "Database Debugger",
                answer: "Use the read replica"
            ) == "Answer worker w7 (Database Debugger): Use the read replica"
        )
        #expect(
            PCodeComposerInsertion.workerAnswerPrompt(id: "w7", role: "", answer: "") ==
                "Answer worker w7: "
        )
    }
}
