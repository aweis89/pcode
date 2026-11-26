import Foundation
import Testing
@testable import Ghostty

/// Session state from the host's `observe_transcript` stream (`state`, `session_changed`).
struct PCodeHostStateTests {
    @Test func decodesStateDeltaOfTrackedFields() {
        let event = PCodeHostEventDecoder.decodeRPCCall(
            method: "state",
            args: [["busy": true, "prompt_state": "running", "turn_outcome": "", "queued": 2]],
            kwargs: [:])
        #expect(event == .sessionState(PCodeHostSessionDelta(busy: true, promptState: "running", turnOutcome: "")))
    }

    @Test func decodesPartialStateDelta() {
        let event = PCodeHostEventDecoder.decodeRPCCall(
            method: "state", args: [["turn_outcome": "failed"]], kwargs: [:])
        #expect(event == .sessionState(PCodeHostSessionDelta(turnOutcome: "failed")))
    }

    @Test func dropsStateDeltaOfOnlyUntrackedFields() {
        let event = PCodeHostEventDecoder.decodeRPCCall(
            method: "state", args: [["queued": 1, "notice": "hi", "notice_expires": 3.5]], kwargs: [:])
        #expect(event == nil)
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "state", args: [], kwargs: [:]) == nil)
    }

    @Test func decodesStateDeltaFromJSONWire() throws {
        // Booleans arrive as NSNumber from JSONSerialization, as on the socket.
        let line = #"{"type":"notify","method":"state","args":[{"busy":false,"prompt_state":"done"}],"kwargs":{}}"#
        let obj = try #require(try JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any])
        let event = PCodeHostEventDecoder.decodeRPCCall(
            method: obj["method"] as! String, args: obj["args"] as! [Any], kwargs: obj["kwargs"] as! [String: Any])
        #expect(event == .sessionState(PCodeHostSessionDelta(busy: false, promptState: "done")))
    }

    @Test func decodesSessionChanged() {
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "session_changed", args: [], kwargs: [:]) == .sessionChanged(""))
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "session_changed", args: ["s2"], kwargs: [:]) == .sessionChanged("s2"))
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "retained_note", args: ["context"], kwargs: [:]) == .note("context"))
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "warning", args: ["heads up"], kwargs: [:]) == .note("heads up"))
    }

    @Test func foldsDeltasAndKeepsUnchangedFields() {
        var state = PCodeHostSessionState()
        state.sessionID = "abc"
        var changed = state.apply(.sessionState(PCodeHostSessionDelta(busy: true, promptState: "running")))
        #expect(changed)
        #expect(state.busy && state.promptState == "running" && state.turnOutcome == "")
        changed = state.apply(.sessionState(PCodeHostSessionDelta(busy: false, turnOutcome: "done")))
        #expect(changed)
        #expect(state == {
            var s = PCodeHostSessionState()
            s.sessionID = "abc"
            s.promptState = "running"
            s.turnOutcome = "done"
            return s
        }())
        // Repeating a value is not a change; transcript events leave the state alone.
        for event in [PCodeHostEvent.sessionState(PCodeHostSessionDelta(busy: false)), .textDelta("hi"), .sessionChanged(""), .sessionChanged("abc")] {
            changed = state.apply(event)
            #expect(!changed)
        }
        changed = state.apply(.sessionChanged("def"))
        #expect(changed)
        #expect(state.sessionID == "def")
    }

    @Test func conversationResetClearsSessionAndOutcome() {
        var state = PCodeHostSessionState()
        state.sessionID = "abc"
        state.turnOutcome = "failed"
        state.promptState = "failed"
        let changed = state.apply(.conversationReset)
        #expect(changed)
        #expect(state.sessionID == "" && state.turnOutcome == "")
        #expect(state.promptState == "failed")
    }

    @Test func welcomeSeedsSessionAndBusy() {
        let welcome = PCodeHostSocketClient.decodeWelcome([
            "type": "ready", "id": "h1", "session_id": "s1", "title": "t", "state": "working",
            "history": [], "calls": [["state", [["turn_outcome": "done"]], [:]]],
        ])
        #expect(welcome.hostState == "working")
        var state = PCodeHostSessionState(welcome: welcome)
        #expect(state.sessionID == "s1" && state.busy && state.promptState == "running")
        for event in welcome.catchUpCalls { state.apply(event) }
        #expect(state.turnOutcome == "done")
        let idle = PCodeHostSessionState(welcome: PCodeHostSocketClient.decodeWelcome(["state": "idle"]))
        #expect(!idle.busy && idle.promptState == "")
    }
}
