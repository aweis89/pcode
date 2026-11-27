import AppKit
import Foundation
import Testing
@testable import Ghostty

@Suite
struct PCodeMenuTests {
    // MARK: - Fixtures mirroring MainMenu.xib, AppDelegate, and SurfaceView

    private static func makeItem(_ title: String, action: String? = nil) -> NSMenuItem {
        NSMenuItem(
            title: title,
            action: action.map(NSSelectorFromString),
            keyEquivalent: ""
        )
    }

    private static func makeSubmenuItem(_ title: String, items: [NSMenuItem]) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        let submenu = NSMenu(title: title)
        for child in items {
            submenu.addItem(child)
        }
        item.submenu = submenu
        return item
    }

    private static func makeMainMenu() -> NSMenu {
        let main = NSMenu(title: "Main Menu")

        main.addItem(makeSubmenuItem("Ghostty", items: [
            makeItem("About Ghostty", action: "showAbout:"),
            makeItem("Check for Updates...", action: "checkForUpdates:"),
            makeItem("Restart Pcode…", action: "restartPcode:"),
            .separator(),
            makeItem("Preferences…", action: "openConfig:"),
            .separator(),
            makeItem("Reload Configuration", action: "reloadConfig:"),
            makeItem("Secure Keyboard Entry", action: "toggleSecureInput:"),
            makeItem("Make Ghostty the Default Terminal", action: "setAsDefaultTerminal:"),
            .separator(),
            makeSubmenuItem("Services", items: []),
            .separator(),
            makeItem("Hide Ghostty", action: "hide:"),
            makeItem("Hide Others", action: "hideOtherApplications:"),
            makeItem("Show All", action: "unhideAllApplications:"),
            .separator(),
            makeItem("Quit Ghostty", action: "terminate:"),
        ]))

        main.addItem(makeSubmenuItem("File", items: [
            makeItem("New Window", action: "newWindow:"),
            makeItem("New Tab", action: "newTab:"),
            .separator(),
            makeItem("Split Right", action: "splitRight:"),
            makeItem("Split Left", action: "splitLeft:"),
            makeItem("Split Down", action: "splitDown:"),
            makeItem("Split Up", action: "splitUp:"),
            .separator(),
            makeItem("Close", action: "close:"),
            makeItem("Close Tab", action: "closeTab:"),
            makeItem("Close Window", action: "closeWindow:"),
            makeItem("Close All Windows", action: "closeAllWindows:"),
        ]))

        main.addItem(makeSubmenuItem("Edit", items: [
            makeItem("Undo", action: "undo:"),
            makeItem("Redo", action: "redo:"),
            .separator(),
            makeItem("Copy", action: "copy:"),
            makeItem("Paste", action: "paste:"),
            makeItem("Paste Selection", action: "pasteSelection:"),
            makeItem("Select All", action: "selectAll:"),
            .separator(),
            makeSubmenuItem("Find", items: [
                makeItem("Find...", action: "find:"),
                makeItem("Find Next", action: "findNext:"),
                makeItem("Find Previous", action: "findPrevious:"),
                .separator(),
                makeItem("Hide Find Bar", action: "findHide:"),
                .separator(),
                makeItem("Use Selection for Find", action: "selectionForFind:"),
                makeItem("Jump to Selection", action: "scrollToSelection:"),
            ]),
            .separator(),
        ]))

        main.addItem(makeSubmenuItem("View", items: [
            makeItem("Reset Font Size", action: "resetFontSize:"),
            makeItem("Increase Font Size", action: "increaseFontSize:"),
            makeItem("Decrease Font Size", action: "decreaseFontSize:"),
            .separator(),
            makeItem("Command Palette", action: "toggleCommandPalette:"),
            makeItem("Change Tab Title...", action: "changeTabTitle:"),
            makeItem("Change Terminal Title...", action: "changeTitle:"),
            makeItem("Terminal Read-only", action: "toggleReadonly:"),
            .separator(),
            makeItem("Quick Terminal", action: "toggleQuickTerminal:"),
            .separator(),
            makeItem("Terminal Inspector", action: "toggleTerminalInspector:"),
        ]))

        main.addItem(makeSubmenuItem("Window", items: [
            makeItem("Minimize", action: "performMiniaturize:"),
            makeItem("Zoom", action: "performZoom:"),
            .separator(),
            makeItem("Toggle Full Screen", action: "toggleGhosttyFullScreen:"),
            makeItem("Show/Hide All Terminals", action: "toggleVisibility:"),
            .separator(),
            makeItem("Zoom Split", action: "splitZoom:"),
            makeItem("Select Previous Split", action: "splitMoveFocusPrevious:"),
            makeItem("Select Next Split", action: "splitMoveFocusNext:"),
            makeSubmenuItem("Select Split", items: [
                makeItem("Select Split Above", action: "splitMoveFocusAbove:"),
                makeItem("Select Split Below", action: "splitMoveFocusBelow:"),
                makeItem("Select Split Left", action: "splitMoveFocusLeft:"),
                makeItem("Select Split Right", action: "splitMoveFocusRight:"),
            ]),
            makeSubmenuItem("Resize Split", items: [
                makeItem("Equalize Splits", action: "equalizeSplits:"),
                .separator(),
                makeItem("Move Divider Up", action: "moveSplitDividerUp:"),
                makeItem("Move Divider Down", action: "moveSplitDividerDown:"),
                makeItem("Move Divider Left", action: "moveSplitDividerLeft:"),
                makeItem("Move Divider Right", action: "moveSplitDividerRight:"),
            ]),
            .separator(),
            makeItem("Return To Default Size", action: "returnToDefaultSize:"),
            .separator(),
            makeItem("Float on Top", action: "floatOnTop:"),
            makeItem("Use as Default", action: "useAsDefault:"),
            .separator(),
            makeItem("Bring All to Front", action: "arrangeInFront:"),
        ]))

        main.addItem(makeSubmenuItem("Help", items: [
            makeItem("Ghostty Help", action: "showHelp:"),
        ]))

        return main
    }

    private static func makeDockMenu() -> NSMenu {
        let menu = NSMenu(title: "Dock")
        menu.addItem(makeItem("New Window", action: "newWindow:"))
        menu.addItem(makeItem("New Tab", action: "newTab:"))
        return menu
    }

    private static func makeSurfaceContextMenu() -> NSMenu {
        let menu = NSMenu()
        menu.addItem(makeItem("Copy", action: "copy:"))
        menu.addItem(makeItem("Paste", action: "paste:"))
        menu.addItem(.separator())
        menu.addItem(makeItem("Split Right", action: "splitRight:"))
        menu.addItem(makeItem("Split Left", action: "splitLeft:"))
        menu.addItem(makeItem("Split Down", action: "splitDown:"))
        menu.addItem(makeItem("Split Up", action: "splitUp:"))
        menu.addItem(.separator())
        menu.addItem(makeItem("Close Split", action: "closeSurface:"))
        menu.addItem(.separator())
        menu.addItem(makeItem("Reset Terminal", action: "resetTerminal:"))
        menu.addItem(makeItem("Toggle Terminal Inspector", action: "toggleTerminalInspector:"))
        menu.addItem(makeItem("Terminal Read-only", action: "toggleReadonly:"))
        menu.addItem(.separator())
        menu.addItem(makeItem("Change Tab Title...", action: "changeTabTitle:"))
        menu.addItem(makeItem("Change Terminal Title...", action: "changeTitle:"))
        return menu
    }

    // MARK: - Inspection helpers

    private static func allTitles(in menu: NSMenu) -> [String] {
        var titles = [menu.title]
        for item in menu.items {
            titles.append(item.title)
            if let submenu = item.submenu {
                titles.append(contentsOf: allTitles(in: submenu))
            }
        }
        return titles
    }

    private static func allItems(in menu: NSMenu) -> [NSMenuItem] {
        var result: [NSMenuItem] = []
        for item in menu.items {
            result.append(item)
            if let submenu = item.submenu {
                result.append(contentsOf: allItems(in: submenu))
            }
        }
        return result
    }

    private static func item(withAction selector: String, in menu: NSMenu) -> NSMenuItem? {
        allItems(in: menu).first { item in
            item.action.map(NSStringFromSelector) == selector
        }
    }

    private static func assertCleanSeparators(in menu: NSMenu) {
        let items = menu.items
        if let first = items.first {
            #expect(!first.isSeparatorItem, "Leading separator in menu '\(menu.title)'")
        }
        if let last = items.last {
            #expect(!last.isSeparatorItem, "Trailing separator in menu '\(menu.title)'")
        }
        if items.count > 1 {
            for index in 1..<items.count {
                let consecutive = items[index - 1].isSeparatorItem && items[index].isSeparatorItem
                #expect(!consecutive, "Consecutive separators in menu '\(menu.title)' at index \(index)")
            }
        }
        for item in items {
            if let submenu = item.submenu {
                assertCleanSeparators(in: submenu)
            }
        }
    }

    // MARK: - Main menu tests

    @Test @MainActor
    func mainMenuWorkspaceModeRebrandsPrunesAndCleansSeparators() throws {
        let menu = Self.makeMainMenu()
        // Add an extra stray Ghostty-branded item to verify the recursive safety net.
        let helpMenu = try #require(menu.items.last?.submenu)
        helpMenu.addItem(Self.makeItem("Ghostty Community", action: "openCommunity:"))

        PCodeMenu.configureMainMenu(menu, replacesTerminalWindows: true)

        // Zero "ghostty" strings anywhere in the configured menu tree.
        for title in Self.allTitles(in: menu) {
            #expect(
                title.range(of: "ghostty", options: .caseInsensitive) == nil,
                "Found unexpected Ghostty reference in title '\(title)'"
            )
        }

        // App menu title and renamed items keep their original actions.
        let appItem = try #require(menu.items.first)
        #expect(appItem.title == "Pcode")
        #expect(appItem.submenu?.title == "Pcode")

        let expectedRenames: [(selector: String, title: String)] = [
            ("showAbout:", "About Pcode"),
            ("hide:", "Hide Pcode"),
            ("terminate:", "Quit Pcode"),
            ("showHelp:", "Pcode Help"),
            ("newTab:", "New Session"),
            ("toggleVisibility:", "Show/Hide All Windows"),
            ("openCommunity:", "Pcode Community"),
        ]
        for expected in expectedRenames {
            let found = try #require(
                Self.item(withAction: expected.selector, in: menu),
                "Missing item for selector \(expected.selector)"
            )
            #expect(found.title == expected.title)
        }

        // Removed items are gone.
        let removedSelectors = [
            "setAsDefaultTerminal:",
            "splitRight:",
            "splitLeft:",
            "splitDown:",
            "splitUp:",
            "closeTab:",
            "closeAllWindows:",
            "toggleCommandPalette:",
            "changeTabTitle:",
            "changeTitle:",
            "toggleQuickTerminal:",
            "splitZoom:",
            "splitMoveFocusPrevious:",
            "splitMoveFocusNext:",
            "splitMoveFocusAbove:",
            "splitMoveFocusBelow:",
            "splitMoveFocusLeft:",
            "splitMoveFocusRight:",
            "equalizeSplits:",
            "moveSplitDividerUp:",
            "moveSplitDividerDown:",
            "moveSplitDividerLeft:",
            "moveSplitDividerRight:",
            "returnToDefaultSize:",
            "floatOnTop:",
            "useAsDefault:",
        ]
        for selector in removedSelectors {
            #expect(
                Self.item(withAction: selector, in: menu) == nil,
                "Expected selector \(selector) to be removed"
            )
        }

        // Select Split and Resize Split submenus are removed.
        let titles = Set(Self.allTitles(in: menu))
        #expect(!titles.contains("Select Split"))
        #expect(!titles.contains("Resize Split"))

        // Kept items and their actions remain intact.
        for kept in [
            "checkForUpdates:",
            "restartPcode:",
            "openConfig:",
            "reloadConfig:",
            "toggleSecureInput:",
            "newWindow:",
            "close:",
            "closeWindow:",
            "resetFontSize:",
            "increaseFontSize:",
            "decreaseFontSize:",
            "toggleReadonly:",
            "toggleTerminalInspector:",
            "performMiniaturize:",
            "performZoom:",
            "toggleGhosttyFullScreen:",
            "arrangeInFront:",
        ] {
            #expect(Self.item(withAction: kept, in: menu) != nil, "Expected selector \(kept) to remain")
        }
        #expect(titles.contains("Services"))

        // No leading, trailing, or consecutive separators anywhere.
        Self.assertCleanSeparators(in: menu)
    }

    @Test @MainActor
    func mainMenuNonWorkspaceModeRebrandsAndKeepsTerminalItems() throws {
        let menu = Self.makeMainMenu()
        PCodeMenu.configureMainMenu(menu, replacesTerminalWindows: false)

        for title in Self.allTitles(in: menu) {
            #expect(
                title.range(of: "ghostty", options: .caseInsensitive) == nil,
                "Found unexpected Ghostty reference in title '\(title)'"
            )
        }

        #expect(menu.items.first?.title == "Pcode")
        #expect(Self.item(withAction: "showAbout:", in: menu)?.title == "About Pcode")
        #expect(Self.item(withAction: "hide:", in: menu)?.title == "Hide Pcode")
        #expect(Self.item(withAction: "terminate:", in: menu)?.title == "Quit Pcode")
        #expect(Self.item(withAction: "showHelp:", in: menu)?.title == "Pcode Help")
        #expect(Self.item(withAction: "setAsDefaultTerminal:", in: menu) == nil)

        // Terminal split/tab/window items stay intact in non-workspace mode.
        #expect(Self.item(withAction: "newTab:", in: menu)?.title == "New Tab")
        #expect(Self.item(withAction: "toggleVisibility:", in: menu)?.title == "Show/Hide All Terminals")
        for kept in [
            "splitRight:",
            "splitLeft:",
            "splitDown:",
            "splitUp:",
            "closeTab:",
            "closeAllWindows:",
            "toggleCommandPalette:",
            "changeTabTitle:",
            "changeTitle:",
            "toggleQuickTerminal:",
            "splitZoom:",
            "splitMoveFocusPrevious:",
            "splitMoveFocusNext:",
            "equalizeSplits:",
            "returnToDefaultSize:",
            "floatOnTop:",
            "useAsDefault:",
        ] {
            #expect(Self.item(withAction: kept, in: menu) != nil, "Expected \(kept) in non-workspace mode")
        }

        let titles = Set(Self.allTitles(in: menu))
        #expect(titles.contains("Select Split"))
        #expect(titles.contains("Resize Split"))

        Self.assertCleanSeparators(in: menu)
    }

    // MARK: - Dock menu tests

    @Test @MainActor
    func dockMenuRenamesNewTabOnlyInWorkspaceMode() throws {
        let workspaceDock = Self.makeDockMenu()
        PCodeMenu.configureDockMenu(workspaceDock, replacesTerminalWindows: true)
        #expect(Self.item(withAction: "newWindow:", in: workspaceDock)?.title == "New Window")
        #expect(Self.item(withAction: "newTab:", in: workspaceDock)?.title == "New Session")

        let terminalDock = Self.makeDockMenu()
        PCodeMenu.configureDockMenu(terminalDock, replacesTerminalWindows: false)
        #expect(Self.item(withAction: "newWindow:", in: terminalDock)?.title == "New Window")
        #expect(Self.item(withAction: "newTab:", in: terminalDock)?.title == "New Tab")
    }

    // MARK: - Surface context menu tests

    @Test @MainActor
    func surfaceContextMenuPrunesSplitsAndTitlesInWorkspaceMode() {
        let menu = Self.makeSurfaceContextMenu()
        PCodeMenu.configureSurfaceContextMenu(menu, replacesTerminalWindows: true)

        for removed in [
            "splitRight:",
            "splitLeft:",
            "splitDown:",
            "splitUp:",
            "closeSurface:",
            "changeTabTitle:",
            "changeTitle:",
        ] {
            #expect(Self.item(withAction: removed, in: menu) == nil)
        }

        for kept in [
            "copy:",
            "paste:",
            "resetTerminal:",
            "toggleTerminalInspector:",
            "toggleReadonly:",
        ] {
            #expect(Self.item(withAction: kept, in: menu) != nil)
        }

        Self.assertCleanSeparators(in: menu)
        #expect(menu.items.filter(\.isSeparatorItem).count == 1)
    }

    @Test @MainActor
    func surfaceContextMenuKeepsItemsInNonWorkspaceMode() {
        let menu = Self.makeSurfaceContextMenu()
        PCodeMenu.configureSurfaceContextMenu(menu, replacesTerminalWindows: false)

        #expect(Self.item(withAction: "splitRight:", in: menu) != nil)
        #expect(Self.item(withAction: "closeSurface:", in: menu) != nil)
        #expect(Self.item(withAction: "changeTabTitle:", in: menu) != nil)
        #expect(Self.item(withAction: "changeTitle:", in: menu) != nil)
        Self.assertCleanSeparators(in: menu)
    }

    // MARK: - Dump helper test

    @Test @MainActor
    func dumpMenuSerializesTreeForDebugSocket() throws {
        let menu = Self.makeMainMenu()
        PCodeMenu.configureMainMenu(menu, replacesTerminalWindows: true)

        let dumped = PCodeMenu.dumpMenu(menu)
        #expect(JSONSerialization.isValidJSONObject(dumped))
        #expect(dumped.count == 6)

        let appEntry = try #require(dumped.first)
        #expect(appEntry["title"] as? String == "Pcode")
        #expect(appEntry["isSeparator"] as? Bool == false)
        #expect(appEntry["hidden"] as? Bool == false)

        let appSubmenu = try #require(appEntry["submenu"] as? [[String: Any]])
        let aboutEntry = try #require(appSubmenu.first)
        #expect(aboutEntry["title"] as? String == "About Pcode")
        #expect(aboutEntry["action"] as? String == "showAbout:")
    }
}
