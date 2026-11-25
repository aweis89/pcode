import Foundation

/// Filesystem directory suggestion and Tab-completion for path queries (`~/` and `/`).
enum PCodePathCompleter {
    struct CompletionResult: Equatable {
        /// The new query string after Tab completion.
        let completed: String
        /// True when completion resolved to a single directory (with trailing `/`).
        let isUnique: Bool
    }

    /// Strip leading whitespace and newlines while preserving trailing spaces
    /// (so queries like `~/Library/Application ` can distinguish `Application Support`).
    private static func cleanQuery(_ query: String) -> String {
        let noNewlines = query.replacingOccurrences(of: "\r", with: "")
            .replacingOccurrences(of: "\n", with: "")
        guard let firstNonSpace = noNewlines.firstIndex(where: { !$0.isWhitespace }) else {
            return ""
        }
        return String(noNewlines[firstNonSpace...])
    }

    /// True when `query` looks like an explicit filesystem path (`/` or `~`).
    static func isPathQuery(_ query: String) -> Bool {
        let cleaned = cleanQuery(query)
        return cleaned.hasPrefix("/") || cleaned.hasPrefix("~")
    }

    /// Format `expandedPath` back into `~/` form when `preserveTilde` is true and the path is under `home`.
    static func formatPath(
        _ expandedPath: String,
        home: String = PCodeWorkspace.normalizePath(NSHomeDirectory()),
        preserveTilde: Bool,
        trailingSlash: Bool = false
    ) -> String {
        let normalized = PCodeWorkspace.normalizePath(expandedPath)
        let base: String
        if preserveTilde {
            if normalized == home {
                base = "~"
            } else if normalized.hasPrefix(home + "/") {
                base = "~" + normalized.dropFirst(home.count)
            } else {
                base = normalized
            }
        } else {
            base = normalized
        }
        guard trailingSlash else { return base }
        return base.hasSuffix("/") ? base : base + "/"
    }

    /// Split a path query into its expanded parent directory and the typed child prefix.
    static func split(
        _ query: String,
        home: String = PCodeWorkspace.normalizePath(NSHomeDirectory())
    ) -> (parent: String, prefix: String, preserveTilde: Bool, isBareTilde: Bool)? {
        let cleaned = cleanQuery(query)
        guard cleaned.hasPrefix("/") || cleaned.hasPrefix("~") else { return nil }

        let preserveTilde = cleaned.hasPrefix("~")
        if cleaned == "~" {
            return (home, "", true, true)
        }

        let expanded: String
        if cleaned.hasPrefix("~/") {
            expanded = home + String(cleaned.dropFirst(1))
        } else if cleaned.hasPrefix("~") {
            expanded = (cleaned as NSString).expandingTildeInPath
        } else {
            expanded = cleaned
        }

        // Unexpanded `~unknown` has no leading `/` and should not probe `/~unknown`.
        guard expanded.hasPrefix("/") else { return nil }

        if expanded.hasSuffix("/") {
            let parent = PCodeWorkspace.normalizePath(expanded)
            return (parent, "", preserveTilde, false)
        }

        let ns = expanded as NSString
        let parent = PCodeWorkspace.normalizePath(ns.deletingLastPathComponent)
        let prefix = ns.lastPathComponent
        return (parent, prefix, preserveTilde, false)
    }

