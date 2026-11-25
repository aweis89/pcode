import AppKit
import Foundation
import JavaScriptCore

/// One syntax-highlighted token produced by a `PCodeSyntaxHighlighter`.
///
/// Token arrays (not colors) are cached so light/dark theme switches re-color
/// instantaneously without re-tokenizing.
struct PCodeSyntaxToken: Equatable, Hashable, Sendable {
    enum Kind: String, Equatable, Hashable, Sendable {
        case plain
        case comment
        case keyword
        case string
        case number
        case type
        case function
        case property
        case `operator`
        case punctuation
        case tag
        case attribute
        case builtin
        case boolean
        case constant
        case variable
        case regex
        case inserted
        case deleted

        init(prismType: String) {
            switch prismType {
            case "comment", "prolog", "doctype", "cdata", "doc-comment":
                self = .comment
            case "keyword", "atrule", "rule", "important", "directive":
                self = .keyword
            case "string", "char", "template-string", "url":
                self = .string
            case "number":
                self = .number
            case "class-name", "namespace", "type-definition":
                self = .type
            case "function", "function-variable", "method":
                self = .function
            case "property", "symbol", "macro":
                self = .property
            case "operator":
                self = .operator
            case "punctuation":
                self = .punctuation
            case "tag", "selector":
                self = .tag
            case "attr-name", "attr-value", "decorator":
                self = .attribute
            case "builtin":
                self = .builtin
            case "boolean":
                self = .boolean
            case "constant":
                self = .constant
            case "variable", "parameter":
                self = .variable
            case "regex":
                self = .regex
            case "inserted":
                self = .inserted
            case "deleted":
                self = .deleted
            default:
                self = .plain
            }
        }
    }

    let content: String
    let kind: Kind
}

/// Native AppKit light/dark syntax highlighting color palette.
struct PCodeSyntaxPalette: Equatable, Sendable {
    let isDark: Bool
    let plain: NSColor
    let comment: NSColor
    let keyword: NSColor
    let string: NSColor
    let number: NSColor
    let type: NSColor
    let function: NSColor
    let property: NSColor
    let `operator`: NSColor
    let punctuation: NSColor
    let tag: NSColor
    let attribute: NSColor
    let builtin: NSColor
    let boolean: NSColor
    let constant: NSColor
    let variable: NSColor
    let regex: NSColor
    let inserted: NSColor
    let deleted: NSColor

    func color(for kind: PCodeSyntaxToken.Kind) -> NSColor {
        switch kind {
        case .plain: return plain
        case .comment: return comment
        case .keyword: return keyword
        case .string: return string
        case .number: return number
        case .type: return type
        case .function: return function
        case .property: return property
        case .operator: return `operator`
        case .punctuation: return punctuation
        case .tag: return tag
        case .attribute: return attribute
        case .builtin: return builtin
        case .boolean: return boolean
        case .constant: return constant
        case .variable: return variable
        case .regex: return regex
        case .inserted: return inserted
        case .deleted: return deleted
        }
    }

