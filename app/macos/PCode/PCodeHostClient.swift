import Darwin
import Foundation

/// One step in a Harness plan (`PlanUpdated` / `PlanPreview`).
struct PCodePlanItem: Equatable {
    let content: String
    let status: String
}

/// Structured diff payload (`EditPreview` morphs into `EditCompleted` under the same `call_id`).
struct PCodeDiffPayload: Equatable {
    let callID: String
    let path: String
    let operation: String
    let patch: String
    let added: Int
    let removed: Int
    let isPreview: Bool
    let truncated: Bool
    let depth: Int
}

/// Unified domain event decoded from both saved `transcript.jsonl` (flat fields)
/// and live `pcode.host` Unix socket RPC notifications (nested `fields` under `$event`).
enum PCodeHostEvent: Equatable {
    case turnStarted(prompt: String, isSide: Bool, runID: String)
    case textDelta(String)
    case message(markdown: String)
    case thinkingDelta(String)
    case thinking(text: String)
    case toolStarted(
        callID: String,
        parentCallID: String,
        name: String,
        detail: String,
        command: String,
        purpose: String)
    case toolSummary(
        callID: String,
        parentCallID: String,
        name: String,
        detail: String,
        command: String,
        failed: Bool,
        elapsedSeconds: Double?,
        error: String,
        result: String?,
        isBackgroundJob: Bool)
    case commandOutput(callID: String, command: String, output: String)
    case editPreview(callID: String, path: String, text: String, kind: String)
    case editCompleted(
        callID: String,
        parentCallID: String,
        path: String,
        operation: String,
        patch: String,
        added: Int,
        removed: Int,
        truncated: Bool)
    case planUpdated(items: [PCodePlanItem])
    case planPreview(items: [PCodePlanItem]?)
    case turnEnded(status: PCodePeekTurn.Status)
    case note(String)
    case error(String)
    case conversationReset
}

/// Dual-format event decoder for `transcript.jsonl` and live `pcode.host` RPC notifications.
enum PCodeHostEventDecoder {
    /// Decode one line from `transcript.jsonl` (fields spread beside `"kind"`).
    static func decodeJournalRecord(_ record: [String: Any]) -> PCodeHostEvent? {
        guard let kind = record["kind"] as? String else { return nil }
        switch kind {
        case "turn_started":
            guard let prompt = record["prompt"] as? String else { return nil }
            let isSide = (record["side"] as? Bool) == true
            let runID = (record["run_id"] as? String) ?? ""
            return .turnStarted(prompt: prompt, isSide: isSide, runID: runID)
        case "turn_completed":
            return .turnEnded(status: .complete)
        case "turn_cancelled":
            return .turnEnded(status: .cancelled)
        case "turn_failed":
            return .turnEnded(status: .failed)
        default:
            return decodeRuntimeEvent(kind: kind, fields: record)
        }
    }

    /// Decode one live RPC view call (`[method, args, kwargs]` or `notify` message).
    static func decodeRPCCall(method: String, args: [Any], kwargs: [String: Any]) -> PCodeHostEvent? {
        switch method {
        case "before_turn":
            let prompt = (args.first as? String) ?? (kwargs["prompt"] as? String) ?? ""
            let isSide = (kwargs["side"] as? Bool) == true
            return .turnStarted(prompt: prompt, isSide: isSide, runID: "")
        case "after_turn":
            let raw = (args.first as? String) ?? (kwargs["outcome"] as? String) ?? "complete"
            let status: PCodePeekTurn.Status
            switch raw {
            case "cancelled": status = .cancelled
            case "failed", "error": status = .failed
            default: status = .complete
            }
            return .turnEnded(status: status)
        case "turn_event":
            guard let first = args.first as? [String: Any],
                  let wrapped = first["$event"] as? [String: Any],
                  let kind = wrapped["kind"] as? String,
                  let fields = wrapped["fields"] as? [String: Any]
            else { return nil }
            return decodeRuntimeEvent(kind: kind, fields: fields)
        case "note":
            if let text = args.first as? String, !text.isEmpty {
                return .note(text)
            }
            return nil
        case "error":
            if let text = args.first as? String, !text.isEmpty {
                return .error(text)
            }
            return nil
        case "conversation_reset":
            return .conversationReset
        default:
            // Terminal-only or unknown notifications are ignored safely.
            return nil
        }
    }

