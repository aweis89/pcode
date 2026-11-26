import Foundation
import Testing
@testable import Ghostty

/// A pane's title and activity from `hosts/<id>.json` or the terminal's OSC reports.
struct PCodePaneStateTests {
    private static func entry(_ fields: [String: Any]) -> Data {
        let base: [String: Any] = [
            "id": "h1", "pid": 4242, "model": "m", "workspace": "/w", "protocol": 3,
            "session_id": "s1", "forked_from": "", "state": "idle", "title": "Fix the bug",
            "last_prompt": "go", "started": 1.0, "updated": 2.0, "log": "", "turns": 3,
            "outcome": "done", "attached": 1, "unseen": false, "held": "", "code": "", "python": "",
        ]
        return try! JSONSerialization.data(withJSONObject: base.merging(fields) { $1 })
    }

    private static func status(
        state: String = "idle", title: String = "Fix the bug", outcome: String = "done",
        turns: Int = 3, hostID: String = "h1", updated: Double = 2
    ) -> PCodeHostStatus {
        PCodeHostStatus(
            hostID: hostID, pid: 4242, sessionID: "s1", state: state, title: title,
            outcome: outcome, turns: turns, unseen: false, updated: updated)
    }

    // MARK: - Parsing

    @Test func parsesHostEntry() {
        let parsed = PCodeHostStatus.parse(Self.entry(["state": "working", "unseen": true]))
        #expect(parsed == PCodeHostStatus(
            hostID: "h1", pid: 4242, sessionID: "s1", state: "working", title: "Fix the bug",
            outcome: "done", turns: 3, unseen: true, updated: 2))
        #expect(parsed?.isWorking == true)
    }

    @Test func parseToleratesMissingOptionalFields() {
        let data = try! JSONSerialization.data(withJSONObject: ["id": "h2", "pid": 7, "session_id": "s2"])
        let parsed = PCodeHostStatus.parse(data)
        #expect(parsed?.state == "" && parsed?.title == "" && parsed?.turns == 0 && parsed?.unseen == false)
    }

    @Test func parseRejectsUnusableEntries() {
        #expect(PCodeHostStatus.parse(Self.entry(["session_id": ""])) == nil)
        #expect(PCodeHostStatus.parse(Self.entry(["state": "stopped"])) == nil)
        #expect(PCodeHostStatus.parse(Self.entry(["pid": 0])) == nil)
        #expect(PCodeHostStatus.parse(Self.entry(["id": "../evil"])) == nil)
        #expect(PCodeHostStatus.parse(Data("not json".utf8)) == nil)
    }

    @Test func latestEntryWinsForOneSession() {
        let older = Self.status(hostID: "a", updated: 1)
        let newer = Self.status(hostID: "b", updated: 5)
        #expect(PCodeHostStatus.bySession([newer, older])["s1"]?.hostID == "b")
        #expect(PCodeHostStatus.bySession([older, newer])["s1"]?.hostID == "b")
    }

    // MARK: - Unhosted: OSC only, as before

    @Test func unhostedPaneFollowsOSC() {
        var r = PCodePaneStateResolver()
        r.oscTitle = "osc title"
        #expect(r.title == "osc title" && r.activity == .idle)
        for activity: PCodeSessionPane.Activity in [.running, .failed, .idle, .failed] {
            r.setOSCActivity(activity)
            #expect(r.activity == activity)
        }
        #expect(r.dismissedTurns == nil)
    }

    // MARK: - Hosted: host first

    @Test func hostTitleWinsAndEmptyFallsBackToOSC() {
        var r = PCodePaneStateResolver()
        r.oscTitle = "👻"
        r.setHost(Self.status(title: "Named session"))
        #expect(r.title == "Named session")
        r.setHost(Self.status(title: ""))
        #expect(r.title == "👻")
        r.setHost(nil)
        #expect(r.title == "👻")
    }

    @Test func hostWorkingIsRunningWithoutOSC() {
        var r = PCodePaneStateResolver()
        r.setHost(Self.status(state: "working"))
        #expect(r.activity == .running)
        r.setHost(Self.status(state: "idle", outcome: "done", turns: 4))
        #expect(r.activity == .idle)
    }

    @Test func oscRunningCoversCommandsTheHostDoesNotReport() {
        // A `!command` at the prompt: the host stays idle, the terminal shows progress.
        var r = PCodePaneStateResolver()
        r.setHost(Self.status(state: "idle"))
        r.setOSCActivity(.running)
        #expect(r.activity == .running)
        r.setOSCActivity(.idle)
        #expect(r.activity == .idle)
    }

    @Test func finishedHostTurnDoesNotWaitForStaleOSCRunningTick() {
        var r = PCodePaneStateResolver()
        r.setHost(Self.status(state: "working", turns: 3))
        r.setOSCActivity(.running)
        #expect(r.activity == .running)
        // The host finishes the turn up to 1s before TabProgress's next tick clears OSC 9;4.
        r.setHost(Self.status(state: "idle", outcome: "done", turns: 4))
        #expect(r.activity == .idle)
        // A failed turn also shows `.failed` immediately and can be dismissed before the OSC tick.
        r.setHost(Self.status(state: "working", turns: 4))
        r.setOSCActivity(.running)
        r.setHost(Self.status(state: "idle", outcome: "failed", turns: 5))
        #expect(r.activity == .failed)
        r.dismissFailure()
        #expect(r.activity == .idle)
        #expect(r.dismissedTurns == 5)
    }

    @Test func hostFailedShowsUntilDismissedThenNextFailureShowsAgain() {
        var r = PCodePaneStateResolver()
        r.setHost(Self.status(state: "idle", outcome: "failed", turns: 4))
        #expect(r.activity == .failed)
        r.setOSCActivity(.failed)
        #expect(r.activity == .failed)
        // A key press in the editor clears the terminal's red bar: the mark goes too.
        r.setOSCActivity(.idle)
        #expect(r.activity == .idle)
        #expect(r.dismissedTurns == 4)
        // The host rewriting its entry for other reasons keeps it dismissed.
        r.setHost(Self.status(state: "idle", title: "renamed", outcome: "failed", turns: 4))
        #expect(r.activity == .idle)
        // The next turn runs and fails too.
        r.setHost(Self.status(state: "working", outcome: "failed", turns: 4))
        #expect(r.activity == .running)
        r.setHost(Self.status(state: "idle", outcome: "failed", turns: 5))
        #expect(r.activity == .failed)
    }

    @Test func failedToRunningIsNotADismissal() {
        var r = PCodePaneStateResolver()
        r.setHost(Self.status(outcome: "failed", turns: 4))
        r.setOSCActivity(.failed)
        r.setOSCActivity(.running)
        #expect(r.dismissedTurns == nil)
        #expect(r.activity == .running)
    }

    @Test func newHostForgetsDismissal() {
        var r = PCodePaneStateResolver()
        r.setHost(Self.status(outcome: "failed", turns: 4))
        r.setOSCActivity(.failed)
        r.setOSCActivity(.idle)
        r.setHost(Self.status(outcome: "failed", turns: 4, hostID: "h2"))
        #expect(r.dismissedTurns == nil)
        #expect(r.activity == .failed)
    }

    @Test func cancelledOutcomeIsIdle() {
        var r = PCodePaneStateResolver()
        r.setHost(Self.status(outcome: "cancelled"))
        #expect(r.activity == .idle)
    }

    @Test func hostLookupAndStopHostCoverUnsavedAndSavedHosts() throws {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("pcode-host-stop-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }

        // h1: brand-new host before turn 1 (empty session_id); h2: saved session s2.
        let h1 = #"{"id":"8f3a1b2c","pid":101,"session_id":"","state":"idle"}"#
        let h2 = #"{"id":"1234abcd","pid":202,"session_id":"s2","state":"working"}"#
        let stopped = #"{"id":"deadbeef","pid":303,"session_id":"s3","state":"stopped"}"#
        try Data(h1.utf8).write(to: dir.appendingPathComponent("8f3a1b2c.json"))
        try Data(h2.utf8).write(to: dir.appendingPathComponent("1234abcd.json"))
        try Data(stopped.utf8).write(to: dir.appendingPathComponent("deadbeef.json"))

        let entries = PCodeSessionInfo.liveHostEntries(in: dir, isAlive: { $0 == 101 || $0 == 202 || $0 == 303 })
        #expect(Set(entries.map(\.id)) == ["8f3a1b2c", "1234abcd"])

        var signalled: [Int32] = []
        let byHost = PCodeWorkspace.stopHost(
            hostID: "8f3a1b2c",
            sessionID: nil,
            in: dir,
            isAlive: { _ in true },
            signal: { signalled.append($0) })
        #expect(byHost == [101] && signalled == [101])

        signalled.removeAll()
        let bySession = PCodeWorkspace.stopHost(
            hostID: nil,
            sessionID: "s2",
            in: dir,
            isAlive: { _ in true },
            signal: { signalled.append($0) })
        #expect(bySession == [202] && signalled == [202])
    }
}
