import Foundation
import Testing
@testable import Ghostty

@Suite
struct PCodeLaunchTests {
    private func makeTempTree() throws -> URL {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("pcode-launch-tests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        return root
    }

    private func makeExecutable(at url: URL) throws {
        FileManager.default.createFile(atPath: url.path, contents: Data("#!/bin/sh\n".utf8))
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: url.path)
    }

    @Test func hasEnvrcFindsCurrentAndAncestorFilesOnly() throws {
        let root = try makeTempTree()
        defer { try? FileManager.default.removeItem(at: root) }

        let child = root.appendingPathComponent("repo/nested", isDirectory: true)
        try FileManager.default.createDirectory(at: child, withIntermediateDirectories: true)
        #expect(!PCodeWorkspaceMode.hasEnvrc(in: child.path))

        // A directory named `.envrc` should not count as an `.envrc` file.
        let fakeDir = root.appendingPathComponent("repo/.envrc", isDirectory: true)
        try FileManager.default.createDirectory(at: fakeDir, withIntermediateDirectories: true)
        #expect(!PCodeWorkspaceMode.hasEnvrc(in: child.path))
        try FileManager.default.removeItem(at: fakeDir)

        // Empty path returns false instead of walking process CWD.
        #expect(!PCodeWorkspaceMode.hasEnvrc(in: ""))

        // An `.envrc` file in an ancestor applies to descendants.
        let envrc = root.appendingPathComponent(".envrc")
        FileManager.default.createFile(atPath: envrc.path, contents: Data("export FOO=1\n".utf8))
        #expect(PCodeWorkspaceMode.hasEnvrc(in: root.path))
        #expect(PCodeWorkspaceMode.hasEnvrc(in: child.path))
    }

    @Test func findExecutableResolvesFirstExecutableOnPath() throws {
        let root = try makeTempTree()
        defer { try? FileManager.default.removeItem(at: root) }

        let dir1 = root.appendingPathComponent("bin1", isDirectory: true)
        let dir2 = root.appendingPathComponent("bin2", isDirectory: true)
        let dir3 = root.appendingPathComponent("bin3", isDirectory: true)
        try FileManager.default.createDirectory(at: dir1, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: dir2, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: dir3, withIntermediateDirectories: true)

        // Non-executable file in dir1 and directory named `direnv` in dir2 should be skipped.
        let nonExec = dir1.appendingPathComponent("direnv")
        FileManager.default.createFile(atPath: nonExec.path, contents: Data())
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: nonExec.path)
        try FileManager.default.createDirectory(at: dir2.appendingPathComponent("direnv"), withIntermediateDirectories: true)

        let target = root.appendingPathComponent("real-direnv")
        try makeExecutable(at: target)
        let symlink = dir3.appendingPathComponent("direnv")
        try FileManager.default.createSymbolicLink(at: symlink, withDestinationURL: target)

