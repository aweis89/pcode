"""Session selectors, without `pcode.sessions`.

That module imports Pydantic, whose first model scans every installed
package's entry points for plugins (~40 ms together). A terminal attaching to
a running host checks its `-c` argument long before it reads any session.
"""

import re


def is_session_selector(value: str) -> bool:
    """Tell an ID/prefix apart from prompt text after an optional-argument flag."""
    return value == "latest" or bool(re.fullmatch(r"[a-f0-9-]{8,36}", value))