    static func decodeRuntimeEvent(kind: String, fields: [String: Any]) -> PCodeHostEvent? {
        switch kind {
        case "TextDelta":
            guard let text = fields["text"] as? String else { return nil }
            return .textDelta(text)
        case "Message":
            guard let markdown = fields["markdown"] as? String else { return nil }
            return .message(markdown: markdown)
        case "ThinkingDelta":
            guard let text = fields["text"] as? String else { return nil }
            return .thinkingDelta(text)
        case "Thinking":
            guard let text = fields["text"] as? String else { return nil }
            return .thinking(text: text)
        case "ToolStarted":
            let callID = (fields["call_id"] as? String) ?? ""
            guard !callID.isEmpty else { return nil }
            return .toolStarted(
                callID: callID,
                parentCallID: (fields["parent_call_id"] as? String) ?? "",
                name: (fields["name"] as? String) ?? "",
                detail: (fields["detail"] as? String) ?? "",
                command: (fields["command"] as? String) ?? "",
                purpose: (fields["purpose"] as? String) ?? "")
        case "ToolSummary", "JobFinished":
            let callID = (fields["call_id"] as? String) ?? ""
            return .toolSummary(
                callID: callID,
                parentCallID: (fields["parent_call_id"] as? String) ?? "",
                name: (fields["name"] as? String) ?? "",
                detail: (fields["detail"] as? String) ?? "",
                command: (fields["command"] as? String) ?? "",
                failed: (fields["failed"] as? Bool) == true,
                elapsedSeconds: fields["elapsed_seconds"] as? Double,
                error: (fields["error"] as? String) ?? "",
                result: fields["result"] as? String,
                isBackgroundJob: kind == "JobFinished" || (fields["execution"] as? String) == "background")
        case "CommandOutput":
            let callID = (fields["call_id"] as? String) ?? ""
            guard !callID.isEmpty else { return nil }
            return .commandOutput(
                callID: callID,
                command: (fields["command"] as? String) ?? "",
                output: (fields["output"] as? String) ?? "")
        case "EditPreview":
            let callID = (fields["call_id"] as? String) ?? ""
            guard !callID.isEmpty else { return nil }
            return .editPreview(
                callID: callID,
                path: (fields["path"] as? String) ?? "",
                text: (fields["text"] as? String) ?? "",
                kind: (fields["kind"] as? String) ?? "edit")
        case "EditCompleted":
            let callID = (fields["call_id"] as? String) ?? ""
            return .editCompleted(
                callID: callID,
                parentCallID: (fields["parent_call_id"] as? String) ?? "",
                path: (fields["path"] as? String) ?? "",
                operation: (fields["operation"] as? String) ?? "edit",
                patch: (fields["patch"] as? String) ?? "",
                added: (fields["added"] as? Int) ?? 0,
                removed: (fields["removed"] as? Int) ?? 0,
                truncated: (fields["truncated"] as? Bool) == true)
        case "PlanUpdated":
            let rawItems = (fields["items"] as? [[String: Any]]) ?? []
            return .planUpdated(items: parsePlanItems(rawItems))
        case "PlanPreview":
            if let rawItems = fields["items"] as? [[String: Any]] {
                return .planPreview(items: parsePlanItems(rawItems))
            }
            return .planPreview(items: nil)
        case "ChildText", "ChildPlan", "WorkerStatus", "RunStatus", "CacheBust":
            // Display-only worker/status events do not create permanent transcript items.
            return nil
        default:
            return nil
        }
    }

    private static func parsePlanItems(_ raw: [[String: Any]]) -> [PCodePlanItem] {
        raw.compactMap { dict in
            let content = (dict["content"] as? String) ?? (dict["title"] as? String) ?? ""
            guard !content.isEmpty else { return nil }
            let status = (dict["status"] as? String) ?? "pending"
            return PCodePlanItem(content: content, status: status)
        }
    }
}

/// Stateful event reducer that maps `PCodeHostEvent`s into ID-addressed mutations on
/// `PCodeTranscriptDocumentView`:
/// - `call_id` upserts (`ToolStarted` -> `CommandOutput`/`EditPreview` -> `ToolSummary`/`JobFinished`/`EditCompleted`)
/// - `parent_call_id` flattened nesting (`depth = 1`, with orphan buffering when child arrives before parent)
/// - `ThinkingDelta` / `Thinking` collapsed-by-default thinking cards
/// - Single upserted `PlanCardView` per turn
final class PCodeTranscriptEventReducer {
    static let maxCommandOutputLines = 40

