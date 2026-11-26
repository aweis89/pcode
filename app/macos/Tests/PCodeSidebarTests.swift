import CoreGraphics
import Foundation
import Testing
@testable import Ghostty

@Suite
struct PCodeSidebarTests {
    @Test func clampsWidthWithinBounds() {
        #expect(PCodeSidebarLayout.clampedWidth(100, totalWidth: 1200) == PCodeSidebarLayout.minWidth)
        #expect(PCodeSidebarLayout.clampedWidth(320, totalWidth: 1200) == 320)
        #expect(PCodeSidebarLayout.clampedWidth(700, totalWidth: 1200) == PCodeSidebarLayout.maxWidth)
    }

    @Test func clampsWidthToReserveDetailPaneSpace() {
        // 700 - 360 = 340 max sidebar width in a narrow window.
        #expect(PCodeSidebarLayout.clampedWidth(420, totalWidth: 700) == 340)
        // Even in a tiny window, never shrink below minWidth.
        #expect(PCodeSidebarLayout.clampedWidth(420, totalWidth: 400) == PCodeSidebarLayout.minWidth)
    }

    @Test func fallsBackToDefaultForNonPositiveOrNonFiniteWidth() {
        #expect(PCodeSidebarLayout.clampedWidth(0, totalWidth: 1200) == PCodeSidebarLayout.defaultWidth)
        #expect(PCodeSidebarLayout.clampedWidth(-50, totalWidth: 1200) == PCodeSidebarLayout.defaultWidth)
        #expect(PCodeSidebarLayout.clampedWidth(.nan, totalWidth: 1200) == PCodeSidebarLayout.defaultWidth)
        #expect(PCodeSidebarLayout.clampedWidth(.infinity, totalWidth: 1200) == PCodeSidebarLayout.defaultWidth)
    }

    @Test func revealEdgeHitZone() {
        let bounds = CGRect(x: 0, y: 0, width: 1200, height: 800)
        #expect(PCodeSidebarLayout.shouldReveal(at: CGPoint(x: 0, y: 400), in: bounds))
        #expect(PCodeSidebarLayout.shouldReveal(at: CGPoint(x: PCodeSidebarLayout.revealEdgeWidth, y: 400), in: bounds))
        #expect(!PCodeSidebarLayout.shouldReveal(at: CGPoint(x: PCodeSidebarLayout.revealEdgeWidth + 1, y: 400), in: bounds))
        #expect(!PCodeSidebarLayout.shouldReveal(at: CGPoint(x: -1, y: 400), in: bounds))
        #expect(!PCodeSidebarLayout.shouldReveal(at: CGPoint(x: 2, y: 801), in: bounds))
    }

