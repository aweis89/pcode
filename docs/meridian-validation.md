# Meridian integration validation

## Verified versions

Validated on 2026-09-17 with Meridian 1.71.1, Pydantic AI 2.44.0, and
Harness 0.31.0. See [dependency guidance](dependencies.md) before repeating
against another release.

## Automated coverage

```sh
uv run pytest -q tests/test_meridian.py tests/test_thinking.py tests/test_delegation.py tests/test_sessions.py
```

The mock HTTP transport exercises actual Anthropic request serialization and SSE
decoding, not just header-building helpers:

- Tool-result rounds keep the parent conversation header.
- A reconstructed agent using prior model-message history keeps that identity.
- A new conversation gets a new identity.
- Parallel Harness explorer runs inherit the model but use independent identities,
  stable across their own tool rounds.
- Request-local headers do not mutate shared settings; other providers are untouched.
- Forwarded thinking blocks reach the transient runtime sink and `Thinking…`
  status without entering application transcript events. A stream without thinking
  still completes normally. This does not remove reasoning from saved native
  model-message history.

Validation results: the targeted suite above passed **87 tests**. A full
`uv run pytest -q` run passed **819 tests** with three dependency/platform
deprecation warnings, including real-tmux regressions. The full run took place
in a shared checkout while another session was editing the renderer; those
renderer changes are not part of the Meridian commits. Ruff checks and formatting
checks passed for the modified Python files.

## Live smoke test

An isolated, side-effect-free probe used `meridian:claude-fable-5-1`, one
`integration_echo` tool, `MeridianSessionIdentity`, an explicit unique conversation
ID, low effort, and request-level disabled thinking. It asked the model to call
the tool once and reply `OK`. A second agent/model instance restored the returned
message history and requested `OK` without tools. It did not resume or interrupt
the user's active session and did not modify Meridian's global settings.

Meridian telemetry reported:

| Request | Lineage | Resume | HTTP | Duration | Cache hit rate |
|---|---|---|---|---|---|
| Initial tool call | new | false | 200 | 2.617 s | 0% |
| Tool result follow-up | continuation | true | 200 | 2.805 s | 89.1% |
| Reconstructed agent with history | continuation | true | 200 | 3.167 s | 96.3% |

Request IDs, for local telemetry correlation:

- `222cf626-1ac6-45c5-bd20-90fa40b85b26`
- `d6dd58ef-ef58-40ab-9249-18dac473ff32`
- `646c4b69-1f58-4a67-8d18-4c230638a471`

This establishes native continuation through the installed proxy, not a latency
benchmark for the original large conversation. Live parallel delegates and a
complete saved-session UI restart were not exercised by this probe; automated
coverage tests delegation and saved-session behavior separately.

## Thinking visibility and remaining checks

A read-only query of the running proxy's `/settings/api/features` confirmed
`passthrough.thinkingPassthrough` was `false`. It also reported the default
`thinking: "disabled"`; neither token totals nor this setting alone establish
whether the upstream model performed hidden reasoning during the earlier pause.

To inspect settings and request telemetry without changing anything:

```sh
curl -fsS http://127.0.0.1:3456/settings/api/features
curl -fsS 'http://127.0.0.1:3456/telemetry/requests?limit=10'
```

Use the appropriate base URL/authentication for a non-default proxy. To receive
thinking, enable **Thinking Passthrough** for the **passthrough** adapter in
Meridian's `/settings` UI, then use `/show-thinking on` in pcode. This setting is
shared with other passthrough clients, so it is deliberately not enabled by pcode.
Actual upstream thinking delivery with that setting enabled remains unverified;
the client-side forwarded/filtered cases are covered with synthetic SSE streams.

Already-running Python processes retain the old integration. After the active
turn completes (or after explicitly cancelling it), exit pcode and resume with:

```sh
pcode --resume <session-id>
```

The first request after migrating a previously headerless conversation may need
a fresh upstream session. Inspect subsequent tool rounds for `continuation` and
`isResume: true`, rather than expecting the first request to resume an old proxy
mapping. Compare long-task latency only after that restart; do not infer a
performance improvement from the tiny smoke-test timings.
