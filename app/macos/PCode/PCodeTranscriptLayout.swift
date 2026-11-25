import CoreGraphics
import Foundation

/// Pure geometry and height-anchoring engine for `PCodeTranscriptDocumentView`.
///
/// Owns backing-pixel-rounded item heights, prefix-sum `yOffset`s, `O(log N)` visible-range
/// lookup, tail height ratcheting while streaming, and scroll-origin deltas when an item
/// above the viewport re-measures (eliminating upward-fling jumps).
struct PCodeTranscriptLayoutEngine {
    struct Entry: Equatable {
        let id: String
        var yOffset: CGFloat
        var height: CGFloat
        var spacingAfter: CGFloat
        var isMeasured: Bool
        /// Minimum height ratchet (non-zero while a streaming tail item is open so
        /// paragraph -> heading/table transitions never bounce shorter mid-stream).
        var minHeight: CGFloat
    }

    struct HeightChangeResult: Equatable {
        let oldTotalHeight: CGFloat
        let newTotalHeight: CGFloat
        /// Amount to add to `clipView.bounds.origin.y` to keep visible content stationary
        /// when the resized item is above the viewport and the view is not pinned.
        let scrollOriginDelta: CGFloat

        var didGrow: Bool { newTotalHeight > oldTotalHeight }
        var didShrink: Bool { newTotalHeight < oldTotalHeight }
    }

    private(set) var entries: [Entry] = []
    private(set) var totalHeight: CGFloat = 0
    var topPadding: CGFloat
    var bottomPadding: CGFloat
    var backingScale: CGFloat

    init(topPadding: CGFloat = 16, bottomPadding: CGFloat = 16, backingScale: CGFloat = 2) {
        self.topPadding = topPadding
        self.bottomPadding = bottomPadding
        self.backingScale = max(backingScale, 1)
        self.totalHeight = roundToPixel(topPadding + bottomPadding)
    }

    /// Round up to the nearest backing pixel so text baselines never land on fractional pixels.
    func roundToPixel(_ value: CGFloat) -> CGFloat {
        guard value.isFinite else { return 0 }
        let scale = max(backingScale, 1)
        return ceil(value * scale) / scale
    }

    mutating func reset(items: [(id: String, estimatedHeight: CGFloat, spacingAfter: CGFloat)]) {
        entries.removeAll(keepingCapacity: true)
        var y = roundToPixel(topPadding)
        for item in items {
            let h = roundToPixel(max(0, item.estimatedHeight))
            let spacing = roundToPixel(max(0, item.spacingAfter))
            entries.append(Entry(
                id: item.id,
                yOffset: y,
                height: h,
                spacingAfter: spacing,
                isMeasured: false,
                minHeight: 0))
            y += h + spacing
        }
        totalHeight = roundToPixel(y + bottomPadding)
    }

    @discardableResult
    mutating func append(id: String, height: CGFloat, spacingAfter: CGFloat, measured: Bool = true) -> Int {
        let y: CGFloat
        if let last = entries.last {
            y = last.yOffset + last.height + last.spacingAfter
        } else {
            y = roundToPixel(topPadding)
        }
        let h = roundToPixel(max(0, height))
        let spacing = roundToPixel(max(0, spacingAfter))
        entries.append(Entry(
            id: id,
            yOffset: y,
            height: h,
            spacingAfter: spacing,
            isMeasured: measured,
            minHeight: 0))
        totalHeight = roundToPixel(y + h + spacing + bottomPadding)
        return entries.count - 1
    }

    /// Ratchet the minimum height of an active streaming item so it never shrinks until finalized.
    mutating func setTailRatchet(at index: Int, active: Bool) {
        guard entries.indices.contains(index) else { return }
        entries[index].minHeight = active ? entries[index].height : 0
    }

    /// Update an entry's `spacingAfter`, shifting trailing `yOffset`s and recomputing `totalHeight`.
    mutating func updateSpacingAfter(at index: Int, spacingAfter: CGFloat) {
        guard entries.indices.contains(index) else { return }
        let newSpacing = roundToPixel(max(0, spacingAfter))
        let delta = newSpacing - entries[index].spacingAfter
        guard abs(delta) > 0.0001 else { return }
        entries[index].spacingAfter = newSpacing
        if index + 1 < entries.count {
            for i in (index + 1)..<entries.count {
                entries[i].yOffset += delta
            }
        }
        totalHeight = roundToPixel(max(topPadding + bottomPadding, totalHeight + delta))
    }

