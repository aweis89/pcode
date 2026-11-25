import AppKit
import Foundation

/// Flat UTF-16 search index mapping contiguous character offsets across virtualized
/// `PCodeTranscriptItem`s for `NSTextFinderClient` (`⌘F`, `⌘G`, `⌘⇧G`).
struct PCodeTranscriptSearchIndex: Equatable {
    struct Entry: Equatable {
        let itemIndex: Int
        /// Start offset in the flat UTF-16 search buffer.
        let utf16Location: Int
        /// UTF-16 length of this item's plain text (excluding the 1-char `"\n"` separator).
        let utf16Length: Int

        var utf16End: Int { utf16Location + utf16Length }
    }

    let entries: [Entry]
    let totalUTF16Length: Int

    init(itemUTF16Lengths: [Int]) {
        var built: [Entry] = []
        built.reserveCapacity(itemUTF16Lengths.count)
        var cursor = 0
        for (idx, len) in itemUTF16Lengths.enumerated() {
            let clamped = max(0, len)
            built.append(Entry(itemIndex: idx, utf16Location: cursor, utf16Length: clamped))
            cursor += clamped
            if idx + 1 < itemUTF16Lengths.count {
                cursor += 1 // single "\n" boundary separator
            }
        }
        self.entries = built
        self.totalUTF16Length = cursor
    }

    /// `O(log N)` lookup of the entry owning `flatOffset` (or the preceding entry if `flatOffset`
    /// lands on the `"\n"` separator between items).
    func entryIndex(for flatOffset: Int) -> Int? {
        guard !entries.isEmpty, flatOffset >= 0, flatOffset < totalUTF16Length else { return nil }
        var lo = 0
        var hi = entries.count - 1
        while lo < hi {
            let mid = lo + (hi - lo + 1) / 2
            if entries[mid].utf16Location <= flatOffset {
                lo = mid
            } else {
                hi = mid - 1
            }
        }
        return lo
    }

    /// Convert a flat UTF-16 `NSRange` into a model-owned `PCodeTranscriptSelection`.
    func selection(for flatRange: NSRange) -> PCodeTranscriptSelection? {
        guard !entries.isEmpty, flatRange.location != NSNotFound else { return nil }
        let clampedStart = min(max(0, flatRange.location), max(0, totalUTF16Length))
        let clampedEnd = min(max(clampedStart, NSMaxRange(flatRange)), max(0, totalUTF16Length))
        guard let startPos = position(forFlatOffset: clampedStart),
              let endPos = position(forFlatOffset: clampedEnd)
        else { return nil }
        return PCodeTranscriptSelection(anchor: startPos, focus: endPos)
    }

    /// Convert a model-owned `PCodeTranscriptSelection` into a flat UTF-16 `NSRange`.
    func flatRange(for selection: PCodeTranscriptSelection) -> NSRange {
        guard !entries.isEmpty else { return NSRange(location: 0, length: 0) }
        let start = flatOffset(for: selection.lower)
        let end = flatOffset(for: selection.upper)
        return NSRange(location: start, length: max(0, end - start))
    }

    func position(forFlatOffset flatOffset: Int) -> PCodeTranscriptSelection.Position? {
        guard !entries.isEmpty else { return nil }
        if flatOffset <= 0 {
            return .init(itemIndex: 0, utf16Offset: 0)
        }
        if flatOffset >= totalUTF16Length {
            let last = entries[entries.count - 1]
            return .init(itemIndex: last.itemIndex, utf16Offset: last.utf16Length)
        }
        guard let idx = entryIndex(for: flatOffset) else { return nil }
        let entry = entries[idx]
        let local = min(max(0, flatOffset - entry.utf16Location), entry.utf16Length)
        return .init(itemIndex: entry.itemIndex, utf16Offset: local)
    }

    func flatOffset(for pos: PCodeTranscriptSelection.Position) -> Int {
        guard !entries.isEmpty else { return 0 }
        let idx = min(max(0, pos.itemIndex), entries.count - 1)
        let entry = entries[idx]
        return entry.utf16Location + min(max(0, pos.utf16Offset), entry.utf16Length)
    }
}

