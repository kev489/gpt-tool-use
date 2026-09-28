# GPT Tools MCP Server

MCP server that drives ChatGPT and Claude via one shared Playwright browser. Exposes `gpt_search` / `gpt_search_batch` (ChatGPT research), `gpt_image_gen` / `gpt_image_gen_batch` (ChatGPT image generation), and `provider_prompt_batch` (raw ChatGPT/Claude batch responses for local pipeline clients). Supports concurrent calls in separate tabs sharing one browser context.

## Architecture

- `mcp_server.py` — MCP server entry point. Server name is `gpt-tools`. Holds a single shared `ChatGPTBrowser` in module state, lazily initialized via `_get_browser()` (lock-protected so concurrent first-callers don't double-init). Each tool call calls `bot.new_session()` to get a fresh page (= fresh chat tab), then closes the page when done.
  - `gpt_search` — sends a query, returns clean markdown. Accepts `query` or `prompt_file`, optional `output_file` (save to disk and return a short summary instead of the full text), and `output_json` (best-effort parse/repair of the response into valid JSON via the raw text path). Strips inline citation markers (`[1]`, `[2]`, etc.) in the markdown path.
  - `gpt_search_batch` — text equivalent of `gpt_image_gen_batch`: fans out request dicts concurrently; optional per-item `label` names its heading in the combined response.
  - `provider_prompt_batch` — provider-aware raw-output batch path used by Inloopd's local planner/script runner. Writes exact responses to caller-supplied absolute paths so the caller can retain its own JSON parser and schema validation.
  - `gpt_image_gen` — sends one image-gen prompt, downloads all returned images via the authenticated browser session, saves to `<cwd>/generated/` (or `save_dir`), and (by default) embeds the image bytes in the response so Claude can analyze them.
  - `gpt_image_gen_batch` — fans out a list of prompt requests concurrently inside a single MCP call. Used when Claude wants real parallel image gen — issuing multiple `gpt_image_gen` calls in one assistant message gets serialized by the Claude Code MCP harness, while a single batch call internally uses `asyncio.gather` to run N sessions in N tabs at once. Failures isolate per item via `return_exceptions=True`. Save logic shared with `gpt_image_gen` via `_save_and_pack`.
- `browser.py` — provider-aware Playwright automation. Two classes:
  - `ChatGPTBrowser` — owns the persistent Chromium context. Long-lived across MCP tool calls. `new_session()` opens a new page, navigates to a fresh chat, and returns a `ChatGPTSession`. `is_alive` reflects whether the context is still usable (closes-event-driven).
  - `ChatGPTSession` — owns a single page. Closed after each tool call. Methods: `stream_message` (text streaming flow with DOM cleanup → markdown), `stream_image_message` (image-gen flow). Both poll for completion in their own way.
- `gpt_search.py` — standalone CLI wrapper (text only). Uses the new browser→session API.
- `login.py` — first-run ChatGPT login helper. Claude login is handled by the long-lived service, which reveals its normally hidden browser when manual authentication is required; the persistent profile keeps both sessions.

## Rules

Mechanics and evidence for each live in [README.md](README.md) *Implementation notes*.

- **Never add a stdio registration while the HTTP service is running.** Two server
  processes fight over the one persistent Chromium profile. HTTP (`127.0.0.1:8788/mcp`) is
  the only transport that lets multiple sessions do image gen at once.
- **The repo must live outside `~/Desktop`, `~/Documents`, `~/Downloads`.** TCC kills a
  launchd job from those folders with exit 78 and empty logs. Bit for real 2026-07-07.
- **Failures raise.** Send/stream timeouts, missing DOM, ChatGPT's own "Something went
  wrong" text, and undismissable rate-limit modals surface as tool errors, never as text
  saved to `output_file`. Overall deadline 480 s.
- **Keep the empty-composer check in `_submit_prompt`** — it is the only thing that
  distinguishes a sent prompt from one stranded in the box when `Enter` inserts a newline.
  Click the real send button first; selectors will need updating, the check must not go.
- **Error detectors match the *whole* normalized message, never `startswith`.** A real answer
  can open by discussing the error. Assume any new matcher is guilty until tested against an
  answer that discusses the error.
- **Background tabs go through `Target.createTarget` with `background: true`**, not
  `new_page()`, which foregrounds the hidden window. Image gen needs the window visible (not
  frontmost); `reveal_for_rendering()` / `hide_after_rendering()` are reference-counted.
- **Image completion = stop button gone AND URL count stable across two polls**, with both
  `Stop generating` and `Stop answering` selectors; zero URLs is not completion by itself.
- **Three concurrent ChatGPT tabs max** (`MAX_CONCURRENT_CHATGPT_TABS`); excess queues.
- **Pass an absolute `save_dir`** from HTTP clients; the default is the service's cwd.
- **Tests: `python3 -m pytest tests/ -q`.** Live browser flows still need manual checks.

## Live service wiring (this machine)

The server runs as the launchd HTTP service `com.kchafloque.gpt-tools` — HTTP on
`127.0.0.1:8788/mcp`, headed but hidden (see the background-browser note above).

- **Live plist: `~/Library/LaunchAgents/com.kchafloque.gpt-tools.plist`.** Edit it there —
  `launchd.plist.template` in-repo is the template, not the live file — then reload:
  `launchctl bootout gui/$UID/com.kchafloque.gpt-tools && launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.kchafloque.gpt-tools.plist`
- Clients connect over HTTP: `~/.claude.json` registers `gpt-tools` as
  `{"type": "http", "url": "http://127.0.0.1:8788/mcp"}`; both Codex homes
  (`~/.codex/config.toml`, `~/.codex-app-alt/config.toml` under `[mcp_servers.gpt-tools]`)
  point at the same URL.
- Inloopd's `backend/scripts/run_fake_researcher_chatgpt.py` is also an HTTP MCP client of this service. It must never launch or share a second Playwright profile; the separate branded Google Chrome CDP process used by the scraper remains intentionally independent.
- **Never add a stdio registration while the service is running** — two server processes
  fight over the persistent Chromium profile (see Transport modes).
- Health check: `launchctl list | grep gpt-tools` — a PID in column 1 means running, `-`
  means dead; column 2 is the *last* exit status (historical, e.g. `-15` after a restart —
  it says nothing about current health). A running service can still fail on ChatGPT rate
  limits or send timeouts; the tell is screenshots in `debug/`.
- This repo also owns the agent-facing usage skill `skills/gpt-tools/SKILL.md` (query
  length calibration, batch-vs-shared-context rules), symlinked into
  `~/.claude/skills/gpt-tools`, `~/.codex/skills/gpt-tools`, and
  `~/.codex-app-alt/skills/gpt-tools` — edit here, all three follow.

Standing the server up elsewhere (only for a machine that won't run the shared service):
clone outside Desktop/Documents/Downloads (TCC; see Key details), `pip install -r
requirements.txt`, `playwright install chromium`, `python login.py`, then install the
launchd service from the template — or, stdio fallback, register
`{"command": "python", "args": [".../mcp_server.py"]}` in the client instead.
