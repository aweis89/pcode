# Soft-launch post

## Suggested title

I built pcode, an open-source terminal coding agent with adjustable scrollback

## Body

I’ve been building pcode, a terminal coding agent on Pydantic AI. **You can use it with your existing Claude or ChatGPT subscription, or bring API keys.** I’m obviously biased, but it’s become my favorite way to work. I built it around my own preferences, so I’m curious whether they translate to anyone else.

One feature I particularly like: you can change what’s visible in your terminal scrollback, even after the work is done. Show commands, output, and diffs when you want the details, then hide them to read the conversation. It rebuilds the history already on screen without rerunning anything.

It uses your terminal’s native scrollback, so normal scrolling, search, and copy still work. The transcript reflows when you resize or split panes, it follows your terminal between dark and light themes in auto mode, and a live task panel keeps ongoing work visible above the prompt.

It’s also highly extensible: small Python files can add tools, slash commands, guardrails, and sub-agents, and /reload picks up changes without restarting. Extensions are built on [Pydantic AI](https://ai.pydantic.dev/), so anything a Pydantic AI capability can do, a pcode extension can do.

There’s quite a bit built in: a pi-inspired /tree for rewinding and branching conversations, customizable keybindings (including leader keys), Vim editing mode, and a /diffs popup for reviewing git diffs without leaving the conversation. Background commands wake the agent when they finish, and separate git worktrees let you run sessions in parallel.

There’s also a Gmail-based email mode on macOS: send tasks from your phone when you step away, reply to continue, and pick the session back up at your terminal later.

Next on my roadmap: transferring active sessions between your local machine and remote environments, in either direction, without losing the conversation.

Repo, screenshots, and install instructions: https://github.com/cruxwell/pcode

If you already use a terminal coding agent, I’d love some blunt feedback. What would make this worth trying, and what’s missing?

![pcode in dark and light terminal themes, side by side](assets/soft-launch-terminal-themes.png)

## Screenshot asset

The current composite uses matching 2784 × 3044 captures from October 8, 2026:
3:05:57 PM (dark, left) and 3:06:27 PM (light, right). Neither capture is resized;
the composite adds a neutral border and gap. This replaces the earlier mismatched
pair in the reusable draft and the new r/CodingAgents post, not the older
r/ChatGPTCoding comment.

## Posting history

- October 8, 2026: published and revised as a comment in r/ChatGPTCoding’s weekly self-promotion thread, with the dark and light screenshots combined side by side. The suggested title was not used for the comment.
  https://www.reddit.com/r/ChatGPTCoding/comments/1wy2qk8/comment/peq7l5h/
- October 8, 2026: published a standalone r/CodingAgents post using the title above
  and the replacement, matched-size screenshot pair.
  https://www.reddit.com/r/CodingAgents/comments/1x14fn1/
- October 8, 2026: revised both entries to lead with subscription support and add
  email mode plus local/remote session transfer as a roadmap item, not a shipped
  feature. Reloaded both to verify text, formatting, repository link, and images.

## Community checks

Checked October 8, 2026. Recheck rules before another post; permission here is not
permission everywhere, and a successful submission is not moderator approval.

- r/CodingAgents: directly relevant, though small. Rules welcome projects with a
  demo, explanation, or open-source contribution; no blind ads. Native images
  supported. No flair required by the composer.
  https://www.reddit.com/r/CodingAgents/about/rules/
- r/ChatGPTCoding: use the weekly self-promotion thread, not a standalone ad.
  Comments support one image, so combine a pair before uploading.
- r/SideProject: the composer rejected images; not a destination for this image-led
  post without changing the format.
- r/commandline: rules exclude this generative-AI project and AI-written posts.
- r/opensource: no images; rules also prohibit AI-generated content.
- r/AI_Agents: project links belong in the weekly project display thread, with
  participation beyond promotion expected. No native images.
- r/AgentsOfAI and r/alphaandbetausers: native images disabled when checked.
- r/LLMDevs: images supported, but commercial/FOSS requirements, feedback-data
  disclosures, and AI-content attribution rules need a separately tailored post.
- r/ClaudeCode: simple project shares go in the weekly showcase; standalone posts
  must explain how Claude Code was used and what was learned.
- r/vibecoding: dev tools require prior moderator approval through its X community.
- r/aipromptprogramming: Showcase Sunday and existing participation required;
  copy-pasted generated posts do not count as contributions.

## Posting mechanics

- Read rules and image settings through signed-in, same-origin Reddit JSON fetches
  when public retrieval fails. Inspect sidebar and pinned guidance too.
- macOS PNG clipboard plus browser paste works for image attachments. A localhost
  image-fetch workaround was blocked by Reddit's Content Security Policy.
- Full rich-text replacement can retain old nodes or collapse paragraph breaks.
  Editing the Lexical editor state preserves existing images, links, and bold
  text; verify the actual saved result after reloading, not just the draft.
- Keep form inspection scoped to title, body, and media. Never dump hidden input
  values, which can include temporary tokens.
