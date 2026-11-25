import Foundation

/// How a pcode pick list filters, ported from `pcode.pick_ui` and
/// `pcode.popup_ui.fuzzy_match` so the native list matches what pcode's own
/// terminal list would. pcode pins the shared cases in
/// `tests/data/pick_fuzzy_cases.json`, which `PCodePickMatchTests` reads too.
///
/// pcode sends each item's search texts casefolded already, so only the query
/// is folded here.
enum PCodePickMatch {
    /// A casefolded substring, or joined word prefixes: `ed_ui` finds `edit_ui.py`.
    ///
    /// Gaps are allowed between words, not inside them. Words are runs of
    /// ASCII lowercase letters and digits, as pcode's `[a-z0-9]+`.
    static func fuzzy(_ term: String, _ text: String) -> Bool {
        let termScalars = Array(term.unicodeScalars)
        let textScalars = Array(text.unicodeScalars)
        if contains(textScalars, termScalars) { return true }
        // Separators in the query are word boundaries, so ed_ui and ed/ui both find edit_ui.
        let joined = words(termScalars).flatMap { $0 }
        if joined.isEmpty { return false }
        // How much of the query successive word prefixes can consume.
        var positions: Set<Int> = [0]
        for word in words(textScalars) {
            var following = positions  // Skipping a word is allowed.
            for position in positions {
                for (offset, character) in word.enumerated() {
                    let index = position + offset
                    if index >= joined.count || character != joined[index] { break }
                    following.insert(index + 1)
                }
            }
            if following.contains(joined.count) { return true }
            positions = following
        }
        return false
    }

    /// Python's `needle in haystack`, by code point.
    static func contains(_ haystack: [Unicode.Scalar], _ needle: [Unicode.Scalar]) -> Bool {
        if needle.isEmpty { return true }
        if needle.count > haystack.count { return false }
        for start in 0...(haystack.count - needle.count)
        where haystack[start..<(start + needle.count)].elementsEqual(needle) {
            return true
        }
        return false
    }

    private static func words(_ scalars: [Unicode.Scalar]) -> [[Unicode.Scalar]] {
        var result: [[Unicode.Scalar]] = []
        var current: [Unicode.Scalar] = []
        for scalar in scalars {
            if ("a"..."z").contains(scalar) || ("0"..."9").contains(scalar) {
                current.append(scalar)
            } else if !current.isEmpty {
                result.append(current)
                current = []
            }
        }
        if !current.isEmpty { result.append(current) }
        return result
    }

    /// The query's words, casefolded.
    static func terms(_ query: String) -> [String] {
        casefold(query).split(whereSeparator: \.isWhitespace).map(String.init)
    }

    /// Python's `str.casefold()` for the cases that differ from lowercasing in
    /// practice: ß and ẞ fold to "ss", final sigma to σ. pcode pins these in
    /// the shared fixture's `casefold` table.
    static func casefold(_ text: String) -> String {
        var folded = ""
        for scalar in text.lowercased().unicodeScalars {
            switch scalar {
            case "ß": folded += "ss"
            case "ς": folded += "σ"
            default: folded.unicodeScalars.append(scalar)
            }
        }
        return folded
    }

    /// Every word matches one of `texts`, fuzzily or as a substring.
    static func matches(_ texts: [String], terms: [String], fuzzy useFuzzy: Bool) -> Bool {
        terms.allSatisfy { term in
            texts.contains { text in
                useFuzzy ? fuzzy(term, text) : contains(Array(text.unicodeScalars), Array(term.unicodeScalars))
            }
        }
    }

    /// True if `pattern` matches all of the trimmed query (pcode's `re.fullmatch`).
    static func fullMatch(_ pattern: NSRegularExpression, _ query: String) -> Bool {
        let range = NSRange(query.startIndex..., in: query)
        return pattern.firstMatch(in: query, range: range) != nil
    }

    /// `pattern` anchored at both ends, as pcode's `re.fullmatch` reads it.
    static func anchored(_ pattern: String) -> NSRegularExpression? {
        try? NSRegularExpression(pattern: "^(?:\(pattern))\\z")
    }

    /// Tree guides for rows listed depth-first, as pcode's `nest_forks` draws them.
    static func treeGuides(_ depths: [Int]) -> [String] {
        var levels: [Int] = []
        for depth in depths {
            levels.append(max(0, min(depth, levels.last.map { $0 + 1 } ?? 0)))
        }
        func continues(_ index: Int, _ level: Int) -> Bool {
            for later in levels[(index + 1)...] where later <= level {
                return later == level
            }
            return false
        }
        return levels.enumerated().map { index, depth in
            guard depth > 0 else { return "" }
            return (1...depth).map { level in
                let more = continues(index, level)
                if level == depth { return more ? "├─ " : "└─ " }
                return more ? "│  " : "   "
            }.joined()
        }
    }
}
