import Foundation

/// One /btw side question as a session host reports it (`SessionHost.aside_state`
/// in `pcode/host.py`). Optional fields the host sends as `null` read as empty.
struct PCodeAsideState: Equatable {
    let id: String
    let question: String
    var model = ""
    var label = ""
    var effort = ""
    var status = "running"
    var answer = ""
    var activity = ""
    var error = ""
    /// Monotonic clock readings (Python's `time.monotonic`, system-wide), so
    /// comparable with `ProcessInfo.systemUptime` here.
    var started: Double?
    var finished: Double?
    var thread = ""
    var read = false

    var isRunning: Bool { status == "running" }

    init(id: String, question: String) {
        self.id = id
        self.question = question
    }

    /// From one decoded state object; nil without an id and question.
    init?(json: [String: Any]) {
        guard let id = json["id"] as? String, !id.isEmpty,
              let question = json["question"] as? String else { return nil }
        self.init(id: id, question: question)
        func text(_ key: String) -> String { (json[key] as? String) ?? "" }
        model = text("model")
        label = text("label")
        effort = text("effort")
        status = (json["status"] as? String) ?? "running"
        answer = text("answer")
        activity = text("activity")
        error = text("error")
        started = (json["started"] as? NSNumber)?.doubleValue
        finished = (json["finished"] as? NSNumber)?.doubleValue
        thread = text("thread")
        read = (json["read"] as? Bool) ?? false
    }
}

/// What a session host tells a side-question observer (`observe` version 2).
enum PCodeAsideMessage: Equatable {
    /// Every side question the host holds, replacing what was known.
    case asides([PCodeAsideState])
    /// One side question, new or changed.
    case changed(PCodeAsideState)
    /// The host is shutting down.
    case hostClosed

    /// A `{"type":"notify",...}` line's object; nil for anything else.
    static func decode(_ object: [String: Any]) -> PCodeAsideMessage? {
        guard object["type"] as? String == "notify", let method = object["method"] as? String else { return nil }
        let args = (object["args"] as? [Any]) ?? []
        switch method {
        case "asides":
            let states = (args.first as? [Any]) ?? []
            return .asides(states.compactMap { ($0 as? [String: Any]).flatMap(PCodeAsideState.init(json:)) })
        case "aside_changed":
            guard let state = (args.first as? [String: Any]).flatMap(PCodeAsideState.init(json:)) else { return nil }
            return .changed(state)
        case "host_closed":
            return .hostClosed
        default:
            return nil
        }
    }
}

/// The side questions known from a host's stream, in the order the host listed or added them.
struct PCodeAsideStore: Equatable {
    private(set) var asides: [PCodeAsideState] = []

    mutating func apply(_ message: PCodeAsideMessage) {
        switch message {
        case .asides(let states):
            asides = states
        case .changed(let state):
            if let index = asides.firstIndex(where: { $0.id == state.id }) {
                asides[index] = state
            } else {
                asides.append(state)
            }
        case .hostClosed:
            break
        }
    }

    /// `thread`'s questions, oldest first.
    func thread(_ thread: String) -> [PCodeAsideState] {
        asides.filter { $0.thread == thread }
    }

    /// How many threads the host has.
    var threadCount: Int { Set(asides.map(\.thread)).count }
}

/// A read-only observer of a session host's side questions, as `pcode.btw_tail`
/// watches them for the drawer: `{"type":"observe","version":2}`, a `ready`
/// reply, then newline-delimited `notify` messages.
///
/// A background thread owns the socket and hands everything to `onEvent` on
/// the main queue. When the host goes it tries again every two seconds, so a
/// /restart comes back by itself; meanwhile `note` says why nothing updates.
final class PCodeAsideObserver {
    static let version = 2
    static let reconnectSeconds: TimeInterval = 2
    private static let maxLineBytes = 64 * 1024 * 1024
    /// What a host on an older observer version answers (`btw_tail.OLD_HOST`).
    static let oldHostMessage = "Unsupported drawer version."
    static let restartMessage = "This session host's pcode predates this drawer: run /restart in pcode."
    static let stoppedMessage = "The session host stopped."

    enum Event: Equatable {
        case message(PCodeAsideMessage)
        /// Why the stream is not live, or empty once it is again.
        case note(String)
    }

    let host: String
    private let socketPath: String
    private let onEvent: (Event) -> Void
    private let condition = NSCondition()
    // Guarded by `condition`.
    private var stopped = false
    private var fd: Int32 = -1
    /// Only touched on the reader thread, as `Tail.note` is.
    private var note = ""

    init(host: String, socketPath: String, onEvent: @escaping (Event) -> Void) {
        self.host = host
        self.socketPath = socketPath
        self.onEvent = onEvent
    }

    /// The socket `host` listens on: `<hosts dir>/<host>.sock` (`host_protocol.socket_path`).
    static func socketPath(host: String) -> String {
        PCodeSessionInfo.hostRoot.appendingPathComponent("\(host).sock").path
    }

    /// Start the reader thread, which holds this observer until `stop`.
    func start() {
        let thread = Thread { [self] in run() }
        thread.name = "pcode.btw.observer"
        thread.qualityOfService = .userInitiated
        thread.start()
    }

