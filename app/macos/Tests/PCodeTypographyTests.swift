import Foundation
import SwiftUI
import Testing
@testable import Ghostty

struct PCodeTypographyTests {
    private func freshDefaults() -> UserDefaults {
        let suite = "com.pcode.tests.typography.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        return defaults
    }

    @Test func defaultsMatchExpectedValues() {
        let defaults = freshDefaults()
        let typo = PCodeTypography(defaults: defaults, codeFamilyAvailable: true)
        #expect(typo.style == .sansAndMono)
        #expect(typo.size == PCodeTypography.defaultSize)
        #expect(typo.weight == .medium)
        #expect(typo.inlineCodeScale < 1.0)
    }

    @Test func persistsAndReloadsPreferences() {
        let defaults = freshDefaults()
        let first = PCodeTypography(defaults: defaults, codeFamilyAvailable: true)
        first.style = .mono
        first.size = 17
        first.weight = .bold

        let reloaded = PCodeTypography(defaults: defaults, codeFamilyAvailable: true)
        #expect(reloaded.style == .mono)
        #expect(reloaded.size == 17)
        #expect(reloaded.weight == .bold)
        #expect(reloaded.inlineCodeScale == 1.0)
    }

    @Test func clampsSizeBounds() {
        let defaults = freshDefaults()
        let typo = PCodeTypography(defaults: defaults, codeFamilyAvailable: false)
        typo.size = 4
        #expect(typo.size == PCodeTypography.minSize)
        typo.size = 99
        #expect(typo.size == PCodeTypography.maxSize)

        typo.decreaseSize()
        #expect(typo.size == PCodeTypography.maxSize - 1)
        typo.increaseSize()
        #expect(typo.size == PCodeTypography.maxSize)
    }

    @Test func emphasisWeightStepsUpFromBaseWeight() {
        #expect(PCodeTypography.Weight.regular.emphasisWeight == .semibold)
        #expect(PCodeTypography.Weight.medium.emphasisWeight == .bold)
        #expect(PCodeTypography.Weight.semibold.emphasisWeight == .bold)
        #expect(PCodeTypography.Weight.bold.emphasisWeight == .heavy)
    }
}