// MARK: - NSTextFinderClient (In-Transcript Find: ⌘F / ⌘G / ⌘⇧G)

extension PCodeTranscriptDocumentView: NSTextFinderClient {
    var isSelectable: Bool { true }
    var allowsMultipleSelection: Bool { false }

    func currentSearchIndex() -> PCodeTranscriptSearchIndex {
        if let cached = cachedSearchIndex {
            return cached
        }
        var lengths: [Int] = []
        lengths.reserveCapacity(items.count)
        for idx in items.indices {
            lengths.append((plainTextForCopy(at: idx) as NSString).length)
        }
        let built = PCodeTranscriptSearchIndex(itemUTF16Lengths: lengths)
        cachedSearchIndex = built
        return built
    }

    func stringLength() -> Int {
        currentSearchIndex().totalUTF16Length
    }

    func string(
        at characterIndex: Int,
        effectiveRange outRange: NSRangePointer,
        endsWithSearchBoundary outFlag: UnsafeMutablePointer<ObjCBool>
    ) -> String {
        let index = currentSearchIndex()
        guard let entryIdx = index.entryIndex(for: characterIndex) else {
            outRange.pointee = NSRange(location: characterIndex, length: 0)
            outFlag.pointee = true
            return ""
        }
        let entry = index.entries[entryIdx]
        if characterIndex < entry.utf16End {
            outRange.pointee = NSRange(location: entry.utf16Location, length: entry.utf16Length)
            outFlag.pointee = true
            return plainTextForCopy(at: entry.itemIndex)
        }
        // Single "\n" boundary separator between adjacent items.
        outRange.pointee = NSRange(location: entry.utf16End, length: 1)
        outFlag.pointee = true
        return "\n"
    }

    var firstSelectedRange: NSRange {
        guard let selection else { return NSRange(location: 0, length: 0) }
        return currentSearchIndex().flatRange(for: selection)
    }

    var selectedRanges: [NSValue] {
        get {
            [NSValue(range: firstSelectedRange)]
        }
        set {
            guard let first = newValue.first?.rangeValue, first.length > 0 else {
                setSelection(nil)
                return
            }
            if let sel = currentSearchIndex().selection(for: first) {
                setSelection(sel)
            }
        }
    }

    func scrollRangeToVisible(_ range: NSRange) {
        let index = currentSearchIndex()
        guard let pos = index.position(forFlatOffset: range.location) else { return }
        scrollItemToVisible(at: pos.itemIndex)
    }

    func contentView(at index: Int, effectiveCharacterRange outRange: NSRangePointer) -> NSView {
        let searchIndex = currentSearchIndex()
        if let entryIdx = searchIndex.entryIndex(for: index) {
            let entry = searchIndex.entries[entryIdx]
            outRange.pointee = NSRange(location: entry.utf16Location, length: max(1, entry.utf16Length))
        } else {
            outRange.pointee = NSRange(location: 0, length: searchIndex.totalUTF16Length)
        }
        return self
    }

    func rects(forCharacterRange range: NSRange) -> [NSValue]? {
        let searchIndex = currentSearchIndex()
        guard let sel = searchIndex.selection(for: range) else { return nil }
        var rects: [NSValue] = []
        for idx in sel.lower.itemIndex...sel.upper.itemIndex where items.indices.contains(idx) {
            let itemLen = (plainTextForCopy(at: idx) as NSString).length
            guard let local = sel.localRange(forItem: idx, utf16Length: itemLen), local.length > 0 else { continue }
            for rect in characterRects(forItemAt: idx, localRange: local) {
                rects.append(NSValue(rect: rect))
            }
        }
        return rects.isEmpty ? nil : rects
    }

    @objc func performFindPanelAction(_ sender: Any?) {
        guard let finder = textFinder else { return }
        let tag: Int
        if let item = sender as? NSMenuItem {
            tag = item.tag
        } else if let control = sender as? NSControl {
            tag = control.tag
        } else {
            tag = NSTextFinder.Action.showFindInterface.rawValue
        }
        guard let action = NSTextFinder.Action(rawValue: tag),
              finder.validateAction(action)
        else { return }
        finder.performAction(action)
    }
}