    private weak var documentView: PCodeTranscriptDocumentView?
    private var messageCounter = 0
    private var toolCounter = 0
    private var thinkingCounter = 0
    private var activeThinkingID: String?
    private var activeThinkingText: String = ""
    private var activePlanItemID: String?
    private var toolCallsByID: [String: PCodePeekToolCall] = [:]
    private var toolDepthByID: [String: Int] = [:]
    private var pendingChildrenByParent: [String: [PCodeTranscriptItem]] = [:]

    init(documentView: PCodeTranscriptDocumentView) {
        self.documentView = documentView
    }

    func reset() {
        messageCounter = 0
        toolCounter = 0
        thinkingCounter = 0
        activeThinkingID = nil
        activeThinkingText = ""
        activePlanItemID = nil
        toolCallsByID.removeAll()
        toolDepthByID.removeAll()
        pendingChildrenByParent.removeAll()
    }

    func apply(_ event: PCodeHostEvent, reveal: Bool) {
        guard let doc = documentView else { return }
        switch event {
        case .conversationReset:
            reset()
            doc.setItems([], pinToBottom: true)

        case .turnStarted(let prompt, let isSide, _):
            activeThinkingID = nil
            activeThinkingText = ""
            activePlanItemID = nil
            doc.beginStreamingTurn(prompt: prompt, isSide: isSide, forcePin: false)

        case .textDelta(let delta):
            if activeThinkingID != nil {
                activeThinkingID = nil
                activeThinkingText = ""
            }
            doc.appendStreamingDelta(delta)

        case .message(let markdown):
            activeThinkingID = nil
            activeThinkingText = ""
            messageCounter += 1
            doc.beginStreamingMessage(id: "host-msg-\(messageCounter)")
            doc.appendStreamingDelta(markdown)
            doc.finishStreamingMessage()

        case .thinkingDelta(let delta):
            guard !delta.isEmpty else { return }
            if activeThinkingID == nil {
                thinkingCounter += 1
                activeThinkingID = "host-thinking-\(thinkingCounter)"
                activeThinkingText = ""
            }
            activeThinkingText.append(delta)
            let item = PCodeTranscriptItem(
                id: activeThinkingID!,
                payload: .thinking(text: activeThinkingText, isExpanded: false),
                spacingAfter: 6)
            doc.upsertItem(item, reveal: reveal)

        case .thinking(let text):
            let id: String
            if let existing = activeThinkingID {
                id = existing
            } else {
                thinkingCounter += 1
                id = "host-thinking-\(thinkingCounter)"
            }
            activeThinkingID = nil
            activeThinkingText = ""
            guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
            let item = PCodeTranscriptItem(
                id: id,
                payload: .thinking(text: text, isExpanded: false),
                spacingAfter: 6)
            doc.upsertItem(item, reveal: reveal)

        case .toolStarted(let callID, let parentCallID, let name, let detail, let command, _):
            doc.finishStreamingMessage()
            toolCounter += 1
            let call = PCodePeekToolCall(
                id: toolCounter,
                name: name,
                detail: detail,
                failed: false,
                elapsedSeconds: nil,
                command: command)
            toolCallsByID[callID] = call
            let depth = parentCallID.isEmpty ? 0 : 1
            toolDepthByID[callID] = depth
            let itemID = "call-\(callID)"
            let item = PCodeTranscriptItem(
                id: itemID,
                payload: .tool(call, isExpanded: false),
                spacingAfter: 5,
                depth: depth)
            insertWithParentNesting(item: item, parentCallID: parentCallID, reveal: reveal, in: doc)

            // Flush any orphan children that arrived before this parent in catch-up.
            if let orphans = pendingChildrenByParent.removeValue(forKey: callID) {
                for orphan in orphans {
                    insertWithParentNesting(item: orphan, parentCallID: callID, reveal: reveal, in: doc)
                }
            }

        case .toolSummary(let callID, let parentCallID, let name, let detail, let command, let failed, let elapsed, let error, let result, _):
            doc.finishStreamingMessage()
            let key = callID.isEmpty ? "anon-\(toolCounter + 1)" : callID
            let existing = toolCallsByID[key]
            toolCounter += 1
            let mergedDetail: String
            if !detail.isEmpty {
                mergedDetail = detail
            } else if !error.isEmpty {
                mergedDetail = error
            } else {
                mergedDetail = existing?.detail ?? ""
            }
            let call = PCodePeekToolCall(
                id: existing?.id ?? toolCounter,
                name: name.isEmpty ? (existing?.name ?? "tool") : name,
                detail: mergedDetail,
                failed: failed,
                elapsedSeconds: elapsed,
                command: command.isEmpty ? (existing?.command ?? "") : command,
                output: result ?? existing?.output ?? "")
            toolCallsByID[key] = call
            let depth = toolDepthByID[key] ?? (parentCallID.isEmpty ? 0 : 1)
            let item = PCodeTranscriptItem(
                id: "call-\(key)",
                payload: .tool(call, isExpanded: false),
                spacingAfter: 5,
                depth: depth)
            insertWithParentNesting(item: item, parentCallID: parentCallID, reveal: reveal, in: doc)

        case .commandOutput(let callID, let command, let output):
            // Cap live command output snapshots to the last N lines so they never pump the conveyor.
            let capped = Self.capTailLines(output, maxLines: Self.maxCommandOutputLines)
            let existing = toolCallsByID[callID]
            toolCounter += 1
            let call = PCodePeekToolCall(
                id: existing?.id ?? toolCounter,
                name: existing?.name ?? "shell",
                detail: existing?.detail ?? command,
                failed: existing?.failed ?? false,
                elapsedSeconds: existing?.elapsedSeconds,
                command: command.isEmpty ? (existing?.command ?? "") : command,
                output: capped)
            toolCallsByID[callID] = call
            let depth = toolDepthByID[callID] ?? 0
            let item = PCodeTranscriptItem(
                id: "call-\(callID)",
                payload: .tool(call, isExpanded: false),
                spacingAfter: 5,
                depth: depth)
            doc.upsertItem(item, reveal: false)

        case .editPreview(let callID, let path, let text, let kind):
            guard kind == "edit", !path.isEmpty else { return }
            let depth = toolDepthByID[callID] ?? 0
            let payload = PCodeDiffPayload(
                callID: callID,
                path: path,
                operation: "edit",
                patch: text,
                added: 0,
                removed: 0,
                isPreview: true,
                truncated: false,
                depth: depth)
            let item = PCodeTranscriptItem(
                id: "edit-\(callID)",
                payload: .diff(payload),
                spacingAfter: 8,
                depth: depth)
            doc.upsertItem(item, reveal: reveal)

        case .editCompleted(let callID, let parentCallID, let path, let operation, let patch, let added, let removed, let truncated):
            doc.finishStreamingMessage()
            let key = callID.isEmpty ? "edit-\(toolCounter + 1)" : callID
            let depth = parentCallID.isEmpty ? (toolDepthByID[key] ?? 0) : 1
            let payload = PCodeDiffPayload(
                callID: key,
                path: path,
                operation: operation,
                patch: patch,
                added: added,
                removed: removed,
                isPreview: false,
                truncated: truncated,
                depth: depth)
            let item = PCodeTranscriptItem(
                id: "edit-\(key)",
                payload: .diff(payload),
                spacingAfter: 8,
                depth: depth)
            insertWithParentNesting(item: item, parentCallID: parentCallID, reveal: reveal, in: doc)

        case .planUpdated(let items):
            guard !items.isEmpty else {
                if let id = activePlanItemID {
                    doc.removeItem(id: id)
                    activePlanItemID = nil
                }
                return
            }
            let id = activePlanItemID ?? "host-plan-\(doc.items.count)"
            activePlanItemID = id
            let item = PCodeTranscriptItem(id: id, payload: .plan(items), spacingAfter: 8)
            doc.upsertItem(item, reveal: reveal)

        case .planPreview(let items):
            if let items, !items.isEmpty {
                let id = activePlanItemID ?? "host-plan-\(doc.items.count)"
                activePlanItemID = id
                let item = PCodeTranscriptItem(id: id, payload: .plan(items), spacingAfter: 8)
                doc.upsertItem(item, reveal: reveal)
            } else if let id = activePlanItemID {
                doc.removeItem(id: id)
                activePlanItemID = nil
            }

        case .turnEnded(let status):
            activeThinkingID = nil
            activeThinkingText = ""
            doc.finishStreamingTurn(status: status)

        case .note(let text), .error(let text):
            let item = PCodeTranscriptItem(
                id: "host-note-\(doc.items.count)",
                payload: .prose(markdown: text, referenceDefinitions: "", copyMarkdown: nil),
                spacingAfter: 6)
            doc.upsertItem(item, reveal: reveal)
        }
    }

