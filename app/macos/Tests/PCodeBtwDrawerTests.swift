import AppKit
import Foundation
import Testing
@testable import Ghostty

struct PCodeBtwDrawerTests {
    // MARK: - Aside state decoding

    private static func stateJSON(_ overrides: [String: Any] = [:]) -> [String: Any] {
        var state: [String: Any] = [
            "id": "a1", "question": "why?", "model": "gpt", "label": "Why", "effort": "high",
            "status": "answered", "answer": "Because.", "activity": "", "error": "",
            "started": 10.5, "finished": 12.0, "thread": "t1", "conversation": NSNull(),
            "base": NSNull(), "bridged": false, "replied": true, "read": true,
        ]
        for (key, value) in overrides { state[key] = value }
        return state
    }

    @Test func decodesAsideStateFields() throws {
        let state = try #require(PCodeAsideState(json: Self.stateJSON()))
        #expect(state.id == "a1")
        #expect(state.question == "why?")
        #expect(state.model == "gpt")
        #expect(state.effort == "high")
        #expect(state.status == "answered")
        #expect(state.answer == "Because.")
        #expect(state.started == 10.5)
        #expect(state.finished == 12.0)
        #expect(state.thread == "t1")
        #expect(state.read)
        #expect(!state.isRunning)
    }

