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
    }

    @Test func decodesNotesWarningsAndErrors() {
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "note", args: ["loaded"], kwargs: [:]) == .note("loaded"))
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "retained_note", args: ["context"], kwargs: [:]) == .note("context"))
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "warning", args: ["heads up"], kwargs: [:]) == .warning("heads up"))
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "error", args: ["boom"], kwargs: [:]) == .error("boom"))
        // Nothing to show: no event.
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "note", args: [""], kwargs: [:]) == nil)
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "warning", args: [""], kwargs: [:]) == nil)
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "error", args: [""], kwargs: [:]) == nil)
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "error", args: [], kwargs: [:]) == nil)
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "retained_note", args: [], kwargs: [:]) == nil)
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
            "queued_prompts": ["steer me", "next turn", "wake"],
            "queued_modes": ["steering", "queue", "provider_wake"],
            "history": [], "calls": [["state", [["turn_outcome": "done"]], [:]]],
        ])
        #expect(welcome.hostState == "working")
        var state = PCodeHostSessionState(welcome: welcome)
        #expect(state.sessionID == "s1" && state.busy && state.promptState == "running")
        #expect(state.pendingPrompts.map(\.mode) == [.steering, .queued])
        #expect(state.pendingPrompts.map(\.text) == ["steer me", "next turn"])
        for event in welcome.catchUpCalls { state.apply(event) }
        #expect(state.turnOutcome == "done")
        let idle = PCodeHostSessionState(welcome: PCodeHostSocketClient.decodeWelcome(["state": "idle"]))
        #expect(!idle.busy && idle.promptState == "" && idle.pendingPrompts.isEmpty)
    }

    @Test func foldsQueuedPromptsAndClearsOnReset() {
        var state = PCodeHostSessionState()
        let deltaEvent = PCodeHostEventDecoder.decodeRPCCall(
            method: "state",
            args: [["queued_prompts": ["also fix docs", "then deploy"], "queued_modes": ["steering", "queue"]]],
            kwargs: [:])
        #expect(deltaEvent != nil)
        let applied = state.apply(deltaEvent!)
        #expect(applied)
        #expect(state.pendingPrompts.count == 2)
        #expect(state.pendingPrompts[0].mode == .steering && state.pendingPrompts[0].text == "also fix docs")
        #expect(state.pendingPrompts[1].mode == .queued && state.pendingPrompts[1].text == "then deploy")

        let reset = state.apply(.conversationReset)
        #expect(reset)
        #expect(state.pendingPrompts.isEmpty)
    }

    @Test func decodesSessionMetadataAndWelcomeFields() {
        let welcome = PCodeHostSocketClient.decodeWelcome([
            "type": "ready", "id": "h1", "session_id": "s1", "title": "t", "state": "idle",
            "model": "antigravity:gemini-4-neon",
            "effort": "medium",
            "context": "42k/200k",
            "history": [], "calls": [],
        ])
        #expect(welcome.model == "antigravity:gemini-4-neon")
        #expect(welcome.effort == "medium")
        #expect(welcome.context == "42k/200k")

        let legacyWelcome = PCodeHostSocketClient.decodeWelcome([
            "type": "ready", "id": "h1", "session_id": "s1", "title": "t", "state": "idle",
            "history": [], "calls": [],
        ])
        #expect(legacyWelcome.model == "")
        #expect(legacyWelcome.effort == "")
        #expect(legacyWelcome.context == "")

        let event = PCodeHostEventDecoder.decodeRPCCall(
            method: "session_metadata",
            args: [["model": "anthropic:claude-sonnet", "effort": "high", "context": "~12.5k/200k"]],
            kwargs: [:])
        #expect(event == .sessionMetadata(model: "anthropic:claude-sonnet", effort: "high", context: "~12.5k/200k"))
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "session_metadata", args: [], kwargs: [:]) == nil)
    }

    @Test func formatsModelProviderEffortAndContextRatio() {
        let split = PCodeSessionMetadataDisplay.splitModel("antigravity:gemini-4-neon")
        #expect(split.provider == "antigravity")
        #expect(split.name == "gemini-4-neon")

        let multiColon = PCodeSessionMetadataDisplay.splitModel("ollama:llama3:8b")
        #expect(multiColon.provider == "ollama")
        #expect(multiColon.name == "llama3:8b")

        for edge in ["preview", ":foo", "foo:"] {
            let parsed = PCodeSessionMetadataDisplay.splitModel(edge)
            #expect(parsed.provider == nil)
            #expect(parsed.name == edge)
        }

        #expect(PCodeSessionMetadataDisplay.normalizedEffort("medium") == "medium")
        #expect(PCodeSessionMetadataDisplay.normalizedEffort("n/a") == nil)
        #expect(PCodeSessionMetadataDisplay.normalizedEffort("") == nil)

        #expect(abs((PCodeSessionMetadataDisplay.contextFillRatio("50k/200k") ?? -1) - 0.25) < 0.001)
        #expect(abs((PCodeSessionMetadataDisplay.contextFillRatio("~1.5m/2m") ?? -1) - 0.75) < 0.001)
        #expect(abs((PCodeSessionMetadataDisplay.contextFillRatio("250k/200k") ?? -1) - 1.0) < 0.001)
        #expect(PCodeSessionMetadataDisplay.contextFillRatio("0/200k") == 0.0)
        #expect(PCodeSessionMetadataDisplay.contextFillRatio("42k/?") == nil)
        #expect(PCodeSessionMetadataDisplay.contextFillRatio("inf/200k") == nil)
        #expect(PCodeSessionMetadataDisplay.contextFillRatio("") == nil)
    }

    @Test func decodesProviderUsageAndMatchesAntigravityGroups() throws {
        let json = """
        {
          "provider": "antigravity",
          "windows": [{"key": "5h", "label": "5h", "used_percent": 20}],
          "groups": [
            {
              "name": "Gemini Models",
              "windows": [
                {"key": "5h", "label": "5h", "used_percent": 20},
                {"key": "weekly", "label": "wk", "used_percent": 6}
              ]
            },
            {
              "name": "Claude and GPT models",
              "windows": [
                {"key": "5h", "label": "5h", "used_percent": 14}
              ]
            }
          ]
        }
        """
        let usage = try JSONDecoder().decode(PCodeProviderUsage.self, from: Data(json.utf8))
        #expect(usage.windows(forModel: "antigravity:gemini-4-neon").map(\.usedPercent) == [20, 6])
        #expect(usage.windows(forModel: "antigravity:claude-sonnet-5-5").map(\.usedPercent) == [14])
        #expect(usage.windows(forModel: "antigravity:gpt-oss-120b").map(\.usedPercent) == [14])
        // Unrecognized Antigravity model falls back to the first non-empty group.
        #expect(usage.windows(forModel: "antigravity:other-model").map(\.usedPercent) == [20, 6])
    }

    @Test @MainActor func usageStoreCachesAndHonorsCooldowns() {
        var fetches: [String] = []
        var nextResult: PCodeProviderUsage? = PCodeProviderUsage(
            provider: "claude",
            windows: [
                PCodeUsageWindow(key: "5h", label: "5h", usedPercent: 6),
                PCodeUsageWindow(key: "weekly", label: "wk", usedPercent: 9),
            ],
            groups: nil)
        var pendingCompletion: (@Sendable (PCodeProviderUsage?) -> Void)?
        var holdNext = false
        let store = PCodeUsageStore(ttl: 180, turnRefreshCooldown: 60, failureBackoff: 120) { provider, _, done in
            fetches.append(provider)
            if holdNext {
                pendingCompletion = done
            } else {
                done(nextResult)
            }
        }
        let t0 = Date(timeIntervalSince1970: 1_000)
        // Unsupported provider does not fetch.
        store.refreshIfNeeded(forModel: "anthropic:claude-sonnet", directory: "/tmp", now: t0)
        #expect(fetches.isEmpty)

        // First fetch populates cache.
        store.refreshIfNeeded(forModel: "claude:claude-sonnet", directory: "/tmp", now: t0)
        #expect(fetches == ["claude"])
        #expect(store.windows(forModel: "claude:claude-sonnet").map(\.label) == ["5h", "wk"])

        // Within TTL (and within turnRefreshCooldown), does not refetch.
        store.refreshIfNeeded(forModel: "claude:claude-sonnet", directory: "/tmp", now: t0.addingTimeInterval(30))
        store.refreshIfNeeded(forModel: "claude:claude-sonnet", directory: "/tmp", afterTurn: true, now: t0.addingTimeInterval(30))
        #expect(fetches.count == 1)

        // After a turn past turnRefreshCooldown (60s), refetches; if it fails, enforces failureBackoff (120s).
        nextResult = nil
        store.refreshIfNeeded(forModel: "claude:claude-sonnet", directory: "/tmp", afterTurn: true, now: t0.addingTimeInterval(90))
        #expect(fetches.count == 2)
        // Must back off for 120s after the failed refresh even though prior success is >60s old.
        store.refreshIfNeeded(forModel: "claude:claude-sonnet", directory: "/tmp", afterTurn: true, now: t0.addingTimeInterval(150))
        #expect(fetches.count == 2)

        // Once failureBackoff elapses, refetches and deduplicates while in-flight.
        holdNext = true
        store.refreshIfNeeded(forModel: "claude:claude-sonnet", directory: "/tmp", now: t0.addingTimeInterval(220))
        store.refreshIfNeeded(forModel: "claude:claude-sonnet", directory: "/tmp", now: t0.addingTimeInterval(221))
        #expect(fetches.count == 3)
        pendingCompletion?(nil)
    }
}