    @Test func revealedZoneIncludesGraceMargin() {
        let bounds = CGRect(x: 0, y: 0, width: 1200, height: 800)
        let width: CGFloat = 280
        #expect(PCodeSidebarLayout.isInsideRevealedZone(at: CGPoint(x: 100, y: 400), in: bounds, sidebarWidth: width))
        #expect(PCodeSidebarLayout.isInsideRevealedZone(
            at: CGPoint(x: width + PCodeSidebarLayout.hideGraceMargin, y: 400),
            in: bounds,
            sidebarWidth: width
        ))
        #expect(!PCodeSidebarLayout.isInsideRevealedZone(
            at: CGPoint(x: width + PCodeSidebarLayout.hideGraceMargin + 1, y: 400),
            in: bounds,
            sidebarWidth: width
        ))
        #expect(!PCodeSidebarLayout.isInsideRevealedZone(at: CGPoint(x: -1, y: 400), in: bounds, sidebarWidth: width))
    }

    @Test func displayTitlePrefersLiveTitleOverSavedAndHandlesPlaceholders() {
        #expect(PCodeWorkspace.displayTitle(paneTitle: "", savedLabel: nil) == "New session")
        #expect(PCodeWorkspace.displayTitle(paneTitle: "👻", savedLabel: nil) == "New session")
        #expect(PCodeWorkspace.displayTitle(paneTitle: "👻", savedLabel: "Saved title") == "Saved title")
        #expect(PCodeWorkspace.displayTitle(paneTitle: "Renamed title", savedLabel: "Old title") == "Renamed title")
        #expect(PCodeWorkspace.displayTitle(paneTitle: "  Trimmed title  ", savedLabel: nil) == "Trimmed title")
        // Shortening `/rename` to a prefix of the old saved label must take effect immediately.
        #expect(PCodeWorkspace.displayTitle(paneTitle: "Fix auth bug", savedLabel: "Fix auth bug in login flow") == "Fix auth bug")
        // Normalize internal newlines and consecutive whitespace.
        #expect(PCodeWorkspace.displayTitle(paneTitle: "👻", savedLabel: "Line one\n  line   two") == "Line one line two")
        // Keep full saved title when OSC 0 live title is a 120-char truncated prefix of it.
        let full = String(repeating: "a", count: 140)
        let prefix = String(full.prefix(120))
        #expect(PCodeWorkspace.displayTitle(paneTitle: prefix, savedLabel: full) == full)
        let fullWithWhitespace = String(repeating: "a", count: 60) + "\n  " + String(repeating: "b", count: 80)
        let normalizedFull = String(repeating: "a", count: 60) + " " + String(repeating: "b", count: 80)
        let normalizedPrefix = String(normalizedFull.prefix(120))
        #expect(PCodeWorkspace.displayTitle(paneTitle: normalizedPrefix, savedLabel: fullWithWhitespace) == normalizedFull)
    }

    @Test func pathCompleterDetectsAndCompletesTildeAndAbsolutePaths() throws {
        let fm = FileManager.default
        let fakeHome = fm.temporaryDirectory.appendingPathComponent("pcode-path-test-\(UUID().uuidString)")
        let pDir = fakeHome.appendingPathComponent("p")
        try fm.createDirectory(at: pDir.appendingPathComponent("pcode"), withIntermediateDirectories: true)
        try fm.createDirectory(at: pDir.appendingPathComponent("pcode-docs"), withIntermediateDirectories: true)
        try fm.createDirectory(at: pDir.appendingPathComponent("alpha"), withIntermediateDirectories: true)
        try fm.createDirectory(at: pDir.appendingPathComponent(".hidden"), withIntermediateDirectories: true)
        try Data("hi".utf8).write(to: pDir.appendingPathComponent("pcode-file.txt"))
        defer { try? fm.removeItem(at: fakeHome) }

        let homePath = PCodeWorkspace.normalizePath(fakeHome.path)

        #expect(PCodePathCompleter.isPathQuery("~"))
        #expect(PCodePathCompleter.isPathQuery("~/p"))
        #expect(PCodePathCompleter.isPathQuery("/tmp"))
        #expect(!PCodePathCompleter.isPathQuery("pcode"))
        #expect(!PCodePathCompleter.isPathQuery(""))

        // Bare tilde completes to ~/
        let bare = PCodePathCompleter.complete(query: "~", home: homePath, fileManager: fm)
        #expect(bare == .init(completed: "~/", isUnique: true))

        // Unique tilde match preserves ~/ and appends /
        let uniqueTilde = PCodePathCompleter.complete(query: "~/p/al", home: homePath, fileManager: fm)
        #expect(uniqueTilde == .init(completed: "~/p/alpha/", isUnique: true))

        // Unique absolute match preserves absolute path and appends /
        let uniqueAbs = PCodePathCompleter.complete(query: "\(homePath)/p/al", home: homePath, fileManager: fm)
        #expect(uniqueAbs == .init(completed: "\(homePath)/p/alpha/", isUnique: true))

        // Multiple matches complete to longest common prefix (ignoring regular files)
        let lcp = PCodePathCompleter.complete(query: "~/p/pc", home: homePath, fileManager: fm)
        #expect(lcp == .init(completed: "~/p/pcode", isUnique: false))

        // Already at LCP: Tab completes into the currently selected candidate + /
        let drill = PCodePathCompleter.complete(
            query: "~/p/pcode",
            selectedPath: "\(homePath)/p/pcode-docs",
            home: homePath,
            fileManager: fm
        )
        #expect(drill == .init(completed: "~/p/pcode-docs/", isUnique: true))

        // Non-path query with selected candidate drills into ~/.../selected/
        let fromProject = PCodePathCompleter.complete(
            query: "pco",
            selectedPath: "\(homePath)/p/pcode",
            home: homePath,
            fileManager: fm
        )
        #expect(fromProject == .init(completed: "~/p/pcode/", isUnique: true))

        // Hidden directories skipped unless prefix starts with '.'
        let normalSubs = PCodePathCompleter.matchingSubdirectories(in: "\(homePath)/p", prefix: "", fileManager: fm)
        #expect(!normalSubs.contains("\(homePath)/p/.hidden"))
        let hiddenSubs = PCodePathCompleter.matchingSubdirectories(in: "\(homePath)/p", prefix: ".", fileManager: fm)
        #expect(hiddenSubs == ["\(homePath)/p/.hidden"])

        // Directory suggestions put exact match first, followed by matching subdirectories
        let suggestions = PCodePathCompleter.directorySuggestions(for: "~/p/pcode", home: homePath, fileManager: fm)
        #expect(suggestions == ["\(homePath)/p/pcode", "\(homePath)/p/pcode-docs"])

        // Case-insensitive exact match preserves canonical on-disk casing without duplicates
        try fm.createDirectory(at: pDir.appendingPathComponent("MixedCase"), withIntermediateDirectories: true)
        let canonical = PCodePathCompleter.directorySuggestions(for: "~/p/mixedcase", home: homePath, fileManager: fm)
        #expect(canonical == ["\(homePath)/p/MixedCase"])

        // Trailing spaces are preserved when completing spaced directory names
        try fm.createDirectory(at: pDir.appendingPathComponent("My App"), withIntermediateDirectories: true)
        try fm.createDirectory(at: pDir.appendingPathComponent("MyOther"), withIntermediateDirectories: true)
        let spaced = PCodePathCompleter.complete(query: "~/p/My ", home: homePath, fileManager: fm)
        #expect(spaced == .init(completed: "~/p/My App/", isUnique: true))

        // Unexpanded ~user without slash does not probe /~user
        #expect(PCodePathCompleter.split("~nonexistentuserxyz", home: homePath) == nil)
        #expect(PCodePathCompleter.directorySuggestions(for: "~nonexistentuserxyz", home: homePath, fileManager: fm).isEmpty)
    }

    @Test func sessionLoadIncludesZeroTurnLiveHostedSessions() throws {
        let fm = FileManager.default
        let root = fm.temporaryDirectory.appendingPathComponent("pcode-sessions-test-\(UUID().uuidString)")
        try fm.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: root) }

        func writeSession(id: String, turns: Int, updated: String, title: String) throws {
            let dir = root.appendingPathComponent(id)
            try fm.createDirectory(at: dir, withIntermediateDirectories: true)
            let json = """
            {"id":"\(id)","workspace":"/tmp/proj","updated":"\(updated)","title":"\(title)","turns":\(turns)}
            """
            try Data(json.utf8).write(to: dir.appendingPathComponent("session.json"))
        }

        try writeSession(id: "empty-idle", turns: 0, updated: "2026-11-26T01:00:00+00:00", title: "Empty")
        try writeSession(id: "live-zero-turns", turns: 0, updated: "2026-11-26T02:00:00+00:00", title: "Running first turn")
        try writeSession(id: "completed-new", turns: 2, updated: "2026-11-26T03:00:00+00:00", title: "Completed new")
        try writeSession(id: "live-old", turns: 1, updated: "2026-11-25T01:00:00+00:00", title: "Old live host")

        let loaded = PCodeSessionInfo.load(root: root, limit: 2, hosted: ["live-zero-turns", "live-old"])
        // Top 2 newest plus the live-old host that fell beyond the limit.
        #expect(loaded.map(\.id) == ["completed-new", "live-zero-turns", "live-old"])
    }

    @Test func hostedSessionsSurfaceActivityAndUnseenInProjectGroup() throws {
        let data = Data("""
        {"id":"s1","workspace":"/tmp/proj","updated":"2026-11-26T02:00:00+00:00","title":"Saved Title","turns":0}
        """.utf8)
        let info = try JSONDecoder().decode(PCodeSessionInfo.self, from: data)

        let workingHost = PCodeHostStatus(
            hostID: "h1", pid: 42, sessionID: "s1", state: "working",
            title: "Live Title", outcome: "", turns: 0, unseen: false, updated: 1_700_000_000)
        let working = PCodeHostedSession(info: info, host: workingHost)
        #expect(working.title == "Live Title")
        #expect(working.activity == .running)
        #expect(working.updatedDate == Date(timeIntervalSince1970: 1_700_000_000))

        let workingGroup = PCodeProjectGroup(path: "/tmp/proj", openPanes: [], hostedSessions: [working], recentSessions: [])
        #expect(workingGroup.activity == .running)
        #expect(!workingGroup.hasUnseen([]))
        #expect(workingGroup.hasUnseen(["s1"]))

        let failedHost = PCodeHostStatus(
            hostID: "h1", pid: 42, sessionID: "s1", state: "idle",
            title: "", outcome: "failed", turns: 1, unseen: true, updated: 0)
        let failed = PCodeHostedSession(info: info, host: failedHost)
        #expect(failed.title == "Saved Title")
        #expect(failed.activity == .failed)
        #expect(failed.updatedDate == info.updatedDate)

        let failedGroup = PCodeProjectGroup(path: "/tmp/proj", openPanes: [], hostedSessions: [failed], recentSessions: [])
        #expect(failedGroup.activity == .failed)
        #expect(failedGroup.hasUnseen([]))

        let startingHost = PCodeHostStatus(
            hostID: "h1", pid: 42, sessionID: "s1", state: "starting",
            title: "Booting", outcome: "cancelled", turns: 1, unseen: false, updated: 102)
        let idleGroup = PCodeProjectGroup(
            path: "/tmp/proj", openPanes: [],
            hostedSessions: [PCodeHostedSession(info: info, host: startingHost)],
            recentSessions: [])
        #expect(idleGroup.activity == .idle)
    }

    @Test func buildProjectGroupsPreservesNewerRecentsInHiddenFolderWithHostedSession() throws {
        func makeInfo(id: String, workspace: String, updated: String) throws -> PCodeSessionInfo {
            let json = """
            {"id":"\(id)","workspace":"\(workspace)","updated":"\(updated)","title":"\(id)","turns":1}
            """
            return try JSONDecoder().decode(PCodeSessionInfo.self, from: Data(json.utf8))
        }

        let hiddenPath = PCodeWorkspace.normalizePath("/tmp/hidden-proj")
        let visiblePath = PCodeWorkspace.normalizePath("/tmp/visible-proj")
        let newerRecent = try makeInfo(id: "newer-recent", workspace: hiddenPath, updated: "2026-11-26T03:00:00+00:00")
        let hostedOld = try makeInfo(id: "hosted-old", workspace: hiddenPath, updated: "2026-11-26T01:00:00+00:00")
        let olderRecent = try makeInfo(id: "older-recent", workspace: hiddenPath, updated: "2026-11-25T23:00:00+00:00")
        let visibleRecent = try makeInfo(id: "visible-recent", workspace: visiblePath, updated: "2026-11-26T02:00:00+00:00")

        let host = PCodeHostStatus(
            hostID: "h1", pid: 42, sessionID: "hosted-old", state: "working",
            title: "Hosted", outcome: "", turns: 0, unseen: false, updated: 100)

        let groups = PCodeWorkspace.buildProjectGroups(
            panes: [],
            projectPathForPane: { $0.projectPath },
            history: [newerRecent, visibleRecent, hostedOld, olderRecent],
            detachedHostStatuses: ["hosted-old": host],
            pinnedFolders: [],
            hiddenFolders: [hiddenPath])

        #expect(groups.map(\.path) == [hiddenPath, visiblePath])
        #expect(groups[0].hostedSessions.map(\.id) == ["hosted-old"])
        // Both newer and older recents in the hidden folder are preserved because it has a live host.
        #expect(groups[0].recentSessions.map(\.id) == ["newer-recent", "older-recent"])
        #expect(groups[1].recentSessions.map(\.id) == ["visible-recent"])
    }
}