    /// List immediate child directories of `parent` whose names match `prefix` (case-insensitive).
    /// Hidden directories (starting with `.`) are skipped unless `prefix` starts with `.`.
    static func matchingSubdirectories(
        in parent: String,
        prefix: String,
        limit: Int = 50,
        fileManager: FileManager = .default
    ) -> [String] {
        guard let names = try? fileManager.contentsOfDirectory(atPath: parent) else { return [] }
        let showHidden = prefix.hasPrefix(".")
        let foldedPrefix = prefix.lowercased()

        var matches: [(name: String, path: String)] = []
        for name in names {
            if !showHidden && name.hasPrefix(".") { continue }
            if !foldedPrefix.isEmpty && !name.lowercased().hasPrefix(foldedPrefix) { continue }
            let full = parent == "/" ? "/\(name)" : "\(parent)/\(name)"
            var isDir: ObjCBool = false
            guard fileManager.fileExists(atPath: full, isDirectory: &isDir), isDir.boolValue else { continue }
            matches.append((name, full))
        }
        matches.sort { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
        if matches.count > limit {
            return Array(matches.prefix(limit).map(\.path))
        }
        return matches.map(\.path)
    }

    /// Directory suggestions to show in the Command Center for a path query.
    ///
    /// Preserves canonical on-disk casing (avoiding duplicate rows on case-insensitive APFS)
    /// and places any exact directory match first, followed by matching subdirectories.
    static func directorySuggestions(
        for query: String,
        home: String = PCodeWorkspace.normalizePath(NSHomeDirectory()),
        limit: Int = 40,
        fileManager: FileManager = .default
    ) -> [String] {
        guard let parts = split(query, home: home) else { return [] }
        var results: [String] = []
        var seen: Set<String> = []

        // When typing a child name (not `.` or `..`), pull canonical names from `parent`
        // so case-insensitive filesystems never produce both `desktop` and `Desktop`.
        if !parts.isBareTilde && !parts.prefix.isEmpty && parts.prefix != "." && parts.prefix != ".." {
            var childMatches = matchingSubdirectories(
                in: parts.parent,
                prefix: parts.prefix,
                limit: limit,
                fileManager: fileManager
            )
            if let exactIdx = childMatches.firstIndex(where: {
                ($0 as NSString).lastPathComponent.compare(parts.prefix, options: .caseInsensitive) == .orderedSame
            }) {
                let exact = childMatches.remove(at: exactIdx)
                childMatches.insert(exact, at: 0)
            }
            return childMatches
        }

        let exactPath: String
        if parts.isBareTilde || parts.prefix.isEmpty {
            exactPath = parts.parent
        } else {
            exactPath = parts.parent == "/" ? "/\(parts.prefix)" : "\(parts.parent)/\(parts.prefix)"
        }
        let normalizedExact = PCodeWorkspace.normalizePath(exactPath)
        var isDir: ObjCBool = false
        if fileManager.fileExists(atPath: normalizedExact, isDirectory: &isDir), isDir.boolValue {
            if seen.insert(normalizedExact).inserted {
                results.append(normalizedExact)
            }
        }

        let childMatches = matchingSubdirectories(
            in: parts.parent,
            prefix: parts.prefix,
            limit: limit,
            fileManager: fileManager
        )
        for path in childMatches where seen.insert(path).inserted {
            results.append(path)
            if results.count >= limit { break }
        }
        return results
    }

    /// Compute the next query string when the user presses Tab.
    ///
    /// - For a non-path query, fills the selected project's `~/` path + `"/"` so you can drill in.
    /// - For bare `"~"`, completes to `"~/"`.
    /// - For a single matching subdirectory, completes to that directory + `"/"`.
    /// - For multiple matches, completes to their longest common prefix if longer than `prefix`;
    ///   otherwise completes to `selectedPath + "/"` so repeated Tab moves into the highlighted row.
    static func complete(
        query: String,
        selectedPath: String? = nil,
        home: String = PCodeWorkspace.normalizePath(NSHomeDirectory()),
        fileManager: FileManager = .default
    ) -> CompletionResult? {
        let cleaned = cleanQuery(query)

        if !isPathQuery(cleaned) {
            guard let selectedPath, !selectedPath.isEmpty else { return nil }
            let formatted = formatPath(selectedPath, home: home, preserveTilde: true, trailingSlash: true)
            return CompletionResult(completed: formatted, isUnique: true)
        }

        guard let parts = split(cleaned, home: home) else { return nil }
        if parts.isBareTilde {
            return CompletionResult(completed: "~/", isUnique: true)
        }

        let matches = matchingSubdirectories(
            in: parts.parent,
            prefix: parts.prefix,
            limit: .max,
            fileManager: fileManager
        )

        if matches.count == 1 {
            let formatted = formatPath(
                matches[0],
                home: home,
                preserveTilde: parts.preserveTilde,
                trailingSlash: true
            )
            return CompletionResult(completed: formatted, isUnique: true)
        }

        if matches.count > 1 {
            let names = matches.map { ($0 as NSString).lastPathComponent }
            let lcp = longestCommonPrefix(names)
            if lcp.count > parts.prefix.count {
                let full = parts.parent == "/" ? "/\(lcp)" : "\(parts.parent)/\(lcp)"
                let formatted = formatPath(
                    full,
                    home: home,
                    preserveTilde: parts.preserveTilde,
                    trailingSlash: false
                )
                return CompletionResult(completed: formatted, isUnique: false)
            }
            if let selectedPath, !selectedPath.isEmpty {
                let normalizedSelected = PCodeWorkspace.normalizePath(selectedPath)
                let formatted = formatPath(
                    normalizedSelected,
                    home: home,
                    preserveTilde: parts.preserveTilde,
                    trailingSlash: true
                )
                if formatted != cleaned {
                    return CompletionResult(completed: formatted, isUnique: true)
                }
            }
            return nil
        }

        return nil
    }

    /// Case-insensitive longest common prefix of `strings`, preserving the casing of the first string.
    static func longestCommonPrefix(_ strings: [String]) -> String {
        guard let first = strings.first else { return "" }
        let chars = Array(first)
        var length = chars.count
        for str in strings.dropFirst() {
            let other = Array(str)
            length = min(length, other.count)
            var i = 0
            while i < length && String(chars[i]).lowercased() == String(other[i]).lowercased() {
                i += 1
            }
            length = i
            if length == 0 { break }
        }
        return String(chars.prefix(length))
    }
}
