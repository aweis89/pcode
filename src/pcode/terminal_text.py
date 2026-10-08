"""Keep untrusted prose from supplying terminal control characters."""


def safe_text(text: str) -> str:
    """Neutralize C0/C1 controls before rendering, not Rich's generated escapes.

    Keep newlines and expand tabs as the streaming display does. Replacing each
    control independently also works when an escape spans provider chunks; do
    not interpret ANSI or buffer incomplete sequences. Printable Unicode and
    literal backslash escape examples remain unchanged. This is presentation
    only: stored model messages and piped source output keep their original text.
    """
    return "".join(
        "    "
        if char == "\t"
        else char
        if char == "\n" or ord(char) >= 32 and not 127 <= ord(char) < 160
        else "�"
        for char in text
    )
