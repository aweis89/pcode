import AppKit
import CoreGraphics
import Foundation
import Markdown

/// Matches pcode's `paced_scrollback` preference (`"rows"`, `"typed"`, `"off"`).
enum PCodeStreamingPaceMode: String, CaseIterable, Equatable {
    case rows
    case typed
    case off

    /// Read `~/.config/pcode/preferences.json` as a local fallback (Python's default is `.rows`).
    /// Live host sessions override this from session state when connected.
    static func loadDefaultFromPreferences() -> PCodeStreamingPaceMode {
        let url = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".config/pcode/preferences.json")
        guard let data = try? Data(contentsOf: url),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let raw = json["paced_scrollback"] as? String,
              let mode = PCodeStreamingPaceMode(rawValue: raw)
        else {
            return .rows
        }
        return mode
    }
}

/// Stage 1 of the settle-then-reveal pipeline (mirrors pcode's `settled_lines()`).
///
/// Buffers incoming `TextDelta`s and commits ONLY settled top-level Markdown blocks that
/// later text cannot mutate — eliminating raw `**bold` / `` `code `` snapping and mid-word
/// line re-wraps. Settled blocks are laid out once at final geometry and then uncovered by
/// `PCodeRevealQueue`.
struct PCodeMarkdownSettler {
    private(set) var fullMarkdown: String = ""
    private var settledBlockCount: Int = 0
    private var lastParsedBlocks: [PCodeMarkdownBlock] = []

    var hasUnsettledTail: Bool {
        let trimmed = fullMarkdown.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return false }
        return settledBlockCount < lastParsedBlocks.count || lastParsedBlocks.isEmpty
    }

    mutating func reset() {
        fullMarkdown = ""
        settledBlockCount = 0
        lastParsedBlocks = []
    }

    /// Append a streaming text delta and return any newly settled top-level blocks (if any).
    mutating func append(_ delta: String) -> [PCodeMarkdownBlock] {
        guard !delta.isEmpty else { return [] }
        fullMarkdown.append(delta)
        return collectNewlySettled(final: false)
    }

    /// Settle and return all remaining blocks when the assistant message finishes.
    mutating func finish() -> [PCodeMarkdownBlock] {
        collectNewlySettled(final: true)
    }

    private mutating func collectNewlySettled(final: Bool) -> [PCodeMarkdownBlock] {
        let trimmed = fullMarkdown.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return [] }

        let allBlocks = PCodeTranscriptMarkdown.parseBlocks(fullMarkdown)
        lastParsedBlocks = allBlocks
        guard !allBlocks.isEmpty else { return [] }

        let targetSettledCount: Int
        if final {
            targetSettledCount = allBlocks.count
        } else {
            // Everything strictly before the last top-level block is settled.
            // Only settle the last block on a trailing blank line (`\n\n`) when that last block
            // is a leaf block that cannot continue after a blank line (never a list or blockquote,
            // which can have loose items / continuation paragraphs after a blank line!).
            let inOpenFence = PCodeStreamingMessageDiffer.detectTrailingOpenFence(in: fullMarkdown) != nil
            if inOpenFence {
                var tailCodeCount = 0
                for block in allBlocks.reversed() {
                    if case .codeBlock = block {
                        tailCodeCount += 1
                    } else {
                        break
                    }
                }
                targetSettledCount = max(0, allBlocks.count - tailCodeCount)
            } else if fullMarkdown.hasSuffix("\n\n") || fullMarkdown.hasSuffix("\r\n\r\n") {
                let doc = Document(parsing: fullMarkdown, options: [])
                let lastChild = doc.children.reversed().first
                let canContinueAfterBlankLine = (lastChild is UnorderedList)
                    || (lastChild is OrderedList)
                    || (lastChild is BlockQuote)
                targetSettledCount = canContinueAfterBlankLine
                    ? max(0, allBlocks.count - 1)
                    : allBlocks.count
            } else {
                targetSettledCount = max(0, allBlocks.count - 1)
            }
        }

        guard targetSettledCount > settledBlockCount else { return [] }
        let newlySettled = Array(allBlocks[settledBlockCount..<targetSettledCount])
        settledBlockCount = targetSettledCount
        return newlySettled
    }
}

