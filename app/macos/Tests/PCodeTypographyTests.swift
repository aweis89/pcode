import Foundation
import SwiftUI
import Testing
@testable import Ghostty

struct PCodeTypographyTests {
    private func withFreshDefaults(_ body: (UserDefaults) -> Void) {
        let suite = "com.pcode.tests.typography.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        defer { defaults.removePersistentDomain(forName: suite) }
        body(defaults)
    }

    @Test func defaultsMatchExpectedValues() {
        withFreshDefaults { defaults in
            let typo = PCodeTypography(defaults: defaults, codeFamilyAvailable: true)
            #expect(typo.style == .sansAndMono)
            #expect(typo.size == PCodeTypography.defaultSize)
            #expect(typo.weight == .medium)
            #expect(typo.inlineCodeScale < 1.0)
        }
    }

    @Test func persistsAndReloadsPreferences() {
        withFreshDefaults { defaults in
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
    }

    @Test func clampsSizeBoundsAndNonFiniteValues() {
        withFreshDefaults { defaults in
            let typo = PCodeTypography(defaults: defaults, codeFamilyAvailable: false)
            typo.size = 4
            #expect(typo.size == PCodeTypography.minSize)
            typo.size = 99
            #expect(typo.size == PCodeTypography.maxSize)
            typo.size = .nan
            #expect(typo.size == PCodeTypography.defaultSize)
            typo.size = .infinity
            #expect(typo.size == PCodeTypography.maxSize)
            typo.size = -.infinity
            #expect(typo.size == PCodeTypography.minSize)

            typo.decreaseSize()
            #expect(typo.size == PCodeTypography.minSize)
            typo.increaseSize()
            #expect(typo.size == PCodeTypography.minSize + 1)
        }
    }

    @Test func recoversFromCorruptStoredDefaults() {
        withFreshDefaults { defaults in
            defaults.set("unknown_style", forKey: "PCodeTypographyStyle")
            defaults.set("ultra_light", forKey: "PCodeTypographyWeight")
            defaults.set(-5.0, forKey: "PCodeTypographySize")

            let typo = PCodeTypography(defaults: defaults, codeFamilyAvailable: true)
            #expect(typo.style == .sansAndMono)
            #expect(typo.weight == .medium)
            #expect(typo.size == PCodeTypography.defaultSize)
        }
    }

    @Test func resetRestoresDefaults() {
        withFreshDefaults { defaults in
            let typo = PCodeTypography(defaults: defaults, codeFamilyAvailable: true)
            typo.style = .mono
            typo.size = 19
            typo.weight = .bold
            typo.reset()

            #expect(typo.style == .sansAndMono)
            #expect(typo.size == PCodeTypography.defaultSize)
            #expect(typo.weight == .medium)
        }
    }

    @Test func emphasisWeightStepsUpFromBaseWeight() {
        #expect(PCodeTypography.Weight.regular.emphasisWeight == .semibold)
        #expect(PCodeTypography.Weight.medium.emphasisWeight == .bold)
        #expect(PCodeTypography.Weight.semibold.emphasisWeight == .bold)
        #expect(PCodeTypography.Weight.bold.emphasisWeight == .heavy)
    }
}