    private func insertWithParentNesting(
        item: PCodeTranscriptItem,
        parentCallID: String,
        reveal: Bool,
        in doc: PCodeTranscriptDocumentView
    ) {
        guard !parentCallID.isEmpty else {
            doc.upsertItem(item, reveal: reveal)
            return
        }
        let parentItemID = "call-\(parentCallID)"
        if let parentIdx = doc.indexOfItem(id: parentItemID) {
            // Insert right after the parent and any already-nested siblings.
            var insertIdx = parentIdx + 1
            while insertIdx < doc.items.count && doc.items[insertIdx].depth > 0 {
                insertIdx += 1
            }
            doc.upsertItem(item, insertAt: insertIdx, reveal: reveal)
        } else {
            // Orphan child arrived before parent in catch-up: buffer until parent arrives.
            pendingChildrenByParent[parentCallID, default: []].append(item)
        }
    }

    static func capTailLines(_ text: String, maxLines: Int) -> String {
        guard maxLines > 0 else { return "" }
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false)
        guard lines.count > maxLines else { return text }
        return "… (\(lines.count - maxLines) earlier lines omitted)\n" + lines.suffix(maxLines).joined(separator: "\n")
    }
}

/// Read-only Unix-socket client for `pcode.host` (`observe_transcript` handshake v1).
///
/// Reuses `PCodePicker`'s socket safety pattern: `AF_UNIX`, `SO_NOSIGPIPE`, `getpeereid`
/// same-user check, and a dedicated background reader thread with a growable buffer so
/// multi-megabyte welcome snapshots never block the main thread.
final class PCodeHostSocketClient {
    static let observerVersion = 1

