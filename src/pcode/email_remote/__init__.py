"""`pcode --email-listen`: a Gmail account as a remote control for session hosts.

A new email to the listener's alias starts a session (an ordinary host, under
`pcode.remote_profile`'s fixed profile, in its own worktree); a reply continues
it. Only mail the owner sent from their own account is accepted: Gmail labels
it `SENT`, which nothing delivered from outside can carry. See
`dev/email-remote.md` for the design and the checks behind it.

- `parsing`: one raw message to headers and the new text in its body
- `mailbox`: Gmail over IMAP and SMTP, and the keychain credential
- `state`: what one listener has handled, routed and sent, on disk
- `outbound`: the messages pcode sends
- `listener`: acceptance, routing, the sessions, and the loop
"""