    /// Replace an entry's ID/spacing in place (e.g. when a streaming block changes kind)
    /// and mark it unmeasured while preserving its current height until re-measured.
    mutating func replaceEntry(at index: Int, id: String, spacingAfter: CGFloat) {
        guard entries.indices.contains(index) else { return }
        let old = entries[index]
        let newSpacing = roundToPixel(max(0, spacingAfter))
        let spacingDelta = newSpacing - old.spacingAfter
        entries[index] = Entry(
            id: id,
            yOffset: old.yOffset,
            height: old.height,
            spacingAfter: newSpacing,
            isMeasured: false,
            minHeight: old.minHeight)
        if abs(spacingDelta) > 0.0001 {
            if index + 1 < entries.count {
                for i in (index + 1)..<entries.count {
                    entries[i].yOffset += spacingDelta
                }
            }
            totalHeight = roundToPixel(max(topPadding + bottomPadding, totalHeight + spacingDelta))
        }
    }

    /// Remove all entries from `index..<entries.count` and recompute `totalHeight`.
    mutating func removeTail(from index: Int) {
        guard index >= 0, index < entries.count else { return }
        entries.removeSubrange(index..<entries.count)
        if let last = entries.last {
            totalHeight = roundToPixel(last.yOffset + last.height + last.spacingAfter + bottomPadding)
        } else {
            totalHeight = roundToPixel(topPadding + bottomPadding)
        }
    }

    /// Update an item's height, shift trailing `yOffset`s, and compute the exact scroll-origin
    /// compensation needed to keep visible content stationary when above `viewportTop`.
    mutating func updateHeight(
        at index: Int,
        measuredHeight: CGFloat,
        viewportTop: CGFloat,
        isPinnedToBottom: Bool
    ) -> HeightChangeResult {
        guard entries.indices.contains(index) else {
            return HeightChangeResult(oldTotalHeight: totalHeight, newTotalHeight: totalHeight, scrollOriginDelta: 0)
        }
        let oldTotal = totalHeight
        let oldHeight = entries[index].height
        var newHeight = roundToPixel(max(0, measuredHeight))
        if entries[index].minHeight > 0 {
            newHeight = max(newHeight, entries[index].minHeight)
            entries[index].minHeight = newHeight
        }
        entries[index].isMeasured = true

        let delta = newHeight - oldHeight
        guard abs(delta) > 0.0001 else {
            return HeightChangeResult(oldTotalHeight: oldTotal, newTotalHeight: oldTotal, scrollOriginDelta: 0)
        }

        entries[index].height = newHeight
        if index + 1 < entries.count {
            for i in (index + 1)..<entries.count {
                entries[i].yOffset += delta
            }
        }
        totalHeight = roundToPixel(max(topPadding + bottomPadding, oldTotal + delta))

        // Anchor visible content if the resized item's bottom is at or above the viewport top.
        let itemBottomBefore = entries[index].yOffset + oldHeight
        let scrollDelta: CGFloat
        if !isPinnedToBottom && itemBottomBefore <= viewportTop + 0.5 {
            scrollDelta = delta
        } else {
            scrollDelta = 0
        }
        return HeightChangeResult(
            oldTotalHeight: oldTotal,
            newTotalHeight: totalHeight,
            scrollOriginDelta: scrollDelta)
    }

    /// Mark all entries unmeasured (e.g. after width or typography change).
    mutating func invalidateAllMeasurements() {
        for i in entries.indices {
            entries[i].isMeasured = false
            entries[i].minHeight = 0
        }
    }