/// Purely visual FIFO reveal queue over laid-out transcript blocks.
///
/// Mirrors Ghostty Smooth's `State.zig` reveal queue:
/// - **Ink sweep** (`.inkSweep`, used for prose in `.typed` mode): uncovers settled lines
///   left-to-right at 280 cells/s (~2240 pt/s) up to 2400 cells/s, 2.0s catch-up.
/// - **Curtain** (`.curtain`, used for code/tables/tools/diffs in `.typed` mode and all blocks
///   in `.rows` mode): compositor-only top-to-bottom gradient mask at 10–30 rows/s (200–600 pt/s),
///   1.5s catch-up.
/// - **Monotonic speed:** speed never decreases mid-reveal (`max(speed, targetSpeed)`).
/// - **Visibility gated:** holds an entry at 0 until its top enters the visible viewport.
struct PCodeRevealQueue {
    enum Style: Equatable {
        case inkSweep
        case curtain
    }

    struct Entry: Equatable {
        let itemID: String
        let style: Style
        /// Total extent to traverse: horizontal line points for `.inkSweep`, or height in pt for `.curtain`.
        var targetExtent: CGFloat
        /// Current frontier in points (`0...targetExtent`).
        var progress: CGFloat
        /// Current monotonic speed in points/sec.
        var speed: CGFloat
        /// Extra feather distance for `.inkSweep` so the leading feather exits the final line cleanly.
        let featherMargin: CGFloat

        var normalizedProgress: CGFloat {
            let total = max(1, targetExtent)
            return min(1, max(0, progress / total))
        }

        var isComplete: Bool {
            progress >= targetExtent + featherMargin
        }
    }

    struct StepUpdate: Equatable {
        let itemID: String
        let style: Style
        /// `nil` once complete (clears mask/frontier).
        let frontier: CGFloat?
        /// Normalized `0...1` progress for `.curtain` (`nil` once complete).
        let curtainProgress: CGFloat?
    }

    private(set) var entries: [Entry] = []

    var isEmpty: Bool { entries.isEmpty }

    mutating func reset() {
        entries.removeAll(keepingCapacity: true)
    }

    mutating func enqueue(itemID: String, style: Style, targetExtent: CGFloat, featherMargin: CGFloat = 48) {
        if entries.contains(where: { $0.itemID == itemID }) { return }
        let safeExtent = max(1, targetExtent)
        let margin: CGFloat = (style == .inkSweep) ? featherMargin : 0
        entries.append(Entry(
            itemID: itemID,
            style: style,
            targetExtent: safeExtent,
            progress: 0,
            speed: 0,
            featherMargin: margin))
    }

    mutating func updateExtent(for itemID: String, targetExtent: CGFloat) {
        guard let idx = entries.firstIndex(where: { $0.itemID == itemID }) else { return }
        entries[idx].targetExtent = max(1, targetExtent)
    }

    mutating func remove(itemID: String) {
        entries.removeAll { $0.itemID == itemID }
    }

    /// Immediately complete and return updates for any queued entries satisfying `predicate`
    /// (e.g. offscreen items above the viewport after clicking "Jump to bottom").
    mutating func completeEntries(where predicate: (String) -> Bool) -> [StepUpdate] {
        var updates: [StepUpdate] = []
        entries.removeAll { entry in
            if predicate(entry.itemID) {
                updates.append(StepUpdate(
                    itemID: entry.itemID,
                    style: entry.style,
                    frontier: nil,
                    curtainProgress: nil))
                return true
            }
            return false
        }
        return updates
    }