    struct Welcome {
        let hostID: String
        let sessionID: String
        let title: String
        let pacedScrollback: PCodeStreamingPaceMode
        let history: [PCodeHostEvent]
        let catchUpCalls: [PCodeHostEvent]
    }

    private let lock = NSLock()
    private var fd: Int32 = -1
    private var closed = false
    private var readThread: Thread?

    deinit {
        close()
    }

    /// Connect to a running host's `<id>.sock` and stream welcome + live events on the main queue.
    func connect(
        socketPath: String,
        onReady: @escaping (Welcome) -> Void,
        onEvent: @escaping (PCodeHostEvent) -> Void,
        onError: @escaping (String) -> Void,
        onClosed: @escaping () -> Void
    ) {
        close()
        lock.lock()
        closed = false
        lock.unlock()

        let thread = Thread { [weak self] in
            self?.runReaderLoop(
                socketPath: socketPath,
                onReady: onReady,
                onEvent: onEvent,
                onError: onError,
                onClosed: onClosed)
        }
        thread.name = "pcode.host.observer"
        thread.qualityOfService = .userInitiated
        lock.lock()
        readThread = thread
        lock.unlock()
        thread.start()
    }

    func close() {
        lock.lock()
        closed = true
        let currentFD = fd
        fd = -1
        lock.unlock()
        if currentFD >= 0 {
            Darwin.shutdown(currentFD, SHUT_RDWR)
            Darwin.close(currentFD)
        }
    }

    private func runReaderLoop(
        socketPath: String,
        onReady: @escaping (Welcome) -> Void,
        onEvent: @escaping (PCodeHostEvent) -> Void,
        onError: @escaping (String) -> Void,
        onClosed: @escaping () -> Void
    ) {
        let sock = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard sock >= 0 else {
            DispatchQueue.main.async { onError("Failed to create Unix socket.") }
            return
        }

        var noSigPipe: Int32 = 1
        setsockopt(sock, SOL_SOCKET, SO_NOSIGPIPE, &noSigPipe, socklen_t(MemoryLayout<Int32>.size))

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(socketPath.utf8)
        let maxLen = MemoryLayout.size(ofValue: addr.sun_path) - 1
        guard pathBytes.count <= maxLen else {
            Darwin.close(sock)
            DispatchQueue.main.async { onError("Host socket path exceeds Unix socket limit.") }
            return
        }
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in
            for i in 0..<pathBytes.count {
                raw[i] = pathBytes[i]
            }
            raw[pathBytes.count] = 0
        }