    static func make(isDark: Bool, foreground: NSColor) -> PCodeSyntaxPalette {
        if isDark {
            return PCodeSyntaxPalette(
                isDark: true,
                plain: foreground,
                comment: NSColor(srgbRed: 0.54, green: 0.60, blue: 0.66, alpha: 1.0),
                keyword: NSColor(srgbRed: 0.99, green: 0.48, blue: 0.64, alpha: 1.0),
                string: NSColor(srgbRed: 0.65, green: 0.85, blue: 0.55, alpha: 1.0),
                number: NSColor(srgbRed: 0.82, green: 0.72, blue: 0.98, alpha: 1.0),
                type: NSColor(srgbRed: 0.45, green: 0.82, blue: 0.94, alpha: 1.0),
                function: NSColor(srgbRed: 0.47, green: 0.72, blue: 0.99, alpha: 1.0),
                property: NSColor(srgbRed: 0.55, green: 0.84, blue: 0.86, alpha: 1.0),
                operator: NSColor(srgbRed: 0.78, green: 0.82, blue: 0.88, alpha: 1.0),
                punctuation: foreground.withAlphaComponent(0.72),
                tag: NSColor(srgbRed: 0.99, green: 0.48, blue: 0.64, alpha: 1.0),
                attribute: NSColor(srgbRed: 0.96, green: 0.75, blue: 0.48, alpha: 1.0),
                builtin: NSColor(srgbRed: 0.78, green: 0.66, blue: 0.98, alpha: 1.0),
                boolean: NSColor(srgbRed: 0.96, green: 0.66, blue: 0.44, alpha: 1.0),
                constant: NSColor(srgbRed: 0.96, green: 0.66, blue: 0.44, alpha: 1.0),
                variable: foreground,
                regex: NSColor(srgbRed: 0.95, green: 0.74, blue: 0.46, alpha: 1.0),
                inserted: NSColor(srgbRed: 0.52, green: 0.87, blue: 0.56, alpha: 1.0),
                deleted: NSColor(srgbRed: 0.98, green: 0.50, blue: 0.50, alpha: 1.0))
        } else {
            return PCodeSyntaxPalette(
                isDark: false,
                plain: foreground,
                comment: NSColor(srgbRed: 0.42, green: 0.47, blue: 0.52, alpha: 1.0),
                keyword: NSColor(srgbRed: 0.69, green: 0.11, blue: 0.46, alpha: 1.0),
                string: NSColor(srgbRed: 0.14, green: 0.50, blue: 0.24, alpha: 1.0),
                number: NSColor(srgbRed: 0.11, green: 0.35, blue: 0.78, alpha: 1.0),
                type: NSColor(srgbRed: 0.08, green: 0.45, blue: 0.58, alpha: 1.0),
                function: NSColor(srgbRed: 0.36, green: 0.20, blue: 0.72, alpha: 1.0),
                property: NSColor(srgbRed: 0.14, green: 0.44, blue: 0.50, alpha: 1.0),
                operator: NSColor(srgbRed: 0.28, green: 0.32, blue: 0.38, alpha: 1.0),
                punctuation: foreground.withAlphaComponent(0.68),
                tag: NSColor(srgbRed: 0.69, green: 0.11, blue: 0.46, alpha: 1.0),
                attribute: NSColor(srgbRed: 0.64, green: 0.36, blue: 0.05, alpha: 1.0),
                builtin: NSColor(srgbRed: 0.44, green: 0.22, blue: 0.72, alpha: 1.0),
                boolean: NSColor(srgbRed: 0.68, green: 0.32, blue: 0.06, alpha: 1.0),
                constant: NSColor(srgbRed: 0.68, green: 0.32, blue: 0.06, alpha: 1.0),
                variable: foreground,
                regex: NSColor(srgbRed: 0.72, green: 0.28, blue: 0.12, alpha: 1.0),
                inserted: NSColor(srgbRed: 0.11, green: 0.50, blue: 0.22, alpha: 1.0),
                deleted: NSColor(srgbRed: 0.76, green: 0.14, blue: 0.16, alpha: 1.0))
        }
    }
}

/// Pluggable syntax highlighter abstraction (backed by off-main Prism in `JavaScriptCore`).
protocol PCodeSyntaxHighlighter: AnyObject, Sendable {
    func cachedTokens(code: String, language: String?) -> [PCodeSyntaxToken]?
    func tokenize(code: String, language: String?) async -> [PCodeSyntaxToken]
}

extension PCodeSyntaxHighlighter {
    /// Normalize common Markdown fence language tags into Prism grammar names.
    static func normalizeLanguage(_ raw: String?) -> String {
        guard let raw else { return "plain" }
        let cleaned = raw
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
            .split(whereSeparator: { $0 == " " || $0 == "{" || $0 == "," })
            .first
            .map(String.init) ?? ""
        switch cleaned {
        case "", "text", "txt", "plaintext", "plain": return "plain"
        case "py", "py3", "python3": return "python"
        case "js", "mjs", "cjs", "node": return "javascript"
        case "ts", "mts", "cts": return "typescript"
        case "sh", "shell", "zsh", "fish", "console": return "bash"
        case "yml": return "yaml"
        case "rb": return "ruby"
        case "rs": return "rust"
        case "md": return "markdown"
        case "c++", "cc", "cxx", "hpp", "hh": return "cpp"
        case "h": return "c"
        case "objc", "obj-c", "m", "mm": return "objectivec"
        case "cs": return "csharp"
        case "kt", "kts": return "kotlin"
        case "tf", "tfvars": return "hcl"
        case "dockerfile": return "docker"
        case "patch": return "diff"
        default: return cleaned
        }
    }

    /// Render a cached token array into a styled `NSAttributedString`.
    static func attributedString(
        from tokens: [PCodeSyntaxToken],
        font: NSFont,
        palette: PCodeSyntaxPalette,
        lineHeight: CGFloat
    ) -> NSAttributedString {
        let paragraph = NSMutableParagraphStyle()
        paragraph.minimumLineHeight = lineHeight
        paragraph.maximumLineHeight = lineHeight
        paragraph.lineBreakMode = .byClipping

        let out = NSMutableAttributedString()
        out.beginEditing()
        for token in tokens {
            let attrs: [NSAttributedString.Key: Any] = [
                .font: font,
                .foregroundColor: palette.color(for: token.kind),
                .paragraphStyle: paragraph,
            ]
            out.append(NSAttributedString(string: token.content, attributes: attrs))
        }
        out.endEditing()
        return out
    }

