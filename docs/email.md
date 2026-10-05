# Email remote control

`pcode --email-listen` lets you hand pcode tasks by email from your phone or
any Gmail client. A new email starts a session, and replying continues it.
Each session is an ordinary [background session](sessions.md#background-sessions),
so you can pick it up at a terminal later with `pcode --attach`.

!!! warning "Unattended work"
    Email-started sessions run with nobody watching, so they run under a fixed,
    locked-down profile (below). Nothing in an email can change it. Check the
    worktree before you merge anything a session produced.

## Setup

You need a Gmail account with 2-Step Verification turned on, and macOS (the
app password goes in the keychain; there is no plaintext fallback).

1. Create an app password at <https://myaccount.google.com/apppasswords>.
2. Run `pcode --email-setup you@gmail.com`. Paste the app password at the
   keychain prompt. pcode signs in once to check it, shows the profile below,
   and asks you to confirm. Use the exact address Gmail shows as your sender:
   mail whose `From` differs (a dotted variant, `googlemail.com`) is ignored.

The keychain item trusts no application, so macOS asks before anything reads
it, `pcode --email-listen` included. Click **Allow** each time you start
listening rather than **Always Allow**: that keeps any other program from
reading it silently.

## Using it

```text
pcode --email-listen             # in the repository to expose; --email-ttl 8h by default
```

pcode emails you a launcher message. Then:

| You send | What happens |
| --- | --- |
| A reply to the launcher with a task | A new session starts in its own worktree |
| A reply to a session's email | That session continues |
| A new email to the address the launcher replies to | Another independent session (the "Start new task" link does this) |
| `/status` as the whole text | That session's state (in a fresh email: every session's) |
| `/stop` as the whole text, replying to a session | Its running turn and anything queued are cancelled; the session stays open |

A task is sent to the model as you wrote it, so a leading `$provider:model` or
`+effort` word picks the model or effort for that turn, as at the terminal.

You get one email when a turn finishes, with the reply, how it ended, the
worktree and branch, and a `git diff --stat`. A turn that runs longer than 20
seconds also gets a "started" email first. Attachments are ignored (and the
reply says so).

Replies are formatted: the model's Markdown shows as headings, lists, tables
and syntax-highlighted code in Gmail, with the plain-text version kept for
other clients. Images in a reply become links rather than loading when you
open the email.

`Ctrl+C`, or the end of `--email-ttl`, stops listening and stops every session.
Transcripts and worktrees are kept. Each run gets a fresh address, so mail
to an earlier run's address is ignored.

## What it accepts

Only mail you sent from the account itself, to that run's address. Gmail
labels mail you send as `SENT`, which mail delivered from anyone else can't
carry, even with your address forged in `From`. Everything else (other
senders, auto-replies, mailing lists, bounces, pcode's own mail) is ignored
without a reply and never reaches the model. Replies only ever go to you.

## The remote profile

| | |
| --- | --- |
| Sandbox | The bundled sandbox is on, whatever `/extensions` says: writes stay in the session's worktree, its own branch and caches (never the main checkout or git's config), credential files and the listener's own state are unreadable, and shell commands can't reach the keychain or another session's host. |
| Shell mode | `!command` input is refused, even from a terminal attached with `pcode --attach`, since it runs outside the sandbox. |
| Sub-agents | Delegated work runs in the session's worktree; isolated worker worktrees are off. |
| Worktree | Always a fresh one, branched from what you have checked out. It's kept afterwards and never merged automatically. |
| Environment | Only allowlisted variables (provider API keys, `PATH`, `HOME`, locale), not your shell's full environment. |
| MCP servers | Off, since they run outside the sandbox. `email_mcp` turns on your default servers. |
| Per turn | 30 minutes, 100 model requests and 100 tool calls, sub-agents included. A turn that reaches one stops and says so. |
| Project code | A repository's extensions and `worktree-setup` run only if you already [trusted](configuration.md#trusting-a-repositorys-own-code) it. |

The limits, and how many sessions and queued emails a run accepts, are
[settings](configuration.md#email-remote-control). They're user-only, so a
repository can't loosen them.
