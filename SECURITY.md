# Security

pcode runs shell commands and edits files with your permissions, reads the
repositories you point it at, and stores provider credentials. Reports about
any of that are welcome.

## Reporting a vulnerability

Please report privately through
[GitHub security advisories](https://github.com/cruxwell/pcode/security/advisories/new),
not a public issue. Include the pcode version (`pcode --version`), what an
attacker controls (a repository's files, a web page the agent reads, an MCP
server, an email), and what they can get from it.

You should hear back within a week. pcode is an alpha maintained by one person,
so fixes land on `master` and the next release.

## What counts

In scope:

- Credentials or tokens leaking into transcripts, logs, or anything sent to a
  model or third party when they shouldn't be.
- Repository content running code before you've
  [trusted the repository](https://cruxwell.github.io/pcode/configuration/#trusting-a-repositorys-own-code).
- Escaping the [sandbox](https://cruxwell.github.io/pcode/tools/#write-policy-and-shell-sandbox-opt-in)
  when it's enabled, or bypassing a tool permission rule.
- Email remote control acting on mail from a sender it shouldn't accept.

Not a vulnerability by itself: the agent running a harmful command in live
mode. Live mode has no approval prompt by design; the docs on
[tool permissions](https://cruxwell.github.io/pcode/tools/#tool-permissions)
cover how to restrict it. Prompt injection that leads the agent to do something
you've allowed it to do is a known limit of every coding agent, though reports
that show a practical mitigation are welcome.