        let found = PCodeWorkspaceMode.findExecutable("direnv", inPath: "relative:\(dir1.path):\(dir2.path):\(dir3.path)")
        #expect(found == symlink.path)
        #expect(PCodeWorkspaceMode.findExecutable("missing", inPath: "\(dir1.path):\(dir3.path)") == nil)
    }

    @Test func cachedLaunchWrapsInDirenvExecWhenEnvrcOrDirenvDirApplies() throws {
        let root = try makeTempTree()
        defer { try? FileManager.default.removeItem(at: root) }

        let pcodeBin = root.appendingPathComponent("pcode")
        let direnvBin = root.appendingPathComponent("direnv")
        try makeExecutable(at: pcodeBin)
        try makeExecutable(at: direnvBin)

        let project = root.appendingPathComponent("work/repo", isDirectory: true)
        try FileManager.default.createDirectory(at: project, withIntermediateDirectories: true)

        let cached = PCodeWorkspaceMode.CachedShellEnv(
            commandPath: pcodeBin.path,
            direnvPath: direnvBin.path,
            env: ["PATH": "/opt/homebrew/bin:/usr/bin"]
        )

        // Without `.envrc` or `DIRENV_DIR`: direct launch, no direnv wrapper.
        let plain = PCodeWorkspaceMode.buildLaunchConfig(
            ["-c", "abc123"],
            directory: project.path,
            cached: cached,
            baseEnvironment: [:]
        )
        #expect(plain.command == "/usr/bin/env -- '\(pcodeBin.path)' '-c' 'abc123'")
        #expect(plain.env == cached.env)

        // With stale `DIRENV_DIR` in base env: wraps in `direnv exec` so it unloads.
        let unload = PCodeWorkspaceMode.buildLaunchConfig(
            ["-c", "abc123"],
            directory: project.path,
            cached: cached,
            baseEnvironment: ["DIRENV_DIR": "-/other/dir"]
        )
        #expect(unload.command == "/usr/bin/env -- '\(direnvBin.path)' exec '\(project.path)' '\(pcodeBin.path)' '-c' 'abc123'")

        // With `.envrc` in ancestor: wraps in `direnv exec`.
        let envrc = root.appendingPathComponent("work/.envrc")
        FileManager.default.createFile(atPath: envrc.path, contents: Data())
        let withEnvrc = PCodeWorkspaceMode.buildLaunchConfig(
            [],
            directory: project.path,
            cached: cached,
            baseEnvironment: [:]
        )
        #expect(withEnvrc.command == "/usr/bin/env -- '\(direnvBin.path)' exec '\(project.path)' '\(pcodeBin.path)'")

        // When `direnv` is not installed (`direnvPath == nil`) or missing: falls back to direct launch.
        let noDirenv = PCodeWorkspaceMode.buildLaunchConfig(
            [],
            directory: project.path,
            cached: PCodeWorkspaceMode.CachedShellEnv(commandPath: pcodeBin.path, direnvPath: nil, env: cached.env),
            baseEnvironment: [:]
        )
        #expect(noDirenv.command == "/usr/bin/env -- '\(pcodeBin.path)'")

        let staleDirenv = PCodeWorkspaceMode.buildLaunchConfig(
            [],
            directory: project.path,
            cached: PCodeWorkspaceMode.CachedShellEnv(
                commandPath: pcodeBin.path,
                direnvPath: root.appendingPathComponent("missing-direnv").path,
                env: cached.env
            ),
            baseEnvironment: [:]
        )
        #expect(staleDirenv.command == "/usr/bin/env -- '\(pcodeBin.path)'")
    }

    @Test func discardedCachedEnvDoesNotMaskBaseDirenvDir() throws {
        let root = try makeTempTree()
        defer { try? FileManager.default.removeItem(at: root) }

        let project = root.appendingPathComponent("repo", isDirectory: true)
        try FileManager.default.createDirectory(at: project, withIntermediateDirectories: true)

        let staleCached = PCodeWorkspaceMode.CachedShellEnv(
            commandPath: root.appendingPathComponent("missing-pcode").path,
            direnvPath: nil,
            env: ["DIRENV_DIR": ""]
        )
        let fallback = PCodeWorkspaceMode.buildLaunchConfig(
            [],
            directory: project.path,
            cached: staleCached,
            baseEnvironment: ["DIRENV_DIR": "-/other/dir"],
            shell: "/bin/zsh",
            command: "pcode"
        )
        #expect(fallback.command.contains("exec direnv exec"))
        #expect(fallback.env.isEmpty)
    }

    @Test func fallbackShellCommandConditionallyUsesDirenvForPosixAndFish() throws {
        let root = try makeTempTree()
        defer { try? FileManager.default.removeItem(at: root) }

        let project = root.appendingPathComponent("repo", isDirectory: true)
        try FileManager.default.createDirectory(at: project, withIntermediateDirectories: true)

        // Without `.envrc`: plain `exec pcode`.
        let plain = PCodeWorkspaceMode.buildLaunchConfig(
            ["-c", "s1"],
            directory: project.path,
            cached: nil,
            baseEnvironment: [:],
            shell: "/bin/zsh",
            command: "pcode"
        )
        #expect(plain.command == "'/bin/zsh' -l -c 'exec pcode '\\''-c'\\'' '\\''s1'\\'''")
        #expect(plain.env.isEmpty)

        // Absolute `PCODE_COMMAND` with spaces is quoted.
        let spacedCmd = PCodeWorkspaceMode.shellCommand(
            [],
            directory: project.path,
            useDirenv: false,
            shell: "/bin/zsh",
            command: "/Users/me/My Work/pcode"
        )
        #expect(spacedCmd.contains("exec '\\''/Users/me/My Work/pcode'\\''"))

        // With `.envrc`: POSIX shell checks `command -v direnv`.
        FileManager.default.createFile(atPath: project.appendingPathComponent(".envrc").path, contents: Data())
        let zsh = PCodeWorkspaceMode.buildLaunchConfig(
            [],
            directory: project.path,
            cached: nil,
            baseEnvironment: [:],
            shell: "/bin/zsh",
            command: "pcode"
        )
        #expect(zsh.command.contains("command -v direnv >/dev/null 2>&1"))
        #expect(zsh.command.contains("exec direnv exec"))

        // With `.envrc` and fish: uses fish's `type -q direnv` and fish single-quote escaping.
        let trickyDir = root.appendingPathComponent("fish's\\dir", isDirectory: true)
        try FileManager.default.createDirectory(at: trickyDir, withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: trickyDir.appendingPathComponent(".envrc").path, contents: Data())
        let fish = PCodeWorkspaceMode.buildLaunchConfig(
            [],
            directory: trickyDir.path,
            cached: nil,
            baseEnvironment: [:],
            shell: "/opt/homebrew/bin/fish",
            command: "pcode"
        )
        #expect(fish.command.contains("if type -q direnv; exec direnv exec"))
        #expect(fish.command.contains("fish\\'\\''s\\\\dir"))
        #expect(fish.command.contains("; else; exec pcode; end"))
    }
}