    @Test func decodesNullsAsEmptyAndRejectsMissingID() throws {
        let state = try #require(PCodeAsideState(json: Self.stateJSON([
            "model": NSNull(), "finished": NSNull(), "status": "running", "started": 3,
        ])))
        #expect(state.model == "")
        #expect(state.finished == nil)
        #expect(state.started == 3)
        #expect(state.isRunning)
        #expect(PCodeAsideState(json: ["question": "q"]) == nil)
    }

    @Test func decodesNotifyLinesFromRealJSON() throws {
        let line = #"{"type":"notify","method":"asides","args":[[{"id":"a","question":"q","thread":"t","status":"running","started":1.0,"finished":null}]]}"#
        let object = try #require(JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any])
        guard case .asides(let states) = try #require(PCodeAsideMessage.decode(object)) else {
            Issue.record("not asides")
            return
        }
        #expect(states.map(\.id) == ["a"])
        #expect(PCodeAsideMessage.decode(["type": "notify", "method": "host_closed"]) == .hostClosed)
        #expect(PCodeAsideMessage.decode(["type": "ready"]) == nil)
        #expect(PCodeAsideMessage.decode(["type": "notify", "method": "other", "args": []]) == nil)
    }

    @Test func storeUpsertsByIDAndFiltersThreads() throws {
        var store = PCodeAsideStore()
        let first = try #require(PCodeAsideState(json: Self.stateJSON(["id": "a", "thread": "t1"])))
        let other = try #require(PCodeAsideState(json: Self.stateJSON(["id": "b", "thread": "t2"])))
        store.apply(.asides([first, other]))
        var changed = first
        changed.answer = "More."
        store.apply(.changed(changed))
        let third = try #require(PCodeAsideState(json: Self.stateJSON(["id": "c", "thread": "t1"])))
        store.apply(.changed(third))
        #expect(store.thread("t1").map(\.id) == ["a", "c"])
        #expect(store.thread("t1").first?.answer == "More.")
        #expect(store.threadCount == 2)
    }

    // MARK: - Aside → transcript items

    private static func aside(
        _ id: String, status: String = "answered", answer: String = "Because.", error: String = "",
        activity: String = ""
    ) -> PCodeAsideState {
        var aside = PCodeAsideState(id: id, question: "Question \(id)?")
        aside.model = "gpt"
        aside.effort = "high"
        aside.status = status
        aside.answer = answer
        aside.error = error
        aside.activity = activity
        aside.started = 100
        aside.finished = status == "running" ? nil : 104
        aside.thread = "t"
        return aside
    }

    @Test func answeredAsideIsSidePromptThenProse() {
        let items = PCodeBtwThread.items(for: [Self.aside("a"), Self.aside("b")], docked: false)
        #expect(items.map(\.id) == [
            "btw-a-prompt", "btw-a-md-0-0", "btw-a-md-0-actions",
            "btw-b-prompt", "btw-b-md-0-0", "btw-b-md-0-actions",
        ])
        guard case .prompt(let label, let text, let isSide) = items[0].payload else {
            Issue.record("not a prompt")
            return
        }
        #expect(label == "/btw · on gpt at high effort")
        #expect(text == "Question a?")
        #expect(isSide)
        // Only a thread's first question says where it runs.
        if case .prompt(let followUp, _, _) = items[3].payload { #expect(followUp == "/btw") }
        guard case .prose(let markdown, _, _) = items[1].payload else {
            Issue.record("not prose")
            return
        }
        #expect(markdown == "Because.")
    }

    @Test func runningAsideShowsActivityWithSpinnerUnlessDocked() throws {
        let running = Self.aside("a", status: "running", answer: "Part", activity: "Searching…")
        let boot = Date(timeIntervalSinceReferenceDate: 870)
        let items = PCodeBtwThread.items(for: [running], docked: false, bootDate: boot)
        #expect(items.map(\.id) == ["btw-a-prompt", "btw-a-md-0-0", "btw-a-status"])
        guard case .tool(let call, _) = try #require(items.last).payload else {
            Issue.record("not a tool row")
            return
        }
        #expect(call.isRunning)
        #expect(call.name == "Searching…")
        #expect(call.startedAt == Date(timeIntervalSinceReferenceDate: 970))
        // The same input renders the same row: no rewrite on every update.
        let again = { PCodeBtwThread.items(for: [running], docked: false) }
        #expect(PCodeTranscriptItemSync.plan(from: again(), to: again()).isEmpty)
        let docked = PCodeBtwThread.items(for: [running], docked: true)
        #expect(docked.map(\.id) == ["btw-a-prompt", "btw-a-md-0-0"])
    }

    @Test func failedAndEmptyAnswersSayWhy() throws {
        let failed = Self.aside("a", status: "failed", answer: "", error: "rate limited")
        let items = PCodeBtwThread.items(for: [failed], docked: true)
        #expect(items.map(\.id) == ["btw-a-prompt", "btw-a-status"])
        guard case .tool(let call, _) = try #require(items.last).payload else {
            Issue.record("not a tool row")
            return
        }
        #expect(call.failed)
        #expect(call.name == "failed")
        #expect(call.detail == "rate limited")
        #expect(call.elapsedSeconds == 4)

        let cancelled = PCodeBtwThread.items(for: [Self.aside("b", status: "cancelled", answer: "")], docked: false)
        guard case .tool(let none, _) = try #require(cancelled.last).payload else {
            Issue.record("not a tool row")
            return
        }
        #expect(!none.failed)
        #expect(none.detail == "no answer")
    }

    // MARK: - Incremental sync

    @Test func syncUpsertsChangedAndNewRowsAndRemovesGoneOnes() {
        let running = Self.aside("a", status: "running", answer: "Par", activity: "")
        var settled = running
        settled.status = "answered"
        settled.answer = "Partial answer."
        settled.finished = 104
        let old = PCodeBtwThread.items(for: [running], docked: false)
        let new = PCodeBtwThread.items(for: [settled, Self.aside("b")], docked: false)
        let steps = PCodeTranscriptItemSync.plan(from: old, to: new)
        #expect(steps == [
            .remove("btw-a-status"),
            .upsert("btw-a-md-0-0", at: 1),
            .upsert("btw-a-md-0-actions", at: 2),
            .upsert("btw-b-prompt", at: 3),
            .upsert("btw-b-md-0-0", at: 4),
            .upsert("btw-b-md-0-actions", at: 5),
        ])
        #expect(PCodeTranscriptItemSync.plan(from: new, to: new).isEmpty)
        #expect(PCodeTranscriptItemSync.plan(from: [], to: new) == [.reset])
        // Another thread shares no rows: start over.
        #expect(PCodeTranscriptItemSync.plan(from: new, to: PCodeBtwThread.items(for: [Self.aside("z")], docked: false)) == [.reset])
    }

    // MARK: - Hints

    @Test func hintsFollowTheBtwDrawerRules() {
        let undocked = PCodeBtwHints.hints(docked: false, others: true, nextThreadKey: "⌃P \\")
        #expect(undocked.map(\.key) == ["↩", "⌃P \\", "esc"])
        #expect(undocked.map(\.label) == ["Reply in editor", "Next thread", "Close"])
        #expect(undocked.map(\.keep) == [true, false, true])
        let docked = PCodeBtwHints.hints(docked: true, others: false, nextThreadKey: "⌃\\")
        #expect(docked.map(\.key) == ["esc"])
    }

    // MARK: - Observer against a socket

    @Test func observerStreamsAsidesThenNotesTheHostStopping() async throws {
        let path = "/tmp/pcode-btw-\(UUID().uuidString.prefix(8)).sock"
        let server = try FakeHost(path: path, lines: [
            #"{"type":"ready","version":2}"#,
            #"{"type":"notify","method":"asides","args":[[{"id":"a","question":"q","thread":"t"}]]}"#,
            #"{"type":"notify","method":"aside_changed","args":[{"id":"a","question":"q","thread":"t","answer":"hi"}]}"#,
        ])
        defer { server.close() }
        let events = EventBox()
        let observer = PCodeAsideObserver(host: "deadbeef", socketPath: path) { events.append($0) }
        observer.start()
        defer { observer.stop() }
        for _ in 0..<200 where !events.all.contains(.note(PCodeAsideObserver.stoppedMessage)) {
            try await Task.sleep(nanoseconds: 20_000_000)
        }
        let all = events.all
        #expect(server.hello.contains(#""type":"observe""#))
        #expect(server.hello.contains(#""version":2"#))
        var store = PCodeAsideStore()
        for case .message(let message) in all { store.apply(message) }
        #expect(store.thread("t").map(\.answer) == ["hi"])
        #expect(all.last == .note(PCodeAsideObserver.stoppedMessage))
    }

    @Test func observerNotesAMissingHost() async throws {
        let events = EventBox()
        let observer = PCodeAsideObserver(host: "deadbeef", socketPath: "/tmp/pcode-btw-missing.sock") {
            events.append($0)
        }
        observer.start()
        defer { observer.stop() }
        for _ in 0..<100 where events.all.isEmpty {
            try await Task.sleep(nanoseconds: 20_000_000)
        }
        #expect(events.all.first == .note("Session deadbeef is not running."))
    }

    @MainActor
    @Test func drawerSwallowsQToReturnFocusWithoutForwardingToComposer() throws {
        let typo = PCodeTypographySnapshot(style: .sansAndMono, size: 13, weight: .regular, codeFamilyAvailable: false)
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let doc = PCodeTranscriptDocumentView(typography: typo, theme: theme)
        doc.frame = NSRect(x: 0, y: 0, width: 360, height: 240)
        doc.setItems(PCodeBtwThread.items(for: [Self.aside("a")], docked: true), pinToBottom: true)

        final class SpyView: NSView {
            var forwarded: [String] = []
            override var acceptsFirstResponder: Bool { true }
            override func keyDown(with event: NSEvent) {
                forwarded.append(event.charactersIgnoringModifiers ?? "")
            }
        }
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 360, height: 240),
            styleMask: [.titled], backing: .buffered, defer: false)
        let spy = SpyView(frame: NSRect(x: 0, y: 0, width: 10, height: 10))
        let root = NSView(frame: NSRect(x: 0, y: 0, width: 360, height: 240))
        root.addSubview(doc)
        root.addSubview(spy)
        window.contentView = root

        var returnedFocus = 0
        doc.swallowsQToReturnFocus = true
        doc.onReturnFocusToComposer = {
            returnedFocus += 1
            window.makeFirstResponder(spy)
        }

        _ = window.makeFirstResponder(doc)
        doc.selectAll(nil)
        #expect(doc.selection != nil)
        let qEvent = try #require(NSEvent.keyEvent(
            with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0,
            windowNumber: window.windowNumber, context: nil,
            characters: "q", charactersIgnoringModifiers: "q", isARepeat: false, keyCode: 12))
        doc.keyDown(with: qEvent)
        #expect(returnedFocus == 1)
        #expect(doc.selection == nil)
        #expect(spy.forwarded.isEmpty)

        // Any other plain character still returns focus and forwards to the composer.
        _ = window.makeFirstResponder(doc)
        let aEvent = try #require(NSEvent.keyEvent(
            with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0,
            windowNumber: window.windowNumber, context: nil,
            characters: "a", charactersIgnoringModifiers: "a", isARepeat: false, keyCode: 0))
        doc.keyDown(with: aEvent)
        #expect(returnedFocus == 2)
        #expect(spy.forwarded == ["a"])
    }
}

