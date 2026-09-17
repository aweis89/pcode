"""Conversation paths, independent of model-message and tool-effect storage."""

from copy import deepcopy
from dataclasses import dataclass, field


@dataclass
class TurnNode:
    id: str
    parent: str | None
    prompt: str
    status: str = "interrupted"
    response: str = ""
    plan: list[dict] = field(default_factory=list)
    # Only unsaved sessions need an in-memory message checkpoint.
    history: list | None = None


class ConversationTree:
    def __init__(self) -> None:
        self.nodes: dict[str, TurnNode] = {}
        self.active: str | None = None
        self.recording: str | None = None

    def path(self, identity: str | None) -> list[str]:
        path = []
        while identity is not None:
            if identity in path or identity not in self.nodes:
                raise ValueError("Invalid conversation tree path.")
            path.append(identity)
            identity = self.nodes[identity].parent
        return path[::-1]

    def consume(self, record: dict) -> None:
        kind = record.get("kind")
        if kind == "turn_started" and record.get("run_id"):
            identity = record["run_id"]
            parent = record.get("parent_id", self.active)  # Old sessions form a linear tree.
            self.path(parent)
            self.nodes[identity] = TurnNode(
                identity,
                parent,
                record["prompt"],
                plan=deepcopy(self.nodes[parent].plan) if parent else [],
            )
            self.active = self.recording = identity
        elif kind == "tree_selected":
            self.path(record["node_id"])
            self.active = record["node_id"]
        elif self.recording is not None:
            node = self.nodes[self.recording]
            if kind == "Message":
                node.response = record["markdown"]
            elif kind == "PlanUpdated":
                node.plan = deepcopy(record["items"])
            elif kind in {"turn_completed", "turn_failed", "turn_cancelled"}:
                node.status = kind.removeprefix("turn_")

    def rows(self) -> list[tuple[tuple[str | None, bool], str]]:
        """Depth-first user/edit and assistant/continue rows, with stable IDs."""
        from pcode.diagnostics import redact
        from pcode.ui import plain

        def excerpt(text):
            return plain(redact(" ".join(text.split())), 100)

        rows = [
            ((None, False), "Conversation start" + (" ← active" if self.active is None else ""))
        ]
        children: dict[str | None, list[TurnNode]] = {}
        for node in self.nodes.values():
            children.setdefault(node.parent, []).append(node)
        # Iterative traversal also handles very long sessions. Only a fork adds
        # a level: user/assistant pairs and single-child continuations stay aligned.
        roots = children.get(None, [])
        pending = [
            (node, "", len(roots) > 1, i == len(roots) - 1)
            for i, node in reversed(list(enumerate(roots)))
        ]
        while pending:
            node, prefix, fork, last = pending.pop()
            # Keep labels visible even with many nested forks.
            if len(prefix) > 24:
                prefix = "… " + prefix[-22:]
            connector = ("└─ " if last else "├─ ") if fork else ""
            rows.append(
                (
                    (node.id, True),
                    prefix + connector + "user: " + excerpt(node.prompt),
                )
            )
            continuation = prefix + (("   " if last else "│  ") if fork else "")
            rows.append(
                (
                    (node.id, False),
                    continuation
                    + "assistant: "
                    + excerpt(node.response or f"[{node.status}; last safe checkpoint]")
                    + (f" [{node.status}]" if node.response and node.status != "completed" else "")
                    + (" ← active" if node.id == self.active else ""),
                )
            )
            descendants = children.get(node.id, [])
            pending.extend(
                (child, continuation, len(descendants) > 1, i == len(descendants) - 1)
                for i, child in reversed(list(enumerate(descendants)))
            )
        return rows