        let connected = withUnsafePointer(to: &addr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockPtr in
                Darwin.connect(sock, sockPtr, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard connected == 0 else {
            Darwin.close(sock)
            DispatchQueue.main.async { onError("Could not connect to session host socket.") }
            return
        }

        // Same-user peer check (matching PCodePicker).
        var euid: uid_t = 0
        var egid: gid_t = 0
        guard Darwin.getpeereid(sock, &euid, &egid) == 0, euid == Darwin.geteuid() else {
            Darwin.close(sock)
            DispatchQueue.main.async { onError("Refusing to connect to host owned by another user.") }
            return
        }

        lock.lock()
        if closed {
            lock.unlock()
            Darwin.close(sock)
            return
        }
        fd = sock
        lock.unlock()

        // Send `observe_transcript` handshake.
        let helloLine = "{\"type\":\"observe_transcript\",\"version\":\(Self.observerVersion)}\n"
        if !writeAll(sock, Data(helloLine.utf8)) {
            close()
            DispatchQueue.main.async { onError("Failed to send observe_transcript handshake.") }
            return
        }

        // Read newline-delimited JSON off the main thread with a growable buffer.
        var buffer = Data()
        buffer.reserveCapacity(64 * 1024)
        var chunk = [UInt8](repeating: 0, count: 64 * 1024)
        var receivedHandshake = false

        while true {
            let n = Darwin.read(sock, &chunk, chunk.count)
            if n <= 0 { break }
            buffer.append(contentsOf: chunk[0..<n])

            while let newlineIdx = buffer.firstIndex(of: 0x0A) {
                let lineData = buffer[buffer.startIndex..<newlineIdx]
                buffer.removeSubrange(buffer.startIndex...newlineIdx)
                guard !lineData.isEmpty,
                      let obj = try? JSONSerialization.jsonObject(with: lineData) as? [String: Any]
                else { continue }

                if !receivedHandshake {
                    receivedHandshake = true
                    let type = (obj["type"] as? String) ?? ""
                    if type == "error" {
                        let msg = (obj["message"] as? String) ?? "Host rejected transcript observer."
                        close()
                        DispatchQueue.main.async { onError(msg) }
                        return
                    }
                    guard type == "ready" else {
                        close()
                        DispatchQueue.main.async { onError("Unexpected host handshake response.") }
                        return
                    }
                    let welcome = Self.decodeWelcome(obj)
                    DispatchQueue.main.async { onReady(welcome) }
                    continue
                }

                let msgType = (obj["type"] as? String) ?? ""
                if msgType == "notify" {
                    let method = (obj["method"] as? String) ?? ""
                    let args = (obj["args"] as? [Any]) ?? []
                    let kwargs = (obj["kwargs"] as? [String: Any]) ?? [:]
                    if let event = PCodeHostEventDecoder.decodeRPCCall(method: method, args: args, kwargs: kwargs) {
                        DispatchQueue.main.async { onEvent(event) }
                    }
                } else if msgType == "request", let reqID = obj["id"] as? Int {
                    // Read-only observer: decline any unexpected host request cleanly so host never waits.
                    let reply = "{\"type\":\"reply\",\"id\":\(reqID),\"error\":{\"type\":\"PermissionError\",\"message\":\"Transcript observer is read-only.\"}}\n"
                    _ = writeAll(sock, Data(reply.utf8))
                }
            }
        }

        close()
        DispatchQueue.main.async { onClosed() }
    }

    private func writeAll(_ sock: Int32, _ data: Data) -> Bool {
        data.withUnsafeBytes { raw in
            guard let base = raw.baseAddress else { return true }
            var written = 0
            while written < data.count {
                let n = Darwin.write(sock, base.advanced(by: written), data.count - written)
                if n <= 0 { return false }
                written += n
            }
            return true
        }
    }

    private static func decodeWelcome(_ obj: [String: Any]) -> Welcome {
        let rawPace = (obj["paced_scrollback"] as? String) ?? "rows"
        let pace = PCodeStreamingPaceMode(rawValue: rawPace) ?? .rows
        let rawHistory = (obj["history"] as? [[String: Any]]) ?? []
        let history = rawHistory.compactMap { PCodeHostEventDecoder.decodeJournalRecord($0) }

        let rawCalls = (obj["calls"] as? [[Any]]) ?? []
        let catchUp = rawCalls.compactMap { call -> PCodeHostEvent? in
            guard call.count >= 3,
                  let method = call[0] as? String,
                  let args = call[1] as? [Any],
                  let kwargs = call[2] as? [String: Any]
            else { return nil }
            return PCodeHostEventDecoder.decodeRPCCall(method: method, args: args, kwargs: kwargs)
        }

        return Welcome(
            hostID: (obj["id"] as? String) ?? "",
            sessionID: (obj["session_id"] as? String) ?? "",
            title: (obj["title"] as? String) ?? "",
            pacedScrollback: pace,
            history: history,
            catchUpCalls: catchUp)
    }
}