    /// Render unhighlighted plain code immediately while async tokenization runs.
    static func plainAttributedString(
        code: String,
        font: NSFont,
        foreground: NSColor,
        lineHeight: CGFloat
    ) -> NSAttributedString {
        let paragraph = NSMutableParagraphStyle()
        paragraph.minimumLineHeight = lineHeight
        paragraph.maximumLineHeight = lineHeight
        paragraph.lineBreakMode = .byClipping
        return NSAttributedString(
            string: code,
            attributes: [
                .font: font,
                .foregroundColor: foreground,
                .paragraphStyle: paragraph,
            ])
    }
}

/// Actor isolating the `JSContext` running Prism.js off the main thread.
private actor PCodePrismJSRuntime {
    private var context: JSContext?
    private var loaded = false

    private func ensureLoaded() -> JSContext? {
        if loaded { return context }
        loaded = true
        guard let scriptURL = Self.locatePrismBundle(),
              let script = try? String(contentsOf: scriptURL, encoding: .utf8),
              let ctx = JSContext()
        else {
            return nil
        }
        ctx.evaluateScript(script)
        context = ctx
        return ctx
    }

    private static func locatePrismBundle() -> URL? {
        // 1. Branded Pcode.app bundle (`Contents/Resources/prism-bundle.js`).
        if let url = Bundle.main.url(forResource: "prism-bundle", withExtension: "js") {
            return url
        }
        // 2. Fallback to Textual's resource bundle during unbranded `xcodebuild test`.
        if let resURL = Bundle.main.resourceURL {
            let textualBundle = resURL
                .appendingPathComponent("textual_Textual.bundle/Contents/Resources/prism-bundle.js")
            if FileManager.default.fileExists(atPath: textualBundle.path) {
                return textualBundle
            }
        }
        for bundle in Bundle.allBundles {
            if let url = bundle.url(forResource: "prism-bundle", withExtension: "js") {
                return url
            }
        }
        return nil
    }

    func tokenize(code: String, language: String) -> [PCodeSyntaxToken] {
        guard language != "plain", !code.isEmpty,
              let ctx = ensureLoaded(),
              let fn = ctx.objectForKeyedSubscript("tokenizeCode"),
              let result = fn.call(withArguments: [code, language]),
              let array = result.toArray() as? [[String: String]]
        else {
            return [PCodeSyntaxToken(content: code, kind: .plain)]
        }

        var tokens: [PCodeSyntaxToken] = []
        tokens.reserveCapacity(array.count)
        for item in array {
            guard let content = item["content"], !content.isEmpty else { continue }
            let kind = PCodeSyntaxToken.Kind(prismType: item["type"] ?? "plain")
            tokens.append(PCodeSyntaxToken(content: content, kind: kind))
        }
        return tokens.isEmpty ? [PCodeSyntaxToken(content: code, kind: .plain)] : tokens
    }
}

/// Thread-safe, caching Prism syntax highlighter.
final class PCodePrismSyntaxHighlighter: PCodeSyntaxHighlighter, @unchecked Sendable {
    static let shared = PCodePrismSyntaxHighlighter()

    private let runtime = PCodePrismJSRuntime()
    private let lock = NSLock()
    private var cache: [String: [PCodeSyntaxToken]] = [:]
    private var cacheOrder: [String] = []
    private static let maxCacheEntries = 512

    private init() {}

    func cachedTokens(code: String, language: String?) -> [PCodeSyntaxToken]? {
        let lang = Self.normalizeLanguage(language)
        if lang == "plain" || code.isEmpty {
            return [PCodeSyntaxToken(content: code, kind: .plain)]
        }
        let key = cacheKey(code: code, language: lang)
        lock.lock()
        defer { lock.unlock() }
        return cache[key]
    }

    func tokenize(code: String, language: String?) async -> [PCodeSyntaxToken] {
        let lang = Self.normalizeLanguage(language)
        if lang == "plain" || code.isEmpty {
            return [PCodeSyntaxToken(content: code, kind: .plain)]
        }
        let key = cacheKey(code: code, language: lang)
        if let hit = lookupCache(key) {
            return hit
        }
        let tokens = await runtime.tokenize(code: code, language: lang)
        storeCache(key, tokens: tokens)
        return tokens
    }

    private func cacheKey(code: String, language: String) -> String {
        "\(language)\u{0}\(code)"
    }

    private func lookupCache(_ key: String) -> [PCodeSyntaxToken]? {
        lock.lock()
        defer { lock.unlock() }
        return cache[key]
    }

    private func storeCache(_ key: String, tokens: [PCodeSyntaxToken]) {
        lock.lock()
        defer { lock.unlock() }
        if cache[key] == nil {
            cacheOrder.append(key)
        }
        cache[key] = tokens
        while cacheOrder.count > Self.maxCacheEntries {
            let evicted = cacheOrder.removeFirst()
            cache.removeValue(forKey: evicted)
        }
    }
}
