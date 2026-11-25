import Foundation

/// Model-owned, cross-item text selection for `PCodeTranscriptView`.
///
/// Storing selection as `(itemIndex, utf16Offset)` decouples selection state from
/// recycled `NSView` instances so scrolling offscreen or streaming into the tail
/// never drops the user's selection.
struct PCodeTranscriptSelection: Equatable {
    struct Position: Equatable, Comparable {
        let itemIndex: Int
        let utf16Offset: Int

        static func < (lhs: Position, rhs: Position) -> Bool {
            if lhs.itemIndex != rhs.itemIndex {
                return lhs.itemIndex < rhs.itemIndex
            }
            return lhs.utf16Offset < rhs.utf16Offset
        }
    }

    var anchor: Position
    var focus: Position

    var lower: Position { min(anchor, focus) }
    var upper: Position { max(anchor, focus) }
    var isEmpty: Bool { anchor == focus }

    /// Local UTF-16 `NSRange` selected inside item `index` (whose text has `utf16Length`),
    /// or `nil` if the item is outside the selection.
    func localRange(forItem index: Int, utf16Length: Int) -> NSRange? {
        guard !isEmpty, utf16Length >= 0 else { return nil }
        let start = lower
        let end = upper
        guard index >= start.itemIndex && index <= end.itemIndex else { return nil }

        let loc = (index == start.itemIndex) ? min(max(0, start.utf16Offset), utf16Length) : 0
        let endLoc = (index == end.itemIndex) ? min(max(0, end.utf16Offset), utf16Length) : utf16Length
        guard endLoc > loc else { return nil }
        return NSRange(location: loc, length: endLoc - loc)
    }

    /// Extract plain text across all selected items, joined with blank lines between items.
    func extractText(itemCount: Int, textForItem: (Int) -> String) -> String {
        guard !isEmpty, itemCount > 0 else { return "" }
        let startIdx = max(0, lower.itemIndex)
        let endIdx = min(itemCount - 1, upper.itemIndex)
        guard startIdx <= endIdx else { return "" }

        var parts: [String] = []
        parts.reserveCapacity(endIdx - startIdx + 1)
        for i in startIdx...endIdx {
            let full = textForItem(i) as NSString
            guard let range = localRange(forItem: i, utf16Length: full.length) else { continue }
            parts.append(full.substring(with: range))
        }
        return parts.joined(separator: "\n\n")
    }
}
