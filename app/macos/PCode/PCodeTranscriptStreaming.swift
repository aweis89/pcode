import AppKit
import CoreGraphics
import Foundation

/// Adaptive 120Hz text pacer that smooths bursty LLM token deltas into a steady,
/// backlog-paced character stream on every `CADisplayLink` frame.
///
/// Always steps on Swift `Character` (extended grapheme cluster) boundaries so emoji
/// and combining marks are never split mid-stream.
struct PCodeStreamingTextPacer {
    var minCharsPerSecond: Double
    var maxCharsPerSecond: Double
    var catchUpSeconds: Double

    private var pending: String = ""
    private var readIndex: String.Index
    private var remainingCount: Int = 0
    private var consumedCount: Int = 0
    private var charBudget: Double = 0

    var hasPending: Bool { remainingCount > 0 }
    var pendingCount: Int { remainingCount }

    init(
        minCharsPerSecond: Double = 110,
        maxCharsPerSecond: Double = 2800,
        catchUpSeconds: Double = 0.18
    ) {
        self.minCharsPerSecond = minCharsPerSecond
        self.maxCharsPerSecond = maxCharsPerSecond
        self.catchUpSeconds = max(0.02, catchUpSeconds)
        self.readIndex = pending.startIndex
    }

    mutating func reset() {
        pending.removeAll(keepingCapacity: true)
        readIndex = pending.startIndex
        remainingCount = 0
        consumedCount = 0
        charBudget = 0
    }

    mutating func enqueue(_ delta: String) {
        guard !delta.isEmpty else { return }
        if remainingCount == 0 {
            pending.removeAll(keepingCapacity: true)
            pending.append(delta)
            readIndex = pending.startIndex
            consumedCount = 0
            remainingCount = delta.count
        } else {
            let offset = pending.utf8.distance(from: pending.startIndex, to: readIndex)
            pending.append(delta)
            readIndex = pending.utf8.index(pending.startIndex, offsetBy: offset)
            remainingCount += delta.count
        }
    }

    /// Adaptive drain rate (characters/sec) based on current backlog.
    func currentRate() -> Double {
        let backlog = Double(remainingCount)
        let target = backlog / catchUpSeconds
        return min(maxCharsPerSecond, max(minCharsPerSecond, target))
    }

    /// Drain the paced chunk for a single display frame of duration `dt` seconds.
    /// Returns `nil` if no full character is ready on this tick.
    mutating func drain(dt: TimeInterval) -> String? {
        guard remainingCount > 0 else {
            charBudget = 0
            return nil
        }
        let clampedDt = min(max(dt, 1.0 / 240.0), 0.1)
        charBudget += currentRate() * clampedDt
        let count = Int(charBudget)
        guard count > 0 else { return nil }
        charBudget -= Double(count)

        if count >= remainingCount {
            let out = String(pending[readIndex...])
            reset()
            return out
        }

        let splitIdx = pending.index(readIndex, offsetBy: count)
        let out = String(pending[readIndex..<splitIdx])
        readIndex = splitIdx
        remainingCount -= count
        consumedCount += count

        if consumedCount >= 2048 {
            pending.removeSubrange(pending.startIndex..<readIndex)
            readIndex = pending.startIndex
            consumedCount = 0
        }
        return out
    }

    /// Drain all buffered text immediately (used on message finalization or headless tests).
    mutating func flushAll() -> String? {
        guard remainingCount > 0 else {
            charBudget = 0
            return nil
        }
        let out = String(pending[readIndex...])
        reset()
        return out
    }
}

/// Critically-damped spring smooth-scroll glide for sticky-bottom streaming.
///
/// Mirrors `pcode`'s `smooth_scroll.py` (`GLIDE_STIFFNESS = 220.0`), gliding
/// `clipView.bounds.origin.y` pixel-by-pixel at 120Hz when new lines, code blocks,
/// or tool cards arrive instead of snapping by full line/block heights.
struct PCodeSmoothScrollGlide: Equatable {
    var stiffness: CGFloat
    var maxOffset: CGFloat

    /// Points remaining behind the target bottom origin (`> 0` while gliding downward).
    private(set) var remainingOffset: CGFloat = 0
    private(set) var velocity: CGFloat = 0

