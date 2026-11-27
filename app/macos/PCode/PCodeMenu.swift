import AppKit

/// Rebrands and prunes Ghostty's nib-loaded and runtime AppKit menus for Pcode.
enum PCodeMenu {
    private static let appRemovedActions: Set<String> = [
        "setAsDefaultTerminal:",
    ]

    private static let fileWorkspaceRemovedActions: Set<String> = [
        "splitRight:",
        "splitLeft:",
        "splitDown:",
        "splitUp:",
        "closeTab:",
        "closeAllWindows:",
    ]

    private static let viewWorkspaceRemovedActions: Set<String> = [
        "toggleCommandPalette:",
        "changeTabTitle:",
        "changeTitle:",
        "toggleQuickTerminal:",
    ]

    private static let windowWorkspaceRemovedActions: Set<String> = [
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

    private static let windowWorkspaceRemovedSubmenus: Set<String> = [
        "Select Split",
        "Resize Split",
    ]

    private static let surfaceContextWorkspaceRemovedActions: Set<String> = [
        "splitRight:",
        "splitLeft:",
        "splitDown:",
        "splitUp:",
        "closeSurface:",
        "changeTabTitle:",
        "changeTitle:",
    ]

    static func configureMainMenu(
        _ menu: NSMenu,
        replacesTerminalWindows: Bool = PCodeWorkspaceMode.enabled
    ) {
        if let appItem = menu.items.first {
            appItem.title = "Pcode"
            if let appMenu = appItem.submenu {
                appMenu.title = "Pcode"
                renameItems(in: appMenu, map: [
                    "showAbout:": "About Pcode",
                    "hide:": "Hide Pcode",
                    "terminate:": "Quit Pcode",
                ])
                removeItems(in: appMenu, actions: appRemovedActions)
            }
        }

        if let helpMenu = submenu(named: "Help", in: menu) {
            renameItems(in: helpMenu, map: [
                "showHelp:": "Pcode Help",
            ])
        }

        if replacesTerminalWindows {
            if let fileMenu = submenu(named: "File", in: menu) {
                renameItems(in: fileMenu, map: [
                    "newTab:": "New Session",
                ])
                removeItems(in: fileMenu, actions: fileWorkspaceRemovedActions)
            }

            if let viewMenu = submenu(named: "View", in: menu) {
                removeItems(in: viewMenu, actions: viewWorkspaceRemovedActions)
            }

            if let windowMenu = submenu(named: "Window", in: menu) {
                renameItems(in: windowMenu, map: [
                    "toggleVisibility:": "Show/Hide All Windows",
                ])
                removeItems(
                    in: windowMenu,
                    actions: windowWorkspaceRemovedActions,
                    submenuTitles: windowWorkspaceRemovedSubmenus
                )
            }
        }

        rebrandRemainingGhosttyStrings(in: menu)
        collapseSeparatorsRecursively(in: menu)
    }

    static func configureDockMenu(
        _ menu: NSMenu,
        replacesTerminalWindows: Bool = PCodeWorkspaceMode.enabled
    ) {
        if replacesTerminalWindows {
            renameItems(in: menu, map: [
                "newTab:": "New Session",
            ])
        }
        rebrandRemainingGhosttyStrings(in: menu)
        collapseSeparatorsRecursively(in: menu)
    }

    static func configureSurfaceContextMenu(
        _ menu: NSMenu,
        replacesTerminalWindows: Bool = PCodeWorkspaceMode.enabled
    ) {
        if replacesTerminalWindows {
            removeItems(in: menu, actions: surfaceContextWorkspaceRemovedActions)
        }
        rebrandRemainingGhosttyStrings(in: menu)
        collapseSeparatorsRecursively(in: menu)
    }

    static func dumpMenu(_ menu: NSMenu) -> [[String: Any]] {
        menu.items.map { item in
            var entry: [String: Any] = [
                "title": item.title,
                "hidden": item.isHidden,
                "isSeparator": item.isSeparatorItem,
            ]
            if let action = item.action {
                entry["action"] = NSStringFromSelector(action)
            }
            if let submenu = item.submenu {
                entry["submenu"] = dumpMenu(submenu)
            }
            return entry
        }
    }

    // MARK: - Helpers

    private static func submenu(named title: String, in menu: NSMenu) -> NSMenu? {
        for item in menu.items {
            if item.title == title || item.submenu?.title == title {
                return item.submenu
            }
        }
        return nil
    }

    private static func actionName(for item: NSMenuItem) -> String? {
        item.action.map(NSStringFromSelector)
    }

    private static func renameItems(in menu: NSMenu, map: [String: String]) {
        for item in menu.items {
            if let name = actionName(for: item), let replacement = map[name] {
                item.title = replacement
            }
            if let submenu = item.submenu {
                renameItems(in: submenu, map: map)
            }
        }
    }

    private static func removeItems(
        in menu: NSMenu,
        actions: Set<String>,
        submenuTitles: Set<String> = []
    ) {
        for item in menu.items {
            if let name = actionName(for: item), actions.contains(name) {
                menu.removeItem(item)
                continue
            }
            if let submenu = item.submenu {
                if submenuTitles.contains(item.title) || submenuTitles.contains(submenu.title) {
                    menu.removeItem(item)
                    continue
                }
                removeItems(in: submenu, actions: actions, submenuTitles: submenuTitles)
            }
        }
    }

    private static func rebrandRemainingGhosttyStrings(in menu: NSMenu) {
        menu.title = rebrandText(menu.title)
        for item in menu.items {
            item.title = rebrandText(item.title)
            if let submenu = item.submenu {
                rebrandRemainingGhosttyStrings(in: submenu)
            }
        }
    }

    private static func rebrandText(_ text: String) -> String {
        guard text.range(of: "ghostty", options: .caseInsensitive) != nil else {
            return text
        }
        return text.replacingOccurrences(of: "ghostty", with: "Pcode", options: .caseInsensitive)
    }

    private static func collapseSeparatorsRecursively(in menu: NSMenu) {
        collapseSeparators(in: menu)
        for item in menu.items {
            if let submenu = item.submenu {
                collapseSeparatorsRecursively(in: submenu)
            }
        }
    }

    private static func collapseSeparators(in menu: NSMenu) {
        var sawNonSeparator = false
        var lastWasSeparator = false

        for item in menu.items {
            if item.isSeparatorItem {
                if !sawNonSeparator || lastWasSeparator {
                    menu.removeItem(item)
                } else {
                    lastWasSeparator = true
                }
            } else {
                sawNonSeparator = true
                lastWasSeparator = false
            }
        }

        while let last = menu.items.last, last.isSeparatorItem {
            menu.removeItem(last)
        }
    }
}