/// Collects events delivered on the main queue.
private final class EventBox: @unchecked Sendable {
    private let lock = NSLock()
    private var events: [PCodeAsideObserver.Event] = []
    func append(_ event: PCodeAsideObserver.Event) {
        lock.lock()
        events.append(event)
        lock.unlock()
    }
    var all: [PCodeAsideObserver.Event] {
        lock.lock()
        defer { lock.unlock() }
        return events
    }
}

/// A one-connection Unix socket server: reads the hello line, writes `lines`, closes.
private final class FakeHost: @unchecked Sendable {
    private let fd: Int32
    private let path: String
    private let lock = NSLock()
    private var helloLine = ""
    var hello: String {
        lock.lock()
        defer { lock.unlock() }
        return helloLine
    }

    init(path: String, lines: [String]) throws {
        self.path = path
        unlink(path)
        fd = socket(AF_UNIX, SOCK_STREAM, 0)
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in
            raw.copyBytes(from: Array(path.utf8))
            raw[path.utf8.count] = 0
        }
        let bound = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard bound == 0, listen(fd, 1) == 0 else { throw POSIXError(.EADDRINUSE) }
        let listener = fd
        Thread { [self] in
            let client = accept(listener, nil, nil)
            guard client >= 0 else { return }
            var buffer = [UInt8](repeating: 0, count: 4096)
            let count = read(client, &buffer, buffer.count)
            lock.lock()
            helloLine = count > 0 ? String(decoding: buffer[0..<count], as: UTF8.self) : ""
            lock.unlock()
            let payload = lines.map { $0 + "\n" }.joined()
            _ = payload.withCString { write(client, $0, strlen($0)) }
            Darwin.close(client)
        }.start()
    }

    func close() {
        Darwin.close(fd)
        unlink(path)
    }
}
