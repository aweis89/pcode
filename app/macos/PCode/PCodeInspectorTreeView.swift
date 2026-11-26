import AppKit
import SwiftUI

/// Native Conversation Tree & Scroll Map tab inside the right-hand Session Inspector.
///
/// - Active-branch turns act as a live table of contents: scrolling the transcript highlights
///   the current turn (scroll-spy), and clicking any active turn jumps straight to it.
/// - Inactive branches stay visible with git-graph rails and collapsible badges; clicking one
///   opens an inline preview with explicit **Continue** and **Edit & fork** actions so a click
///   never mutates context by accident.
struct PCodeInspectorTreeView: View {
    @ObservedObject var state: PCodeInspectorState
    @ObservedObject var store: PCodeConversationTreeStore
    let actions: PCodeInspectorActions

    @Environment(\.colorScheme) private var colorScheme
    @State private var searchText: String = ""
    @State private var activeBranchOnly: Bool = false
    @State private var includeAsides: Bool = true
    @State private var includeCompactions: Bool = true
    @State private var collapsedBranchRoots: Set<String> = []
    @State private var selectedNodeID: String?
    @State private var hoveredRowID: String?
    @State private var isNavigating: Bool = false
    @State private var statusBanner: String?

    private var hasAsides: Bool {
        store.tree.nodesByID.values.contains { $0.kind == .aside }
    }

    private var hasCompactions: Bool {
        store.tree.nodesByID.values.contains { $0.kind == .compaction }
    }

    private var rows: [PCodeConversationTreeRow] {
        store.tree.flattenedRows(
            collapsedBranchRoots: collapsedBranchRoots,
            includeAsides: includeAsides,
            includeCompactions: includeCompactions,
            activeBranchOnly: activeBranchOnly,
            searchQuery: searchText
        )
    }

    private var selectedInactiveNode: PCodeConversationTurnNode? {
        guard let selectedNodeID,
              let node = store.tree.nodesByID[selectedNodeID],
              !store.tree.activePathSet().contains(selectedNodeID)
        else { return nil }
        return node
    }

