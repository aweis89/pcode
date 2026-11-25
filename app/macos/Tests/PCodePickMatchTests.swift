import Foundation
import Testing
@testable import Ghostty

/// The native pick list must filter as pcode does. `pick_fuzzy_cases.json` is
/// pcode's `tests/data/pick_fuzzy_cases.json`, which pcode's own `fuzzy_match`
/// and `casefold` are tested against; `app/scripts/build.sh` copies it in.
@Suite
struct PCodePickMatchTests {
    struct FuzzyCase: Decodable, Sendable, CustomTestStringConvertible {
        let term: String
        let text: String
        let match: Bool
        var testDescription: String { "\(term) in \(text)" }
    }

    struct FoldCase: Decodable, Sendable, CustomTestStringConvertible {
        let text: String
        let folded: String
        var testDescription: String { text }
    }

    struct Fixture: Decodable {
        let fuzzy: [FuzzyCase]
        let casefold: [FoldCase]
    }

    private final class Anchor {}

    static let fixture: Fixture = {
        guard let url = Bundle(for: Anchor.self).url(forResource: "pick_fuzzy_cases", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let fixture = try? JSONDecoder().decode(Fixture.self, from: data) else {
            return Fixture(fuzzy: [], casefold: [])
        }
        return fixture
    }()

    @Test func theFixtureLoads() {
        #expect(Self.fixture.fuzzy.count > 10)
        #expect(!Self.fixture.casefold.isEmpty)
    }

    @Test(arguments: fixture.fuzzy)
    func fuzzyMatchesLikePcode(_ testCase: FuzzyCase) {
        #expect(PCodePickMatch.fuzzy(testCase.term, testCase.text) == testCase.match)
    }

    @Test(arguments: fixture.casefold)
    func foldsLikeCasefold(_ testCase: FoldCase) {
        #expect(PCodePickMatch.casefold(testCase.text) == testCase.folded)
    }

    static let treeCases: [([Int], [String])] = [
        ([0, 1, 2, 1, 0, 1, 1, 2, 0], ["", "├─ ", "│  └─ ", "└─ ", "", "├─ ", "└─ ", "   └─ ", ""]),
        ([2, 3, 0], ["", "└─ ", ""]),
        ([0, 1, 2, 3, 1], ["", "├─ ", "│  └─ ", "│     └─ ", "└─ "]),
    ]

    @Test(arguments: treeCases)
    func treeGuidesLikeNestForks(depths: [Int], guides: [String]) {
        #expect(PCodePickMatch.treeGuides(depths) == guides)
    }

    @Test func customRowsNeedTheWholeQuery() throws {
        let pattern = try #require(PCodePickMatch.anchored("(?:openai|openai-codex):[A-Za-z0-9_.:/-]+"))
        #expect(PCodePickMatch.fullMatch(pattern, "openai-codex:x"))
        #expect(PCodePickMatch.fullMatch(pattern, "openai:x"))
        #expect(!PCodePickMatch.fullMatch(pattern, "openai:bad id"))
        #expect(!PCodePickMatch.fullMatch(pattern, "xopenai:a"))
    }
}
