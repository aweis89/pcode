"""FETCH responses in the fragmented form returned by imaplib, without Gmail."""

import imaplib

import pytest

from pcode.email_remote.mailbox import GmailMailbox, Meta


def mailbox(monkeypatch, data):
    client = GmailMailbox("owner@example.test", "unused")
    calls = []

    def uid(*args):
        calls.append(args)
        return data

    monkeypatch.setattr(client, "_uid", uid)
    return client, calls


def literal(prefix, value):
    return prefix + b"{" + str(len(value)).encode() + b"}", value


def test_meta_joins_multiple_literal_labels_and_continuations(monkeypatch):
    data = [
        b"1 (UID 8 X-GM-MSGID 88 X-GM-LABELS (\\Inbox) RFC822.SIZE 90)",
        literal(b"2 (X-GM-MSGID 99 X-GM-LABELS (\\Sent ", b"a (literal) label"),
        literal(b" ", b"another label"),
        b' "quoted \\"label\\"" \\Inbox) RFC822.SIZE 321 UID 42)',
        b"3 (UID 9 FLAGS (\\Seen))",
    ]
    client, calls = mailbox(monkeypatch, data)
    meta = client.meta("42")
    assert meta == Meta(
        "99",
        frozenset({"\\Sent", "\\Inbox", "a (literal) label", "another label", 'quoted "label"'}),
        321,
    )
    assert meta.sent
    assert calls == [("FETCH", "42", "(X-GM-MSGID X-GM-LABELS RFC822.SIZE)")]


@pytest.mark.parametrize("encoding", ["literal", "quoted"])
@pytest.mark.parametrize(
    "label",
    [
        b"not \\Sent",
        b") X-GM-LABELS (\\Sent) (",
        b") UID 42 X-GM-MSGID 999 RFC822.SIZE 1 (",
        b'quoted " and \\ backslash (\\Sent)',
        b"\r\n2 (UID 42 X-GM-LABELS (\\Sent) X-GM-MSGID 999 RFC822.SIZE 1)",
    ],
)
def test_label_text_cannot_impersonate_sent_or_metadata(monkeypatch, encoding, label):
    prefix = b"2 (UID 42 X-GM-LABELS (\\Inbox "
    suffix = b") X-GM-MSGID 99 RFC822.SIZE 321)"
    if encoding == "literal":
        data = [literal(prefix, label), suffix]
    else:
        quoted = label.replace(b"\\", b"\\\\").replace(b'"', b'\\"')
        data = [prefix + b'"' + quoted + b'"' + suffix]
    client, _ = mailbox(monkeypatch, data)
    meta = client.meta("42")
    assert meta == Meta("99", frozenset({"\\Inbox", label.decode()}), 321)
    assert not meta.sent


def test_literal_sent_is_one_complete_label(monkeypatch):
    client, _ = mailbox(
        monkeypatch,
        [literal(b"1 (UID 42 X-GM-LABELS (", b"\\Sent"), b") X-GM-MSGID 99 RFC822.SIZE 1)"],
    )
    assert client.meta("42").sent


@pytest.mark.parametrize("method,field", [("headers", b"BODY[HEADER]"), ("fetch", b"BODY[]")])
@pytest.mark.parametrize("uid_first", [False, True])
def test_content_selects_requested_uid_even_when_uid_follows_literal(
    monkeypatch, method, field, uid_first
):
    prefix = b"4 (" + (b"UID 42 " if uid_first else b"")
    suffix = b")" if uid_first else b" UID 42)"
    content = b"Subject: test\r\n\r\nbody (UID 8)\x00\xff"
    data = [
        literal(b"1 (UID 8 " + field + b" ", b"unrelated"),
        b")",
        b"2 (UID 42 FLAGS (\\Seen))",
        literal(b"3 (UID 42 X-GM-LABELS (", b"label, not body"),
        b"))",
        literal(prefix + field + b" ", content),
        suffix,
        literal(b"5 (UID 9 " + field + b" ", b"also unrelated"),
        b")",
    ]
    client, calls = mailbox(monkeypatch, data)
    assert getattr(client, method)("42") == content
    request = "BODY.PEEK[HEADER]" if method == "headers" else "BODY.PEEK[]"
    assert calls == [("FETCH", "42", f"({request})")]


@pytest.mark.parametrize("method", ["meta", "headers", "fetch"])
def test_unrelated_uid_and_embedded_uid_do_not_match(monkeypatch, method):
    data = [
        literal(b"1 (UID 8 X-GM-LABELS (", b") UID 42 X-GM-LABELS (\\Sent"),
        b") X-GM-MSGID 99 RFC822.SIZE 1)",
        literal(b"2 (UID 8 BODY[] ", b"UID 42"),
        b")",
        literal(b"3 (UID 8 BODY[HEADER] ", b"UID 42"),
        b")",
    ]
    client, _ = mailbox(monkeypatch, data)
    with pytest.raises(imaplib.IMAP4.error, match="message 42"):
        getattr(client, method)("42")