    /// End the stream for good: no more events reach `onEvent`.
    func stop() {
        condition.lock()
        stopped = true
        if fd >= 0 { Darwin.shutdown(fd, SHUT_RDWR) }
        condition.broadcast()
        condition.unlock()
    }

    private var isStopped: Bool {
        condition.lock()
        defer { condition.unlock() }
        return stopped
    }

    private func emit(_ event: Event) {
        DispatchQueue.main.async { [weak self] in
            guard let self, !self.isStopped else { return }
            self.onEvent(event)
        }
    }

    private func setNote(_ text: String) {
        note = text
        emit(.note(text))
    }

    // MARK: Reader thread

    private func run() {
        while !isStopped {
            watch()
            condition.lock()
            if !stopped { _ = condition.wait(until: Date().addingTimeInterval(Self.reconnectSeconds)) }
            condition.unlock()
        }
    }

    /// Stream the host's side questions until it goes, or say why it cannot be reached.
    private func watch() {
        guard let sock = Self.connect(socketPath) else {
            if note.isEmpty { setNote("Session \(host) is not running.") }
            return
        }
        condition.lock()
        if stopped {
            condition.unlock()
            Darwin.close(sock)
            return
        }
        fd = sock
        condition.unlock()
        defer {
            condition.lock()
            fd = -1
            condition.unlock()
            Darwin.close(sock)
        }

        let hello = "{\"type\":\"observe\",\"version\":\(Self.version)}\n"
        guard Self.writeAll(sock, Data(hello.utf8)) else {
            setNote("Lost the session host: the handshake could not be sent.")
            return
        }
        var ready = false
        let finished = Self.readLines(sock) { object in
            if !ready {
                guard object["type"] as? String == "ready" else {
                    let message = (object["message"] as? String).flatMap { $0.isEmpty ? nil : $0 }
                        ?? Self.restartMessage
                    setNote(message == Self.oldHostMessage ? Self.restartMessage : message)
                    return false
                }
                ready = true
                if !note.isEmpty { setNote("") }
                return true
            }
            guard let message = PCodeAsideMessage.decode(object) else { return true }
            if message == .hostClosed {
                setNote(Self.stoppedMessage)
            } else {
                emit(.message(message))
            }
            return true
        }
        if isStopped { return }
        if !finished {
            setNote("Lost the session host.")
        } else if !ready {
            // Closed before answering: as `btw_tail`, an old host.
            if note.isEmpty { setNote(Self.restartMessage) }
        } else if note.isEmpty {
            setNote(Self.stoppedMessage)
        }
    }

    /// A connected `AF_UNIX` stream socket to `path`, owned by this user; nil if none.
    static func connect(_ path: String) -> Int32? {
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8)
        guard bytes.count < MemoryLayout.size(ofValue: addr.sun_path) else { return nil }
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in
            raw.copyBytes(from: bytes)
            raw[bytes.count] = 0
        }
        let sock = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard sock >= 0 else { return nil }
        var noSigPipe: Int32 = 1
        setsockopt(sock, SOL_SOCKET, SO_NOSIGPIPE, &noSigPipe, socklen_t(MemoryLayout<Int32>.size))
        let connected = withUnsafePointer(to: &addr) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(sock, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        var euid: uid_t = 0
        var egid: gid_t = 0
        guard connected == 0, Darwin.getpeereid(sock, &euid, &egid) == 0, euid == Darwin.geteuid() else {
            Darwin.close(sock)
            return nil
        }
        return sock
    }

    /// Hand each JSON object line to `handle` until it returns false, the
    /// socket closes, or a line passes the limit. True if the peer closed or
    /// `handle` stopped; false on a read error or an oversized line.
    private static func readLines(_ sock: Int32, _ handle: ([String: Any]) -> Bool) -> Bool {
        var buffer = Data()
        var scanOffset = 0
        var chunk = [UInt8](repeating: 0, count: 64 * 1024)
        while true {
            let count = Darwin.read(sock, &chunk, chunk.count)
            if count < 0 {
                if errno == EINTR { continue }
                return false
            }
            if count == 0 { return true }
            buffer.append(contentsOf: chunk[0..<count])
            if buffer.count - scanOffset > maxLineBytes { return false }
            while scanOffset < buffer.count, let newline = buffer[scanOffset...].firstIndex(of: 0x0A) {
                let line = buffer[scanOffset..<newline]
                scanOffset = newline + 1
                guard !line.isEmpty,
                      let object = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else { continue }
                if !handle(object) { return true }
            }
            if scanOffset >= buffer.count {
                buffer.removeAll(keepingCapacity: true)
                scanOffset = 0
            } else if scanOffset >= 64 * 1024 {
                buffer.removeSubrange(0..<scanOffset)
                scanOffset = 0
            }
        }
    }

    private static func writeAll(_ sock: Int32, _ data: Data) -> Bool {
        data.withUnsafeBytes { raw in
            guard let base = raw.baseAddress else { return true }
            var written = 0
            while written < raw.count {
                let count = Darwin.write(sock, base + written, raw.count - written)
                if count < 0 {
                    if errno == EINTR { continue }
                    return false
                }
                written += count
            }
            return true
        }
    }
}