    var body: some View {
        VStack(spacing: 0) {
            if store.tree.isEmpty {
                emptyState
            } else {
                toolbar
                Divider().opacity(0.5)

                if let statusBanner, !statusBanner.isEmpty {
                    bannerBar(statusBanner)
                    Divider().opacity(0.4)
                }

                treeList

                if let previewNode = selectedInactiveNode {
                    Divider().opacity(0.6)
                    inactivePreviewCard(previewNode)
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .onChange(of: store.sessionID) { _ in
            searchText = ""
            selectedNodeID = nil
            collapsedBranchRoots.removeAll()
            statusBanner = nil
        }
    }

    // MARK: - Toolbar

    private var toolbar: some View {
        VStack(spacing: 6) {
            HStack(spacing: 6) {
                HStack(spacing: 5) {
                    Image(systemName: "magnifyingglass")
                        .font(.system(size: 10.5))
                        .foregroundStyle(.secondary)
                    ZStack(alignment: .leading) {
                        if searchText.isEmpty {
                            Text("Filter turns…")
                                .font(.system(size: 11.5))
                                .foregroundStyle(.secondary.opacity(0.75))
                                .allowsHitTesting(false)
                        }
                        TextField("", text: $searchText)
                            .textFieldStyle(.plain)
                            .font(.system(size: 11.5))
                    }
                    if !searchText.isEmpty {
                        Button {
                            searchText = ""
                        } label: {
                            Image(systemName: "xmark.circle.fill")
                                .font(.system(size: 10.5))
                                .foregroundStyle(.secondary)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 7)
                .padding(.vertical, 4)
                .background(
                    RoundedRectangle(cornerRadius: 5, style: .continuous)
                        .fill(Color.primary.opacity(colorScheme == .dark ? 0.07 : 0.05))
                )

                if store.tree.hasBranches {
                    branchScopeToggle
                }
            }

            HStack(spacing: 8) {
                let turns = store.tree.turnCount
                let tips = store.tree.branchTipCount
                Text(
                    tips > 1
                        ? "\(turns) turn\(turns == 1 ? "" : "s") · \(tips) branches"
                        : "\(turns) turn\(turns == 1 ? "" : "s")"
                )
                .font(.system(size: 10.5, design: .monospaced))
                .foregroundStyle(.secondary)

                if store.isBusy {
                    HStack(spacing: 4) {
                        Circle()
                            .fill(Color.accentColor)
                            .frame(width: 5, height: 5)
                        Text("Working · scroll-only")
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                    }
                }

                Spacer(minLength: 4)

                if hasAsides {
                    filterTogglePill(
                        label: "/btw",
                        isOn: $includeAsides,
                        help: "Show or hide /btw side questions"
                    )
                }
                if hasCompactions {
                    filterTogglePill(
                        label: "Compactions",
                        isOn: $includeCompactions,
                        help: "Show or hide context compaction checkpoints"
                    )
                }
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
    }

    private var branchScopeToggle: some View {
        HStack(spacing: 1) {
            scopeButton("All", selected: !activeBranchOnly) {
                activeBranchOnly = false
            }
            scopeButton("Active", selected: activeBranchOnly) {
                activeBranchOnly = true
            }
        }
        .padding(2)
        .background(
            RoundedRectangle(cornerRadius: 5, style: .continuous)
                .fill(Color.primary.opacity(colorScheme == .dark ? 0.08 : 0.05))
        )
    }

    private func scopeButton(_ title: String, selected: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title)
                .font(.system(size: 10.5, weight: selected ? .semibold : .medium))
                .foregroundStyle(selected ? .primary : .secondary)
                .padding(.horizontal, 6)
                .padding(.vertical, 2.5)
                .background(
                    RoundedRectangle(cornerRadius: 3.5, style: .continuous)
                        .fill(selected ? Color.primary.opacity(colorScheme == .dark ? 0.14 : 0.10) : .clear)
                )
        }
        .buttonStyle(.plain)
    }

    private func filterTogglePill(label: String, isOn: Binding<Bool>, help: String) -> some View {
        Button {
            isOn.wrappedValue.toggle()
        } label: {
            Text(label)
                .font(.system(size: 10, weight: .medium))
                .foregroundStyle(isOn.wrappedValue ? Color.primary : Color.secondary.opacity(0.65))
                .padding(.horizontal, 5)
                .padding(.vertical, 1.5)
                .background(
                    Capsule()
                        .fill(Color.primary.opacity(isOn.wrappedValue ? 0.10 : 0.04))
                )
        }
        .buttonStyle(.plain)
        .help(help)
    }

    private func bannerBar(_ message: String) -> some View {
        HStack(spacing: 6) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 10.5))
                .foregroundStyle(.orange)
            Text(message)
                .font(.system(size: 11))
                .foregroundStyle(.primary)
                .lineLimit(2)
            Spacer(minLength: 4)
            Button {
                statusBanner = nil
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(.secondary)
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(Color.orange.opacity(colorScheme == .dark ? 0.12 : 0.08))
    }

    // MARK: - Tree List

    private var treeList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    if searchText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        conversationStartRow
                    }

                    ForEach(rows) { row in
                        treeRowView(row)
                            .id(row.id)
                    }

                    if rows.isEmpty && !searchText.isEmpty {
                        Text("No turns matching \"\(searchText)\"")
                            .font(.system(size: 11.5))
                            .foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .center)
                            .padding(.vertical, 24)
                    }
                }
                .padding(.horizontal, 6)
                .padding(.vertical, 6)
            }
            .onChange(of: store.visibleTurnID) { newID in
                guard let newID,
                      searchText.isEmpty,
                      hoveredRowID == nil,
                      selectedInactiveNode == nil
                else { return }
                withAnimation(.easeInOut(duration: 0.16)) {
                    proxy.scrollTo(newID, anchor: .center)
                }
            }
        }
    }

    private var conversationStartRow: some View {
        let isActive = store.tree.activeID == nil
        let isHovered = hoveredRowID == "__start__"
        return HStack(spacing: 6) {
            Circle()
                .strokeBorder(
                    isActive ? Color.accentColor : Color.secondary.opacity(0.45),
                    lineWidth: isActive ? 2 : 1.25
                )
                .frame(width: 8, height: 8)
                .padding(.leading, 8)

            Text("Conversation start")
                .font(.system(size: 11, weight: isActive ? .semibold : .medium))
                .foregroundStyle(isActive ? .primary : .secondary)

            if isActive {
                headBadge
            }

            Spacer(minLength: 4)

            if isHovered && !isActive && !store.isBusy && actions.navigateTree != nil {
                Button {
                    triggerNavigate(nodeID: nil, edit: false)
                } label: {
                    Text("Start fresh")
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(
                            RoundedRectangle(cornerRadius: 4, style: .continuous)
                                .fill(Color.primary.opacity(0.08))
                        )
                }
                .buttonStyle(.plain)
                .disabled(isNavigating)
                .help("Switch to empty context (previous branches are kept)")
            }
        }
        .padding(.vertical, 5)
        .padding(.trailing, 8)
        .background(
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .fill(isHovered ? Color.primary.opacity(0.04) : .clear)
        )
        .contentShape(Rectangle())
        .onHover { hovering in
            hoveredRowID = hovering ? "__start__" : (hoveredRowID == "__start__" ? nil : hoveredRowID)
        }
        .contextMenu {
            if !isActive {
                Button("Start fresh (empty context)") {
                    triggerNavigate(nodeID: nil, edit: false)
                }
                .disabled(store.isBusy || isNavigating || actions.navigateTree == nil)
            }
        }
    }

    private func treeRowView(_ row: PCodeConversationTreeRow) -> some View {
        let isInViewport = row.isOnActivePath && store.visibleTurnID == row.id
        let isSelected = selectedNodeID == row.id
        let isHovered = hoveredRowID == row.id
        let canMutate = !store.isBusy && !isNavigating && !row.node.isSyntheticLive && actions.navigateTree != nil

        return HStack(spacing: 0) {
            // Left scroll-spy bar (shows which turn is currently in the transcript viewport).
            RoundedRectangle(cornerRadius: 1.5, style: .continuous)
                .fill(isInViewport ? Color.accentColor : .clear)
                .frame(width: 2.5)
                .padding(.vertical, 4)

            // Git-graph rail columns for forks.
            if !row.railColumns.isEmpty {
                HStack(spacing: 0) {
                    ForEach(Array(row.railColumns.enumerated()), id: \.offset) { _, segment in
                        railSegmentView(segment)
                    }
                }
                .padding(.leading, 4)
            }

            // Node status glyph.
            nodeIndicator(for: row)
                .frame(width: 16, alignment: .center)
                .padding(.leading, row.railColumns.isEmpty ? 6 : 2)

            // Row body.
            VStack(alignment: .leading, spacing: 2.5) {
                HStack(spacing: 5) {
                    if row.node.kind == .turn {
                        Text("#\(row.turnNumber)")
                            .font(.system(size: 10, weight: .medium, design: .monospaced))
                            .foregroundStyle(row.isOnActivePath ? Color.secondary : Color.secondary.opacity(0.55))
                    } else if row.node.kind == .aside {
                        Text("/btw")
                            .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                            .foregroundStyle(Color.purple.opacity(0.9))
                            .padding(.horizontal, 4)
                            .padding(.vertical, 0.5)
                            .background(
                                Capsule().fill(Color.purple.opacity(0.14))
                            )
                    } else {
                        Text("Compacted")
                            .font(.system(size: 9.5, weight: .semibold))
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 4)
                            .padding(.vertical, 0.5)
                            .background(
                                Capsule().fill(Color.primary.opacity(0.08))
                            )
                    }

                    Text(rowTitle(row.node))
                        .font(.system(size: 11.5, weight: row.isActiveHead || isInViewport ? .semibold : .medium))
                        .foregroundStyle(row.isOnActivePath ? .primary : .secondary)
                        .lineLimit(2)

                    Spacer(minLength: 4)

                    if row.isActiveHead {
                        headBadge
                    } else if isInViewport {
                        viewportBadge
                    }
                }

                // Second line: assistant excerpt + compact metadata chips.
                HStack(spacing: 6) {
                    let excerpt = row.node.responseExcerpt
                    if !excerpt.isEmpty {
                        Text(excerpt)
                            .font(.system(size: 10.5))
                            .foregroundStyle(.secondary.opacity(row.isOnActivePath ? 0.85 : 0.6))
                            .lineLimit(1)
                    }

                    Spacer(minLength: 4)

                    if row.node.toolCount > 0 {
                        metaChip(
                            icon: "wrench",
                            text: "\(row.node.toolCount)",
                            tint: row.node.failedToolCount > 0 ? .orange : .secondary
                        )
                    }
                    if row.node.editCount > 0 {
                        metaChip(
                            icon: "doc.badge.plus",
                            text: "\(row.node.editCount)",
                            tint: .secondary
                        )
                    }
                    if row.node.status == .failed {
                        statusChip("Failed", color: .red)
                    } else if row.node.status == .cancelled {
                        statusChip("Cancelled", color: .orange)
                    } else if row.node.status == .running {
                        statusChip("Live", color: .accentColor)
                    }
                }

                // Collapsed inactive branch toggle pill.
                if row.isCollapsibleBranchRoot {
                    Button {
                        toggleBranchCollapse(row.id)
                    } label: {
                        HStack(spacing: 4) {
                            Image(systemName: row.isCollapsed ? "chevron.right" : "chevron.down")
                                .font(.system(size: 8.5, weight: .semibold))
                            Text(
                                row.isCollapsed
                                    ? "Show \(row.collapsedDescendantCount) more turn\(row.collapsedDescendantCount == 1 ? "" : "s")"
                                    : "Collapse branch"
                            )
                            .font(.system(size: 10, weight: .medium))
                        }
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(
                            Capsule().fill(Color.primary.opacity(0.06))
                        )
                    }
                    .buttonStyle(.plain)
                    .padding(.top, 1)
                }

                // Inline quick-action strip when selected (never on hover, preventing vertical jitter).
                if isSelected && !row.node.isSyntheticLive {
                    HStack(spacing: 6) {
                        if row.isOnActivePath {
                            rowActionButton("Jump", icon: "arrow.up.and.down.text.horizontal") {
                                actions.scrollToTurn(row.id)
                            }
                        }
                        if row.node.kind == .turn && !row.node.prompt.isEmpty {
                            rowActionButton("Edit & fork", icon: "arrow.triangle.branch", disabled: !canMutate) {
                                triggerNavigate(nodeID: row.id, edit: true)
                            }
                        }
                        if !row.isActiveHead {
                            rowActionButton("Continue", icon: "play.fill", disabled: !canMutate) {
                                triggerNavigate(nodeID: row.id, edit: false)
                            }
                        }
                    }
                    .padding(.top, 2)
                }
            }
            .padding(.leading, 4)
            .padding(.trailing, 8)
            .padding(.vertical, 6)
        }
        .background(
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .fill(rowBackground(isInViewport: isInViewport, isSelected: isSelected, isHovered: isHovered))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .strokeBorder(
                    isSelected ? Color.accentColor.opacity(0.45) : .clear,
                    lineWidth: 1
                )
        )
        .contentShape(Rectangle())
        .onTapGesture {
            selectedNodeID = row.id
            if row.isOnActivePath {
                actions.scrollToTurn(row.id)
            }
        }
        .onHover { hovering in
            hoveredRowID = hovering ? row.id : (hoveredRowID == row.id ? nil : hoveredRowID)
        }
        .contextMenu {
            if row.isOnActivePath {
                Button("Scroll to turn") {
                    actions.scrollToTurn(row.id)
                }
            }
            if row.node.kind == .turn && !row.node.prompt.isEmpty {
                Button("Edit & fork from here") {
                    triggerNavigate(nodeID: row.id, edit: true)
                }
                .disabled(!canMutate)
            }
            if !row.isActiveHead {
                Button("Continue from here") {
                    triggerNavigate(nodeID: row.id, edit: false)
                }
                .disabled(!canMutate)
            }
            Divider()
            if !row.node.prompt.isEmpty {
                Button("Copy prompt") {
                    actions.copyToClipboard(row.node.prompt)
                }
            }
            if !row.node.response.isEmpty {
                Button("Copy response") {
                    actions.copyToClipboard(row.node.response)
                }
            }
        }
    }

    // MARK: - Inactive Branch Preview Card

    private func inactivePreviewCard(_ node: PCodeConversationTurnNode) -> some View {
        let canMutate = !store.isBusy && !isNavigating && !node.isSyntheticLive && actions.navigateTree != nil
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: "arrow.triangle.branch")
                    .font(.system(size: 10.5, weight: .semibold))
                    .foregroundStyle(.secondary)
                Text("Inactive branch preview")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.primary)
                Spacer(minLength: 4)
                Button {
                    selectedNodeID = nil
                } label: {
                    Image(systemName: "xmark")
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(.secondary)
                        .frame(width: 18, height: 18)
                }
                .buttonStyle(.plain)
            }

            ScrollView {
                VStack(alignment: .leading, spacing: 6) {
                    if !node.prompt.isEmpty {
                        Text(node.prompt)
                            .font(.system(size: 11.5, weight: .medium))
                            .foregroundStyle(.primary)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(8)
                            .background(
                                RoundedRectangle(cornerRadius: 6, style: .continuous)
                                    .fill(Color.primary.opacity(colorScheme == .dark ? 0.08 : 0.05))
                            )
                    }
                    if !node.response.isEmpty {
                        Text(node.response)
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    } else {
                        Text(node.responseExcerpt)
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                            .italic()
                    }
                }
            }
            .frame(maxHeight: 160)

            HStack(spacing: 8) {
                Button {
                    triggerNavigate(nodeID: node.id, edit: false)
                } label: {
                    Label("Continue from here", systemImage: "play.fill")
                        .font(.system(size: 11, weight: .medium))
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                .disabled(!canMutate)

                if node.kind == .turn && !node.prompt.isEmpty {
                    Button {
                        triggerNavigate(nodeID: node.id, edit: true)
                    } label: {
                        Label("Edit & fork", systemImage: "arrow.triangle.branch")
                            .font(.system(size: 11, weight: .medium))
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .disabled(!canMutate)
                }

                Spacer(minLength: 4)
            }
        }
        .padding(10)
        .background(Color.primary.opacity(colorScheme == .dark ? 0.04 : 0.025))
    }

    // MARK: - Subviews & Helpers

    private var emptyState: some View {
        VStack(spacing: 8) {
            Image(systemName: "point.3.connected.trianglepath.dotted")
                .font(.system(size: 24, weight: .light))
                .foregroundStyle(.secondary.opacity(0.7))
            Text("No conversation turns yet")
                .font(.system(size: 12.5, weight: .medium))
                .foregroundStyle(.primary)
            Text("Send a message to build the conversation tree. Click any active turn to jump to it, or fork from earlier points.")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 240)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(20)
    }

    @ViewBuilder
    private func nodeIndicator(for row: PCodeConversationTreeRow) -> some View {
        switch row.node.kind {
        case .compaction:
            Image(systemName: "arrow.triangle.2.circlepath")
                .font(.system(size: 9.5, weight: .semibold))
                .foregroundStyle(row.isOnActivePath ? Color.accentColor : .secondary)
        case .aside:
            Image(systemName: "bubble.left.and.bubble.right")
                .font(.system(size: 9, weight: .medium))
                .foregroundStyle(Color.purple.opacity(0.85))
        case .turn:
            ZStack {
                if row.isActiveHead {
                    Circle()
                        .fill(Color.accentColor.opacity(0.22))
                        .frame(width: 13, height: 13)
                    Circle()
                        .fill(Color.accentColor)
                        .frame(width: 7, height: 7)
                } else if row.isOnActivePath {
                    Circle()
                        .fill(Color.accentColor.opacity(0.85))
                        .frame(width: 6, height: 6)
                } else {
                    Circle()
                        .strokeBorder(Color.secondary.opacity(0.55), lineWidth: 1.25)
                        .frame(width: 6.5, height: 6.5)
                }
            }
        }
    }

    private func railSegmentView(_ segment: PCodeConversationTreeRow.RailSegment) -> some View {
        Canvas { ctx, size in
            let midX = size.width / 2
            let midY = size.height / 2
            let lineWidth: CGFloat = 1.25
            switch segment {
            case .empty:
                break
            case .vertical(let active):
                var path = Path()
                path.move(to: CGPoint(x: midX, y: 0))
                path.addLine(to: CGPoint(x: midX, y: size.height))
                ctx.stroke(
                    path,
                    with: .color(active ? Color.accentColor.opacity(0.55) : Color.secondary.opacity(0.28)),
                    lineWidth: lineWidth
                )
            case .branch(let active, let isLast):
                var path = Path()
                path.move(to: CGPoint(x: midX, y: 0))
                path.addLine(to: CGPoint(x: midX, y: isLast ? midY : size.height))
                path.move(to: CGPoint(x: midX, y: midY))
                path.addLine(to: CGPoint(x: size.width, y: midY))
                ctx.stroke(
                    path,
                    with: .color(active ? Color.accentColor.opacity(0.65) : Color.secondary.opacity(0.32)),
                    lineWidth: lineWidth
                )
            }
        }
        .frame(width: 12)
    }

    private var headBadge: some View {
        Text("HEAD")
            .font(.system(size: 8.5, weight: .bold, design: .monospaced))
            .foregroundStyle(Color.accentColor)
            .padding(.horizontal, 4.5)
            .padding(.vertical, 1)
            .background(
                Capsule().fill(Color.accentColor.opacity(0.16))
            )
    }

    private var viewportBadge: some View {
        Text("IN VIEW")
            .font(.system(size: 8.5, weight: .semibold, design: .monospaced))
            .foregroundStyle(.secondary)
            .padding(.horizontal, 4.5)
            .padding(.vertical, 1)
            .background(
                Capsule().fill(Color.primary.opacity(0.08))
            )
    }

    private func metaChip(icon: String, text: String, tint: Color) -> some View {
        HStack(spacing: 2) {
            Image(systemName: icon)
                .font(.system(size: 8.5))
            Text(text)
                .font(.system(size: 9.5, design: .monospaced))
        }
        .foregroundStyle(tint.opacity(0.85))
    }

    private func statusChip(_ label: String, color: Color) -> some View {
        Text(label)
            .font(.system(size: 9, weight: .semibold))
            .foregroundStyle(color)
            .padding(.horizontal, 4.5)
            .padding(.vertical, 0.5)
            .background(
                Capsule().fill(color.opacity(0.14))
            )
    }

    private func rowActionButton(
        _ title: String,
        icon: String,
        disabled: Bool = false,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            HStack(spacing: 3) {
                Image(systemName: icon)
                    .font(.system(size: 9))
                Text(title)
                    .font(.system(size: 10, weight: .medium))
            }
            .foregroundStyle(disabled ? Color.secondary.opacity(0.45) : Color.primary.opacity(0.85))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(
                RoundedRectangle(cornerRadius: 4, style: .continuous)
                    .fill(Color.primary.opacity(colorScheme == .dark ? 0.09 : 0.06))
            )
        }
        .buttonStyle(.plain)
        .disabled(disabled)
    }

    private func rowTitle(_ node: PCodeConversationTurnNode) -> String {
        if node.kind == .compaction {
            return node.prompt.isEmpty ? "Context compaction" : node.promptExcerpt
        }
        let excerpt = node.promptExcerpt
        return excerpt.isEmpty ? "(continuation)" : excerpt
    }

    private func rowBackground(isInViewport: Bool, isSelected: Bool, isHovered: Bool) -> Color {
        if isInViewport {
            return Color.accentColor.opacity(colorScheme == .dark ? 0.13 : 0.09)
        }
        if isSelected {
            return Color.primary.opacity(colorScheme == .dark ? 0.10 : 0.07)
        }
        if isHovered {
            return Color.primary.opacity(colorScheme == .dark ? 0.06 : 0.04)
        }
        return .clear
    }

    private func toggleBranchCollapse(_ nodeID: String) {
        if collapsedBranchRoots.contains(nodeID) {
            collapsedBranchRoots.remove(nodeID)
        } else {
            collapsedBranchRoots.insert(nodeID)
        }
    }

    private func triggerNavigate(nodeID: String?, edit: Bool) {
        guard let navigateTree = actions.navigateTree else { return }
        statusBanner = nil
        isNavigating = true
        navigateTree(nodeID, edit) { result in
            isNavigating = false
            switch result {
            case .success(let draft):
                if edit, !draft.isEmpty {
                    actions.insertIntoComposer(draft)
                }
                selectedNodeID = nil
            case .failure(let error):
                statusBanner = error.localizedDescription
            }
        }
    }
}
