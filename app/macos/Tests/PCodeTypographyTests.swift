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

    @Test func resetSizePreservesStyleAndWeight() {
        withFreshDefaults { defaults in
            let typo = PCodeTypography(defaults: defaults, codeFamilyAvailable: true)
            typo.style = .mono
            typo.weight = .bold
            typo.size = 19

            typo.resetSize()
            #expect(typo.size == PCodeTypography.defaultSize)
            #expect(typo.style == .mono)
            #expect(typo.weight == .bold)
        }
    }

    @Test func diffCodeFontIsDenserAndScalesWithTypographySize() {
        withFreshDefaults { defaults in
            let typo = PCodeTypography(defaults: defaults, codeFamilyAvailable: false)
            let defaultSnap = typo.snapshot
            let defaultDiffPt = defaultSnap.diffCodeNSFont().pointSize
            let defaultCodePt = defaultSnap.codeNSFont(scale: defaultSnap.codeBlockScale).pointSize
            #expect(defaultDiffPt < defaultCodePt)

            typo.increaseSize()
            typo.increaseSize()
            #expect(typo.snapshot.diffCodeNSFont().pointSize > defaultDiffPt)

            typo.size = PCodeTypography.minSize
            #expect(typo.snapshot.diffCodeNSFont().pointSize >= 9.5)
        }
    }

    @Test func fontSizeShortcutMatchesExpectedCombosOnly() {
        #expect(PCodeFontSizeShortcut.match(charactersIgnoringModifiers: "=", modifierFlags: .command) == .increase)
        #expect(PCodeFontSizeShortcut.match(charactersIgnoringModifiers: "+", modifierFlags: [.command, .shift]) == .increase)
        #expect(PCodeFontSizeShortcut.match(charactersIgnoringModifiers: "=", modifierFlags: [.command, .shift]) == .increase)
        #expect(PCodeFontSizeShortcut.match(charactersIgnoringModifiers: "+", modifierFlags: [.command, .numericPad]) == .increase)
        #expect(PCodeFontSizeShortcut.match(charactersIgnoringModifiers: "-", modifierFlags: .command) == .decrease)
        #expect(PCodeFontSizeShortcut.match(charactersIgnoringModifiers: "-", modifierFlags: [.command, .numericPad]) == .decrease)
        #expect(PCodeFontSizeShortcut.match(charactersIgnoringModifiers: "0", modifierFlags: .command) == .reset)
        #expect(PCodeFontSizeShortcut.match(charactersIgnoringModifiers: "0", modifierFlags: [.command, .numericPad]) == .reset)

        #expect(PCodeFontSizeShortcut.match(charactersIgnoringModifiers: "-", modifierFlags: [.command, .shift]) == nil)
        #expect(PCodeFontSizeShortcut.match(charactersIgnoringModifiers: "=", modifierFlags: [.command, .option]) == nil)
        #expect(PCodeFontSizeShortcut.match(charactersIgnoringModifiers: "0", modifierFlags: [.command, .shift]) == nil)
        #expect(PCodeFontSizeShortcut.match(charactersIgnoringModifiers: "=", modifierFlags: []) == nil)
        #expect(PCodeFontSizeShortcut.match(charactersIgnoringModifiers: "a", modifierFlags: .command) == nil)
    }

    @Test func persistsAndResetsCustomFontFamilies() {
        withFreshDefaults { defaults in
            let first = PCodeTypography(defaults: defaults, codeFamilyAvailable: true)
            #expect(first.proseFamily == nil)
            #expect(first.codeFamily == nil)

            first.proseFamily = "Helvetica Neue"
            first.codeFamily = "Menlo"

            let reloaded = PCodeTypography(defaults: defaults, codeFamilyAvailable: true)
            #expect(reloaded.proseFamily == "Helvetica Neue")
            #expect(reloaded.codeFamily == "Menlo")

            // Whitespace-only values normalize back to nil.
            reloaded.proseFamily = "   "
            reloaded.codeFamily = ""
            #expect(reloaded.proseFamily == nil)
            #expect(reloaded.codeFamily == nil)

            reloaded.proseFamily = "Georgia"
            reloaded.codeFamily = "Menlo"
            reloaded.reset()
            #expect(reloaded.proseFamily == nil)
            #expect(reloaded.codeFamily == nil)
        }
    }

    @Test func resolvesInstalledSystemFontFamiliesWithoutFallbackToHelvetica() {
        let menlo = PCodeTypographySnapshot.resolveFamilyNSFont(
            family: "Menlo", size: 14, weight: .medium, italic: false)
        #expect(menlo != nil)
        #expect(menlo?.familyName == "Menlo")

        let menloBold = PCodeTypographySnapshot.resolveFamilyNSFont(
            family: "Menlo", size: 14, weight: .bold, italic: false)
        #expect(menloBold != nil)
        #expect(menloBold?.familyName == "Menlo")

        let missing = PCodeTypographySnapshot.resolveFamilyNSFont(
            family: "DefinitelyNotARealFontFamilyXYZ", size: 14, weight: .regular, italic: false)
        #expect(missing == nil)
    }

    @Test func enumeratesSystemAndMonospaceFontFamilies() {
        let all = PCodeTypography.availableSystemFontFamilies()
        let mono = PCodeTypography.availableMonospaceFontFamilies()
        #expect(!all.isEmpty)
        #expect(!mono.isEmpty)
        #expect(all.allSatisfy { !$0.hasPrefix(".") })
        #expect(mono.contains("Menlo"))
    }

    @Test func generatesGhosttyOverrideConfigText() {
        let bundled = PCodeTypography.ghosttyOverrideConfigText(codeFamily: nil, codeFamilyAvailable: true)
        #expect(bundled.contains("font-family = \"\""))
        #expect(bundled.contains("font-family-bold = \"\""))
        #expect(bundled.contains("font-style = default"))
        #expect(bundled.contains("font-style-bold = default"))
        #expect(bundled.contains("font-family = \"JetBrains Mono\""))
        #expect(bundled.contains("font-size = 14"))

        let custom = PCodeTypography.ghosttyOverrideConfigText(codeFamily: "Menlo", codeFamilyAvailable: true)
        #expect(custom.contains("font-family = \"Menlo\""))
        #expect(!custom.contains("JetBrains Mono"))

        let sanitized = PCodeTypography.ghosttyOverrideConfigText(
            codeFamily: "My \"Quoted\" Font\nInjected", codeFamilyAvailable: true)
        #expect(sanitized.contains("font-family = \"My Quoted FontInjected\""))
    }

    @Test func pickFontsReflectTypographySnapshot() {
        let monoSnap = PCodeTypographySnapshot(
            style: .mono, size: 16, weight: .regular, codeFamilyAvailable: false, codeFamily: "Menlo")
        let fonts = PCodePickFonts(typography: monoSnap)
        #expect(fonts.regular.familyName == "Menlo")
        #expect(abs(fonts.regular.pointSize - 16) < 0.01)
        #expect(abs(fonts.small.pointSize - 16 * PCodePickFonts.smallScale) < 0.01)
    }
}