    /// Current reveal state for a newly mounted view so scrolled-in queued items start hidden.
    func state(for itemID: String) -> StepUpdate? {
        guard let entry = entries.first(where: { $0.itemID == itemID }) else { return nil }
        switch entry.style {
        case .inkSweep:
            return StepUpdate(itemID: itemID, style: .inkSweep, frontier: entry.progress, curtainProgress: nil)
        case .curtain:
            return StepUpdate(itemID: itemID, style: .curtain, frontier: nil, curtainProgress: entry.normalizedProgress)
        }
    }

    /// Advance the head reveal entry by `dt` seconds, gated on `isVisible(itemID)`.
    mutating func step(
        dt: TimeInterval,
        speedMultiplier: CGFloat = 1.0,
        isVisible: (String) -> Bool
    ) -> [StepUpdate] {
        guard !entries.isEmpty else { return [] }
        let clampedDt = CGFloat(min(max(dt, 1.0 / 240.0), 0.05))
        let mult = max(0.25, speedMultiplier)
        var updates: [StepUpdate] = []
        var remainingDt = clampedDt

        while !entries.isEmpty && remainingDt > 0.0001 {
            let head = entries[0]
            if !isVisible(head.itemID) {
                // Visibility gate: hold reveal until the item glides into view above the clip edge.
                break
            }

            let minSpeed: CGFloat
            let maxSpeed: CGFloat
            let catchUp: CGFloat
            let backlog: CGFloat
            switch head.style {
            case .inkSweep:
                // Ghostty Smooth: 280 cells/s min (~2240 pt/s), 2400 cells/s max, 2.0s catch-up.
                minSpeed = 2240.0 * mult
                maxSpeed = 19200.0 * mult
                catchUp = 2.0 / mult
                backlog = entries
                    .filter { $0.style == .inkSweep }
                    .reduce(0) { $0 + max(0, ($1.targetExtent + $1.featherMargin) - $1.progress) }
            case .curtain:
                // Ghostty Smooth: 10 rows/s min (~200 pt/s), 30 rows/s max (~600 pt/s), 1.5s catch-up.
                minSpeed = 200.0 * mult
                maxSpeed = 600.0 * mult
                catchUp = 1.5 / mult
                backlog = entries
                    .filter { $0.style == .curtain }
                    .reduce(0) { $0 + max(0, $1.targetExtent - $1.progress) }
            }

            let targetSpeed = min(maxSpeed, max(minSpeed, backlog / max(0.1, catchUp)))
            // Monotonic speed: never decelerate mid-reveal.
            let speed = max(head.speed, targetSpeed)
            entries[0].speed = speed

            let totalSpan = head.targetExtent + head.featherMargin
            let distNeeded = max(0, totalSpan - head.progress)
            let distStep = speed * remainingDt

            if distStep >= distNeeded {
                let completedID = head.itemID
                let completedStyle = head.style
                entries.removeFirst()
                updates.append(StepUpdate(itemID: completedID, style: completedStyle, frontier: nil, curtainProgress: nil))
                let usedDt = speed > 0 ? (distNeeded / speed) : remainingDt
                remainingDt = max(0, remainingDt - usedDt)
            } else {
                entries[0].progress += distStep
                let updated = entries[0]
                switch updated.style {
                case .inkSweep:
                    updates.append(StepUpdate(
                        itemID: updated.itemID,
                        style: .inkSweep,
                        frontier: updated.progress,
                        curtainProgress: nil))
                case .curtain:
                    updates.append(StepUpdate(
                        itemID: updated.itemID,
                        style: .curtain,
                        frontier: nil,
                        curtainProgress: updated.normalizedProgress))
                }
                remainingDt = 0
            }
        }

        return updates
    }
}