    var isActive: Bool {
        abs(remainingOffset) > 0.25 || abs(velocity) > 1.0
    }

    init(stiffness: CGFloat = 220.0, maxOffset: CGFloat = 480.0) {
        self.stiffness = max(10, stiffness)
        self.maxOffset = max(32, maxOffset)
    }

    mutating func reset() {
        remainingOffset = 0
        velocity = 0
    }

    /// Absorb a new bottom growth delta (`> 0` when content grew taller) into the active glide.
    mutating func addOffset(_ delta: CGFloat) {
        guard delta.isFinite, abs(delta) > 0.01 else { return }
        remainingOffset = min(max(remainingOffset + delta, -maxOffset), maxOffset)
    }

    /// Absorb a bottom shrink (e.g. an overestimated offscreen item measuring shorter) without
    /// pulling the viewport backward.
    mutating func absorbShrink(_ shrinkAmount: CGFloat) {
        guard shrinkAmount.isFinite, shrinkAmount > 0, remainingOffset > 0 else { return }
        remainingOffset = max(0, remainingOffset - shrinkAmount)
        if remainingOffset == 0 {
            velocity = 0
        }
    }

    /// Advance the critically-damped spring by `dt` seconds and return the new `remainingOffset`.
    @discardableResult
    mutating func step(dt: TimeInterval) -> CGFloat {
        guard isActive else {
            reset()
            return 0
        }
        let clampedDt = CGFloat(min(max(dt, 1.0 / 240.0), 0.1))
        let omega = sqrt(stiffness)
        let c = velocity + omega * remainingOffset
        let decay = exp(-omega * clampedDt)
        let nextOffset = (remainingOffset + c * clampedDt) * decay
        let nextVel = (velocity - omega * c * clampedDt) * decay

        let crossed = remainingOffset != 0 && (remainingOffset > 0) != (nextOffset > 0)
        if crossed || (abs(nextOffset) <= 0.25 && abs(nextVel) < 1.0) {
            reset()
            return 0
        }
        remainingOffset = nextOffset
        velocity = nextVel
        return remainingOffset
    }
}

/// Per-frame Markdown block differ for an active streaming assistant message.
///
/// Re-parses the active message via `PCodeTranscriptMarkdown.parseBlocks` and computes
/// the longest unchanged prefix so earlier blocks keep their identity, layout, and user
/// selection untouched. When the message ends inside an unclosed fenced code block, uses
/// an append-only fast path that avoids re-parsing the full Markdown document on every frame.
struct PCodeStreamingMessageDiffer {
    struct OpenFence: Equatable {
        /// Exact substring of the markdown up to and including the opening fence line's newline.
        let prefixThroughOpeningLine: String
        let fenceChar: Character
        let fenceLength: Int
        let language: String?
        let body: String
    }

    struct DiffResult: Equatable {
        /// Number of leading blocks identical to the previous frame (`0..<commonPrefixCount`).
        let commonPrefixCount: Int
        /// Full updated block list for the active message.
        let newBlocks: [PCodeMarkdownBlock]
        /// Whether this frame took the append-only open-fence fast path.
        let usedOpenFenceFastPath: Bool
    }

    private(set) var rawMarkdown: String = ""
    private(set) var blocks: [PCodeMarkdownBlock] = []
    private(set) var lastUpdateUsedOpenFenceFastPath: Bool = false

    private var activeFence: OpenFence?
    private var prefixBlockCountBeforeFence: Int = 0

    mutating func reset() {
        rawMarkdown = ""
        blocks = []
        lastUpdateUsedOpenFenceFastPath = false
        activeFence = nil
        prefixBlockCountBeforeFence = 0
    }