@pytest.mark.parametrize("method", ["meta", "headers", "fetch"])
@pytest.mark.parametrize("data", [[None], [], [b"1 (UID 42 FLAGS (\\Seen))"]])
def test_missing_requested_data_fails_closed(monkeypatch, method, data):
    client, _ = mailbox(monkeypatch, data)
    with pytest.raises(imaplib.IMAP4.error):
        getattr(client, method)("42")


@pytest.mark.parametrize(
    "data",
    [
        [b'1 (UID 42 X-GM-LABELS ("unterminated) X-GM-MSGID 99 RFC822.SIZE 1)'],
        [(b"1 (UID 42 X-GM-LABELS ({8}", b"short"), b") X-GM-MSGID 99 RFC822.SIZE 1)"],
        [literal(b"1 (UID 42 X-GM-LABELS (", b"\\Sent")],
        [b"1 (UID 8 UID 42 X-GM-LABELS (\\Sent) X-GM-MSGID 99 RFC822.SIZE 1)"],
        [b'1 (UID 42 X-GM-LABELS (\\Sent) X-GM-MSGID "99" RFC822.SIZE 1)'],
        [b"1 (UID 42 X-GM-LABELS ((\\Sent)) X-GM-MSGID 99 RFC822.SIZE 1)"],
    ],
)
def test_malformed_metadata_fails_closed(monkeypatch, data):
    client, _ = mailbox(monkeypatch, data)
    with pytest.raises(imaplib.IMAP4.error):
        client.meta("42")


def test_meta_skips_same_uid_unsolicited_flags(monkeypatch):
    client, _ = mailbox(
        monkeypatch,
        [
            b"1 (UID 42 FLAGS (\\Seen))",
            b'1 (UID 42 X-GM-LABELS ("\\\\Sent") X-GM-MSGID 99 RFC822.SIZE 1)',
        ],
    )
    assert client.meta("42") == Meta("99", frozenset({"\\Sent"}), 1)


@pytest.mark.parametrize("method", ["meta", "headers", "fetch"])
def test_sequence_number_is_not_a_uid(monkeypatch, method):
    client, _ = mailbox(
        monkeypatch,
        [
            literal(b"42 (X-GM-MSGID 99 RFC822.SIZE 1 X-GM-LABELS (\\Sent) BODY[] ", b"body"),
            literal(b" BODY[HEADER] ", b"headers"),
            b")",
        ],
    )
    with pytest.raises(imaplib.IMAP4.error, match="message 42"):
        getattr(client, method)("42")


def test_label_cannot_supply_missing_numeric_fields(monkeypatch):
    client, _ = mailbox(
        monkeypatch,
        [
            literal(b"1 (UID 42 X-GM-LABELS (", b") X-GM-MSGID 99 RFC822.SIZE 1 (\\Sent"),
            b"))",
        ],
    )
    with pytest.raises(imaplib.IMAP4.error):
        client.meta("42")


def test_empty_body_literal_and_other_body_section(monkeypatch):
    client, _ = mailbox(
        monkeypatch,
        [
            literal(b"1 (UID 42 BODY[HEADER] ", b"headers"),
            literal(b" BODY[] ", b""),
            b")",
        ],
    )
    assert client.fetch("42") == b""
    assert client.headers("42") == b"headers"


def test_search_polls_before_searching_so_new_mail_is_visible():
    """A selected mailbox stays the snapshot taken at SELECT until the client polls."""

    class Connection:
        def __init__(self):
            self.calls = []

        def noop(self):
            self.calls.append("NOOP")
            return "OK", [b""]

        def uid(self, *args):
            self.calls.append(args[0])
            return "OK", [b"5 9"]

    client = GmailMailbox("owner@example.test", "unused")
    client._imap = connection = Connection()
    assert client.search("owner+pcode-x@example.test") == ["5", "9"]
    assert connection.calls == ["NOOP", "SEARCH"]


def test_app_password_drops_display_spaces_and_paste_markers():
    from pcode.email_remote.mailbox import app_password

    assert app_password("\x1b[200~abcd efgh ijkl mnop\x1b[201~\n") == "abcdefghijklmnop"


def test_rejected_login_is_a_setup_error_naming_the_likely_causes(monkeypatch):
    from pcode.email_remote import mailbox as module

    class Imap:
        def __init__(self, *args, **kwargs):
            pass

        def login(self, user, password):
            assert password == "abcdefghijklmnop"
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials (Failure)")

        def shutdown(self):
            pass

    monkeypatch.setattr(module.imaplib, "IMAP4_SSL", Imap)
    client = GmailMailbox("owner@example.test", "abcd efgh ijkl mnop")
    with pytest.raises(module.SetupError, match="signed in as owner@example.test"):
        client.verify()