/// Backlog-paced conveyor smooth-scroll glide for sticky-bottom streaming.
///
/// Mirrors Ghostty Smooth's conveyor (`src/pcode/smooth_scroll.py`):
/// - `minSpeed = 110 pt/s` (~5.5 rows/s), `maxSpeed = 660 pt/s` (~33 rows/s)
/// - `catchUpSeconds = 2.2s` so multi-block output glides continuously without start-stop kicks
/// - Smooth velocity easing with a gentle 24pt landing taper.
struct PCodeSmoothScrollGlide: Equatable {
    var minSpeed: CGFloat
    var maxSpeed: CGFloat
    var catchUpSeconds: CGFloat
    var maxOffset: CGFloat

    /// Points remaining behind the target bottom origin (`> 0` while gliding downward).
    private(set) var remainingOffset: CGFloat = 0
    /// Current conveyor velocity in points/sec.
    private(set) var velocity: CGFloat = 0

    var isActive: Bool {
        remainingOffset > 0.25
    }

    init(
        minSpeed: CGFloat = 110.0,
        maxSpeed: CGFloat = 660.0,
        catchUpSeconds: CGFloat = 2.2,
        maxOffset: CGFloat = 2400.0
    ) {
        self.minSpeed = max(20, minSpeed)
        self.maxSpeed = max(self.minSpeed, maxSpeed)
        self.catchUpSeconds = max(0.2, catchUpSeconds)
        self.maxOffset = max(64, maxOffset)
    }

    mutating func reset() {
        remainingOffset = 0
        velocity = 0
    }

    /// Absorb a new bottom growth delta (`> 0` when content grew taller) into the active conveyor.
    mutating func addOffset(_ delta: CGFloat) {
        guard delta.isFinite, delta > 0.01 else { return }
        remainingOffset = min(remainingOffset + delta, maxOffset)
    }

    /// Absorb a bottom shrink (e.g. an overestimated offscreen item measuring shorter) without
    /// pulling the viewport backward.
    mutating func absorbShrink(_ shrinkAmount: CGFloat) {
        guard shrinkAmount.isFinite, shrinkAmount > 0, remainingOffset > 0 else { return }
        remainingOffset = max(0, remainingOffset - shrinkAmount)
        if remainingOffset <= 0.25 {
            reset()
        }
    }

    /// Advance the conveyor glide by `dt` seconds and return the new `remainingOffset`.
    @discardableResult
    mutating func step(dt: TimeInterval, speedMultiplier: CGFloat = 1.0) -> CGFloat {
        guard isActive else {
            reset()
            return 0
        }
        let clampedDt = CGFloat(min(max(dt, 1.0 / 240.0), 0.05))
        let mult = max(0.25, speedMultiplier)
        let effMin = minSpeed * mult
        let effMax = maxSpeed * mult
        let effCatchUp = max(0.15, catchUpSeconds / mult)

        let rawTarget: CGFloat
        if remainingOffset < 24.0 {
            rawTarget = max(24.0 * mult, (remainingOffset / 24.0) * effMin)
        } else {
            rawTarget = min(effMax, max(effMin, remainingOffset / effCatchUp))
        }

        // Smooth velocity easing so arrival of new blocks never kicks acceleration abruptly.
        let blend = 1.0 - exp(-8.0 * clampedDt)
        velocity = velocity + (rawTarget - velocity) * blend

        let advance = velocity * clampedDt
        if advance >= remainingOffset - 0.25 {
            reset()
            return 0
        }
        remainingOffset -= advance
        return remainingOffset
    }
}

/// Per-frame Markdown block differ with open-fence detection (used by `PCodeMarkdownSettler`
/// and streaming diff unit tests).
struct PCodeStreamingMessageDiffer {
    struct OpenFence: Equatable {
        let prefixThroughOpeningLine: String
        let fenceChar: Character
        let fenceLength: Int
        let language: String?
        let body: String
    }

    struct DiffResult: Equatable {
        let commonPrefixCount: Int
        let newBlocks: [PCodeMarkdownBlock]
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