    /// Apply the latest full Markdown string for the active message and return the minimal block diff.
    mutating func update(markdown: String) -> DiffResult {
        if let fence = activeFence,
           prefixBlockCountBeforeFence <= blocks.count,
           markdown.hasPrefix(fence.prefixThroughOpeningLine)
        {
            let suffix = String(markdown.dropFirst(fence.prefixThroughOpeningLine.count))
            if !suffix.contains("\r"),
               !Self.containsClosingFence(in: suffix, fenceChar: fence.fenceChar, fenceLength: fence.fenceLength)
            {
                let rawCode = suffix.hasSuffix("\n") ? String(suffix.dropLast()) : suffix
                let chunks = PCodeTranscriptMarkdown.chunkLines(
                    rawCode,
                    maxLines: PCodeTranscriptMarkdown.maxCodeLinesPerItem)
                var newBlocks = Array(blocks.prefix(prefixBlockCountBeforeFence))
                newBlocks.reserveCapacity(prefixBlockCountBeforeFence + chunks.count)
                for (idx, chunk) in chunks.enumerated() {
                    newBlocks.append(.codeBlock(
                        language: fence.language,
                        code: chunk,
                        fullCode: rawCode,
                        chunkIndex: idx,
                        totalChunks: chunks.count))
                }
                let common = Self.commonPrefixCount(old: blocks, new: newBlocks)
                rawMarkdown = markdown
                blocks = newBlocks
                activeFence = OpenFence(
                    prefixThroughOpeningLine: fence.prefixThroughOpeningLine,
                    fenceChar: fence.fenceChar,
                    fenceLength: fence.fenceLength,
                    language: fence.language,
                    body: rawCode)
                lastUpdateUsedOpenFenceFastPath = true
                return DiffResult(
                    commonPrefixCount: common,
                    newBlocks: newBlocks,
                    usedOpenFenceFastPath: true)
            }
        }

        // General path: exact cmark-gfm re-parse + top-level block diff.
        let newBlocks = PCodeTranscriptMarkdown.parseBlocks(markdown)
        let common = Self.commonPrefixCount(old: blocks, new: newBlocks)
        rawMarkdown = markdown
        blocks = newBlocks
        lastUpdateUsedOpenFenceFastPath = false

        // Check if we just entered (or are inside) an unclosed top-level fenced code block
        // whose parsed `fullCode` matches `open.body` exactly so subsequent streaming frames
        // can safely take the append-only fast path.
        if let open = Self.detectTrailingOpenFence(in: markdown),
           !open.body.contains("\r"),
           let tailChunkCount = Self.trailingCodeBlockChunkCount(
               in: newBlocks,
               language: open.language,
               expectedFullCode: open.body)
        {
            activeFence = open
            prefixBlockCountBeforeFence = newBlocks.count - tailChunkCount
        } else {
            activeFence = nil
            prefixBlockCountBeforeFence = 0
        }

        return DiffResult(
            commonPrefixCount: common,
            newBlocks: newBlocks,
            usedOpenFenceFastPath: false)
    }

    /// Compare blocks for streaming stability: settled code-block chunks compare by
    /// `(language, code, chunkIndex)` rather than `fullCode`/`totalChunks` so chunk 0
    /// does not re-render at 120Hz once chunk 1 begins streaming.
    static func commonPrefixCount(old: [PCodeMarkdownBlock], new: [PCodeMarkdownBlock]) -> Int {
        let limit = min(old.count, new.count)
        var i = 0
        while i < limit && blocksMatchForDiff(old[i], new[i]) {
            i += 1
        }
        return i
    }

    private static func blocksMatchForDiff(_ lhs: PCodeMarkdownBlock, _ rhs: PCodeMarkdownBlock) -> Bool {
        switch (lhs, rhs) {
        case (.codeBlock(let l1, let c1, _, let idx1, _), .codeBlock(let l2, let c2, _, let idx2, _)):
            return l1 == l2 && c1 == c2 && idx1 == idx2
        default:
            return lhs == rhs
        }
    }

