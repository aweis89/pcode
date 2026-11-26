import Darwin
import Foundation

/// Off-by-default startup timing: one line per milestone, in ms since the
/// process started, to stderr and `$TMPDIR/pcode-startup.log`.
///
/// Turn on with `PCODE_STARTUP_TRACE=1` in the app's environment or
/// `defaults write <bundle-id> PCodeStartupTrace -bool YES`. Every call is a
/// cheap no-op otherwise.
enum PCodeStartupTrace {
    static let enabled: Bool = {
        if let env = ProcessInfo.processInfo.environment["PCODE_STARTUP_TRACE"], !env.isEmpty, env != "0" {
            return true
        }
        return UserDefaults.standard.bool(forKey: "PCodeStartupTrace")
    }()

    private static let lock = NSLock()
    private static var seen: Set<String> = []

    /// Wall-clock process start, from the kernel (`kp_proc.p_starttime`).
    private static let processStart: Double = {
        var info = kinfo_proc()
        var size = MemoryLayout<kinfo_proc>.stride
        var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_PID, getpid()]
        guard sysctl(&mib, u_int(mib.count), &info, &size, nil, 0) == 0 else {
            return Date().timeIntervalSince1970
        }
        let start = info.kp_proc.p_starttime
        return Double(start.tv_sec) + Double(start.tv_usec) / 1_000_000
    }()

    private static let logURL = URL(fileURLWithPath: NSTemporaryDirectory())
        .appendingPathComponent("pcode-startup.log")

    private static var panesCreated = 0
    private static var panesReady = 0

    /// A session pane's terminal was made (main thread).
    static func paneCreated(_ detail: @autoclosure () -> String) {
        guard enabled else { return }
        panesCreated += 1
        mark("pane \(panesCreated) created (\(detail()))")
    }

    /// A session pane dropped its boot overlay (main thread).
    static func paneReady(via reason: String) {
        guard enabled else { return }
        panesReady += 1
        mark("pane ready \(panesReady)/\(panesCreated) via \(reason)")
        if panesReady == 1 { mark("first pane ready") }
        if panesReady == panesCreated { mark("all \(panesCreated) panes ready") }
    }

    /// Record `name`; with `once`, only its first occurrence.
    static func mark(_ name: @autoclosure () -> String, once: Bool = false) {
        guard enabled else { return }
        let label = name()
        let ms = (Date().timeIntervalSince1970 - processStart) * 1000
        lock.lock()
        defer { lock.unlock() }
        if once, !seen.insert(label).inserted { return }
        let line = String(format: "pcode-startup %8.1f ms  %@%@\n", ms, label, Thread.isMainThread ? "" : " (bg)")
        FileHandle.standardError.write(Data(line.utf8))
        if let handle = try? FileHandle(forWritingTo: logURL) {
            handle.seekToEndOfFile()
            handle.write(Data(line.utf8))
            try? handle.close()
        } else {
            try? Data(line.utf8).write(to: logURL)
        }
    }
}
