"""Describing a failure must not fail.

`pcode.live.error_message` sanitizes an exception for the transcript, but it
lives in the package's heaviest module, so callers import it lazily -- inside
the `except` block that needs it. That import can itself raise. A session
whose source tree is deleted underneath it (a worktree merged and removed from
elsewhere) answers `No module named 'pcode.live'`, and because the failure
happens in the handler it escapes, kills the task that was reporting, and
loses the original error. Import from here instead of from `pcode.live`: this
module has no imports of its own, so it is resident before anything goes
wrong.
"""


def error_message(error: Exception, *, unexpected: str | None = None) -> str:
    """`pcode.live.error_message`, degrading to a description we can always build."""
    try:
        from pcode.live import error_message as describe

        return describe(error, unexpected=unexpected)
    except Exception as failure:  # noqa: BLE001 - the caller is already handling an error.
        return _undescribed(error, failure, unexpected)


def _undescribed(error: Exception, failure: Exception, unexpected: str | None) -> str:
    """Name both failures. Never `str(error)`: sanitizing it is what just broke."""
    opening = unexpected or "Run failed"
    message = f"{opening} ({type(error).__name__}), and describing it failed "
    message += f"({type(failure).__name__})."
    if isinstance(failure, ImportError) and (failure.name or "").split(".")[0] == "pcode":
        message += (
            " pcode cannot load its own source any more: the directory it runs from was"
            " deleted, usually a session worktree removed from elsewhere. Restart pcode"
            " from a directory that still exists."
        )
    else:
        message += " See the saved session diagnostics."
    return message