    /// `O(log N)` binary search returning the contiguous range of items intersecting `rect`.
    func visibleRange(in rect: CGRect) -> Range<Int> {
        guard !entries.isEmpty, rect.height > 0 else { return 0..<0 }
        let minY = rect.minY
        let maxY = rect.maxY

        // Find first index whose bottom (`yOffset + height`) > `minY`.
        var lo = 0
        var hi = entries.count
        while lo < hi {
            let mid = lo + (hi - lo) / 2
            if entries[mid].yOffset + entries[mid].height <= minY {
                lo = mid + 1
            } else {
                hi = mid
            }
        }
        let start = lo
        guard start < entries.count else { return entries.count..<entries.count }

        // Find first index whose top (`yOffset`) >= `maxY`.
        lo = start
        hi = entries.count
        while lo < hi {
            let mid = lo + (hi - lo) / 2
            if entries[mid].yOffset < maxY {
                lo = mid + 1
            } else {
                hi = mid
            }
        }
        return start..<lo
    }

    /// Index of the item whose vertical band contains `y` (or nearest item for drag clamping).
    func itemIndex(atY y: CGFloat) -> Int? {
        guard !entries.isEmpty else { return nil }
        if y <= entries[0].yOffset { return 0 }
        if y >= entries[entries.count - 1].yOffset { return entries.count - 1 }
        var lo = 0
        var hi = entries.count - 1
        while lo < hi {
            let mid = lo + (hi - lo + 1) / 2
            if entries[mid].yOffset <= y {
                lo = mid
            } else {
                hi = mid - 1
            }
        }
        return lo
    }
}

/// Deterministic sticky-bottom state machine for live streaming transcripts.
///
/// Unpins strictly on user intent (live scroll away from bottom, legacy wheel upward,
/// selection drag autoscroll, Find jump, or card expansion) and never writes clip origin
/// while an AppKit live scroll or momentum gesture is active.
struct PCodeStickyBottomState: Equatable {
    enum Mode: Equatable {
        case pinnedToBottom
        case userScrolledUp
    }

    private(set) var mode: Mode = .pinnedToBottom
    private(set) var isLiveScrolling: Bool = false
    var bottomThreshold: CGFloat = 8

    /// True only when programmatic bottom-pinning is safe to execute.
    var shouldProgrammaticallyPin: Bool {
        mode == .pinnedToBottom && !isLiveScrolling
    }

    /// Exact target `clipView.bounds.origin.y` for the bottom of the document,
    /// accounting for `contentInsets` and clip height.
    static func bottomOriginY(
        documentHeight: CGFloat,
        clipHeight: CGFloat,
        topInset: CGFloat = 0,
        bottomInset: CGFloat = 0
    ) -> CGFloat {
        max(-topInset, documentHeight + bottomInset - clipHeight)
    }

    /// Distance from the current `clipOriginY` to the true bottom (`<= 0` means at/past bottom).
    static func distanceFromBottom(
        clipOriginY: CGFloat,
        documentHeight: CGFloat,
        clipHeight: CGFloat,
        topInset: CGFloat = 0,
        bottomInset: CGFloat = 0
    ) -> CGFloat {
        let bottom = bottomOriginY(
            documentHeight: documentHeight,
            clipHeight: clipHeight,
            topInset: topInset,
            bottomInset: bottomInset)
        return bottom - clipOriginY
    }

    mutating func didStartLiveScroll() {
        isLiveScrolling = true
    }

    mutating func didLiveScroll(distanceFromBottom: CGFloat, movedUpward: Bool) {
        if movedUpward && distanceFromBottom > bottomThreshold {
            mode = .userScrolledUp
        }
    }

    mutating func didEndLiveScroll(distanceFromBottom: CGFloat) {
        isLiveScrolling = false
        if distanceFromBottom <= bottomThreshold {
            mode = .pinnedToBottom
        }
    }

    /// Legacy mouse wheels do not always fire `willStartLiveScroll`/`didEndLiveScroll`.
    mutating func didLegacyWheelScroll(distanceFromBottom: CGFloat, movedUpward: Bool) {
        if movedUpward && distanceFromBottom > bottomThreshold {
            mode = .userScrolledUp
        } else if !movedUpward && distanceFromBottom <= bottomThreshold {
            mode = .pinnedToBottom
        }
    }

    /// Unpin immediately for non-scroll user actions (selection autoscroll, Find jump,
    /// expanding a card while pinned).
    mutating func unpinForUserAction() {
        mode = .userScrolledUp
    }

    /// Explicit re-pin (e.g. clicking "Jump to bottom" or submitting a new prompt).
    mutating func pinToBottom() {
        mode = .pinnedToBottom
    }
}