        let newBlocks = PCodeTranscriptMarkdown.parseBlocks(markdown)
        let common = Self.commonPrefixCount(old: blocks, new: newBlocks)
        rawMarkdown = markdown
        blocks = newBlocks
        lastUpdateUsedOpenFenceFastPath = false

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
        guard let fc = line.first, fc == "`" || fc == "~" else { return nil }
        var idx = line.startIndex
        var count = 0
        while idx < line.endIndex && line[idx] == fc {
            count += 1
            idx = line.index(after: idx)
        }
        guard count >= 3 else { return nil }

        let info = line[idx...].trimmingCharacters(in: .whitespacesAndNewlines)
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

/// Settled-block stream replayer that drives `PCodeTranscriptDocumentView` at `1×` (regular)
/// or `4×` (accelerated) speed through the exact same reveal queue and conveyor glide used
/// for live sessions.
final class PCodeTranscriptStreamReplayer {
    enum Step: Equatable {
        case beginTurn(prompt: String, isSide: Bool)
        case settledMessage(id: String, markdown: String)
        case toolCall(name: String, detail: String, command: String, failed: Bool, elapsed: Double?, id: Int)
        case finishTurn(PCodePeekTurn.Status)
    }

    private weak var documentView: PCodeTranscriptDocumentView?
    private let steps: [Step]
    private var nextStepIndex = 0
    private var timer: Timer?
    private let speedMultiplier: Double
    private let stepInterval: TimeInterval
    private let onComplete: (() -> Void)?

    var isRunning: Bool { timer != nil }

    init(
        turns: [PCodePeekTurn],
        documentView: PCodeTranscriptDocumentView,
        speedMultiplier: Double = 1.0,
        onComplete: (() -> Void)? = nil
    ) {
        self.documentView = documentView
        self.speedMultiplier = max(0.25, speedMultiplier)
        self.steps = Self.buildSteps(from: turns)
        // Natural cadence per settled block/tool call, scaled by speedMultiplier.
        self.stepInterval = max(0.03, 0.22 / self.speedMultiplier)
        self.onComplete = onComplete
    }

    deinit {
        stop()
    }

    static func buildSteps(from turns: [PCodePeekTurn]) -> [Step] {
        var out: [Step] = []
        for turn in turns {
            out.append(.beginTurn(prompt: turn.prompt, isSide: turn.side))
            for block in turn.blocks {
                switch block {
                case .markdown(let blockID, let text):
                    out.append(.settledMessage(id: "replay-turn-\(turn.id)-md-\(blockID)", markdown: text))
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
        documentView.streamingSpeedMultiplier = CGFloat(speedMultiplier)
        documentView.setItems([], pinToBottom: true)
        // Fire the first step immediately so replay begins without a blank beat.
        if !stepOnce() {
            onComplete?()
            return
        }
        let t = Timer(timeInterval: stepInterval, repeats: true) { [weak self] _ in
            guard let self else { return }
            // Keep a comfortable reveal queue depth while pinned at bottom (at most 3 queued
            // items ahead). When the user scrolls up, keep advancing so replay still completes.
            if self.documentView?.stickyBottom.mode == .pinnedToBottom,
               (self.documentView?.pendingRevealCount ?? 0) > 3
            {
                return
            }
            if !self.stepOnce() {
                if self.documentView?.stickyBottom.mode == .pinnedToBottom,
                   self.documentView?.isStreamingAnimationActive == true
                {
                    return
                }
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
        documentView?.streamingSpeedMultiplier = 1.0
    }

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
        case .settledMessage(let id, let markdown):
            documentView.beginStreamingMessage(id: id)
            documentView.appendStreamingDelta(markdown)
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

    /// Run all steps synchronously (for headless unit tests and benchmarks).
    func runAllSynchronously() {
        documentView?.setItems([], pinToBottom: true)
        nextStepIndex = 0
        while stepOnce() {}
    }
}