// MARK: - Accessibility Proxies (`NSAccessibilityElement`)

/// Lightweight VoiceOver proxy for one visible row in `PCodeTranscriptDocumentView`.
final class PCodeTranscriptAccessibilityRowElement: NSAccessibilityElement {
    private weak var documentView: PCodeTranscriptDocumentView?
    let itemID: String
    let itemIndex: Int
    let isExpandable: Bool

    init(documentView: PCodeTranscriptDocumentView, itemID: String, itemIndex: Int, isExpandable: Bool) {
        self.documentView = documentView
        self.itemID = itemID
        self.itemIndex = itemIndex
        self.isExpandable = isExpandable
        super.init()
    }

    override func accessibilityParent() -> Any? {
        documentView
    }

    override func accessibilityFrame() -> NSRect {
        guard let doc = documentView,
              doc.layoutEngine.entries.indices.contains(itemIndex),
              let window = doc.window
        else { return .zero }
        let entry = doc.layoutEngine.entries[itemIndex]
        let localRect = CGRect(
            x: PCodeTranscriptDocumentView.horizontalPadding,
            y: entry.yOffset,
            width: max(1, doc.bounds.width - PCodeTranscriptDocumentView.horizontalPadding * 2),
            height: entry.height)
        let windowRect = doc.convert(localRect, to: nil)
        return window.convertToScreen(windowRect)
    }

    override func accessibilityPerformPress() -> Bool {
        guard isExpandable, let doc = documentView else { return false }
        doc.toggleExpansionForAccessibility(id: itemID)
        return true
    }
}

extension PCodeTranscriptDocumentView {
    override func isAccessibilityElement() -> Bool {
        false
    }

    override func accessibilityRole() -> NSAccessibility.Role? {
        .list
    }

    override func accessibilityLabel() -> String? {
        "Conversation transcript"
    }

    override func accessibilityChildren() -> [Any]? {
        guard !items.isEmpty else { return [] }
        let range = layoutEngine.visibleRange(in: visibleRect)
        let indices: Range<Int> = range.isEmpty ? (0..<min(items.count, 16)) : range
        var children: [PCodeTranscriptAccessibilityRowElement] = []
        children.reserveCapacity(indices.count)
        for idx in indices where items.indices.contains(idx) {
            let item = items[idx]
            let (role, roleDesc, label, expandable) = accessibilityMetadata(for: item)
            let element = PCodeTranscriptAccessibilityRowElement(
                documentView: self,
                itemID: item.id,
                itemIndex: idx,
                isExpandable: expandable)
            element.setAccessibilityRole(role)
            element.setAccessibilityRoleDescription(roleDesc)
            element.setAccessibilityLabel(label)
            element.setAccessibilityValue(plainTextForCopy(at: idx))
            children.append(element)
        }
        return children
    }

    private func accessibilityMetadata(
        for item: PCodeTranscriptItem
    ) -> (role: NSAccessibility.Role, roleDescription: String, label: String, expandable: Bool) {
        switch item.payload {
        case .prompt(let label, _, _):
            return (.staticText, "Prompt", label, false)
        case .prose:
            return (.staticText, "Response", "Assistant response", false)
        case .codeBlock(let lang, _, _, _, _):
            let name = (lang?.isEmpty == false) ? "\(lang!) code block" : "Code block"
            return (.group, "Code block", name, false)
        case .table:
            return (.table, "Table", "Table", false)
        case .tool(let call, let isExpanded):
            let state = isExpanded ? "expanded" : "collapsed"
            let expandable = call.summaryText.count > 90 || call.summaryText.contains("\n") || !call.output.isEmpty
            return (.group, "Tool call", "\(call.name) (\(state))", expandable)
        case .diff(let payload):
            return (.group, "Diff", "Diff for \(payload.path)", false)
        case .thinking(_, let isExpanded):
            let state = isExpanded ? "expanded" : "collapsed"
            return (.group, "Thinking", "Thinking (\(state))", true)
        case .plan(let planItems):
            return (.group, "Plan", "Plan (\(planItems.count) steps)", false)
        case .status(let status):
            return (.staticText, "Status", PCodeStatusRowView.displayLabel(for: status), false)
        }
    }
}