    /// Detect whether `markdown` currently ends inside an unclosed top-level fenced code block
    /// (```` ``` ```` or `~~~` with 0–3 leading spaces and a newline after the opening fence line).
    static func detectTrailingOpenFence(in markdown: String) -> OpenFence? {
        var openChar: Character?
        var openLen = 0
        var openLang: String?
        var openBodyStart: String.Index?

        var lineStart = markdown.startIndex
        while lineStart < markdown.endIndex {
            let lineEnd = markdown[lineStart...].firstIndex(of: "\n") ?? markdown.endIndex
            let line = markdown[lineStart..<lineEnd]
            let nextStart = lineEnd < markdown.endIndex ? markdown.index(after: lineEnd) : markdown.endIndex

            if let fc = openChar {
                if isClosingFenceLine(line, fenceChar: fc, fenceLength: openLen) {
                    openChar = nil
                    openLen = 0
                    openLang = nil
                    openBodyStart = nil
                }
            } else if let opened = parseOpeningFenceLine(line) {
                // Only engage the fast path once the opening fence line has its trailing newline.
                if lineEnd < markdown.endIndex {
                    openChar = opened.char
                    openLen = opened.length
                    openLang = opened.language
                    openBodyStart = nextStart
                }
            }

            lineStart = nextStart
        }

        guard let fc = openChar, let bodyStart = openBodyStart else { return nil }
        let prefix = String(markdown[..<bodyStart])
        let body = String(markdown[bodyStart...])
        return OpenFence(
            prefixThroughOpeningLine: prefix,
            fenceChar: fc,
            fenceLength: openLen,
            language: openLang,
            body: body.hasSuffix("\n") ? String(body.dropLast()) : body)
    }

    private static func containsClosingFence(in suffix: String, fenceChar: Character, fenceLength: Int) -> Bool {
        var lineStart = suffix.startIndex
        while lineStart < suffix.endIndex {
            let lineEnd = suffix[lineStart...].firstIndex(of: "\n") ?? suffix.endIndex
            if isClosingFenceLine(suffix[lineStart..<lineEnd], fenceChar: fenceChar, fenceLength: fenceLength) {
                return true
            }
            lineStart = lineEnd < suffix.endIndex ? suffix.index(after: lineEnd) : suffix.endIndex
        }
        return false
    }

    private static func parseOpeningFenceLine(_ line: Substring) -> (char: Character, length: Int, language: String?)? {
        // Restrict fast path to column-0 fences so indented fences (which strip leading spaces
        // per CommonMark) stay on the exact cmark-gfm parser.
        guard let fc = line.first, fc == "`" || fc == "~" else { return nil }
        var idx = line.startIndex
        var count = 0
        while idx < line.endIndex && line[idx] == fc {
            count += 1
            idx = line.index(after: idx)
        }
        guard count >= 3 else { return nil }

        let info = line[idx...].trimmingCharacters(in: .whitespacesAndNewlines)
        // CommonMark: backtick info strings cannot contain backticks.
        if fc == "`" && info.contains("`") { return nil }
        let langToken = info.split(whereSeparator: { $0.isWhitespace }).first.map(String.init)
        let normalizedLang = (langToken?.isEmpty == false) ? langToken : nil
        return (fc, count, normalizedLang)
    }

    private static func isClosingFenceLine(_ line: Substring, fenceChar: Character, fenceLength: Int) -> Bool {
        var idx = line.startIndex
        var leadingSpaces = 0
        while idx < line.endIndex && line[idx] == " " {
            leadingSpaces += 1
            if leadingSpaces > 3 { return false }
            idx = line.index(after: idx)
        }
        var count = 0
        while idx < line.endIndex && line[idx] == fenceChar {
            count += 1
            idx = line.index(after: idx)
        }
        guard count >= fenceLength else { return false }
        while idx < line.endIndex {
            let ch = line[idx]
            if ch != " " && ch != "\t" { return false }
            idx = line.index(after: idx)
        }
        return true
    }

    private static func trailingCodeBlockChunkCount(
        in blocks: [PCodeMarkdownBlock],
        language: String?,
        expectedFullCode: String
    ) -> Int? {
        guard case .codeBlock(let tailLang, _, let fullCode, let chunkIdx, let totalChunks) = blocks.last,
              tailLang == language,
              fullCode == expectedFullCode,
              chunkIdx == totalChunks - 1,
              totalChunks > 0,
              blocks.count >= totalChunks
        else { return nil }
        return totalChunks
    }
}

/// Event stream replayer that drives `PCodeTranscriptDocumentView` at realistic (or accelerated)
/// token-by-token speed from saved `[PCodePeekTurn]` data.
///
/// Useful both for interactive live-streaming preview inside `Pcode.app` and for deterministic
/// headless stress tests.
final class PCodeTranscriptStreamReplayer {
    enum Step: Equatable {
        case beginTurn(prompt: String, isSide: Bool)
        case beginMessage(id: String)
        case textDelta(String)
        case finishMessage
        case toolCall(name: String, detail: String, command: String, failed: Bool, elapsed: Double?, id: Int)
        case finishTurn(PCodePeekTurn.Status)
    }

    private weak var documentView: PCodeTranscriptDocumentView?
    private let steps: [Step]
    private var nextStepIndex = 0
    private var timer: Timer?
    private let stepInterval: TimeInterval
    private let onComplete: (() -> Void)?

    var isRunning: Bool { timer != nil }
    var progress: Double {
        guard !steps.isEmpty else { return 1 }
        return Double(nextStepIndex) / Double(steps.count)
    }

    init(
        turns: [PCodePeekTurn],
        documentView: PCodeTranscriptDocumentView,
        speedMultiplier: Double = 4.0,
        chunkSize: Int = 18,
        onComplete: (() -> Void)? = nil
    ) {
        self.documentView = documentView
        self.steps = Self.buildSteps(from: turns, chunkSize: max(1, chunkSize))
        self.stepInterval = max(0.004, 0.032 / max(0.5, speedMultiplier))
        self.onComplete = onComplete
    }

    deinit {
        stop()
    }

    /// Build a flat sequence of streaming steps from saved turns.
    static func buildSteps(from turns: [PCodePeekTurn], chunkSize: Int = 18) -> [Step] {
        var out: [Step] = []
        for turn in turns {
            out.append(.beginTurn(prompt: turn.prompt, isSide: turn.side))
            for block in turn.blocks {
                switch block {
                case .markdown(let blockID, let text):
                    out.append(.beginMessage(id: "replay-turn-\(turn.id)-md-\(blockID)"))
                    var idx = text.startIndex
                    while idx < text.endIndex {
                        let end = text.index(idx, offsetBy: chunkSize, limitedBy: text.endIndex) ?? text.endIndex
                        out.append(.textDelta(String(text[idx..<end])))
                        idx = end
                    }
                    out.append(.finishMessage)
                case .tool(let call):
                    out.append(.toolCall(
                        name: call.name,
                        detail: call.detail,
                        command: call.command,
                        failed: call.failed,
                        elapsed: call.elapsedSeconds,
                        id: call.id))
                }
            }
            out.append(.finishTurn(turn.status))
        }
        return out
    }

    func start() {
        stop()
        guard let documentView else { return }
        nextStepIndex = 0
        documentView.setItems([], pinToBottom: true)
        let t = Timer(timeInterval: stepInterval, repeats: true) { [weak self] _ in
            guard let self else { return }
            // Let the 120Hz text pacer finish draining the current message before `.finishMessage`
            // flushes it, so short/medium messages glide smoothly to their final character.
            if self.nextStepIndex < self.steps.count,
               case .finishMessage = self.steps[self.nextStepIndex],
               self.documentView?.hasPacedStreamingBacklog == true
            {
                return
            }
            if !self.stepOnce() {
                self.stop()
                self.onComplete?()
            }
        }
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    func stop() {
        timer?.invalidate()
        timer = nil
    }

    /// Execute a single replay step synchronously (returns `false` once finished).
    @discardableResult
    func stepOnce() -> Bool {
        guard let documentView, nextStepIndex < steps.count else {
            return false
        }
        let step = steps[nextStepIndex]
        nextStepIndex += 1
        switch step {
        case .beginTurn(let prompt, let isSide):
            documentView.beginStreamingTurn(prompt: prompt, isSide: isSide, forcePin: false)
        case .beginMessage(let id):
            documentView.beginStreamingMessage(id: id)
        case .textDelta(let delta):
            documentView.appendStreamingDelta(delta)
        case .finishMessage:
            documentView.finishStreamingMessage()
        case .toolCall(let name, let detail, let command, let failed, let elapsed, let id):
            let call = PCodePeekToolCall(
                id: id,
                name: name,
                detail: detail,
                failed: failed,
                elapsedSeconds: elapsed,
                command: command)
            documentView.appendStreamingToolCall(call)
        case .finishTurn(let status):
            documentView.finishStreamingTurn(status: status)
        }
        return nextStepIndex < steps.count
    }

    /// Run all remaining steps synchronously (for headless benchmarks and unit tests).
    func runAllSynchronously() {
        documentView?.setItems([], pinToBottom: true)
        nextStepIndex = 0
        while stepOnce() {}
    }
}
