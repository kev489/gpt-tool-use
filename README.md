# GPT Tools MCP Server

MCP tools and a local batch interface that drive ChatGPT and Claude through one shared Playwright browser. The tools:

- **`gpt_search`** — routes research queries through ChatGPT and returns clean markdown, or saves it directly to disk to keep the MCP client context light.
- **`gpt_image_gen`** — sends an image-gen prompt, saves the generated images to disk, and returns them so the calling agent can analyze the result.
- **`gpt_search_batch`** / **`gpt_image_gen_batch`** — run several prompts concurrently inside one MCP call, each in its own ChatGPT tab.
- **`provider_prompt_batch`** — writes raw ChatGPT or Claude responses for local structured-output pipelines such as Inloopd.

## Why

Codex and Claude Code can use MCP tools. This lets either client delegate web research and image generation to ChatGPT, using your existing ChatGPT subscription instead of a separate API key.

### Token efficiency (search)

When the calling agent does web research natively, it spends context and tokens on search results, page fetches, and reasoning about what it found. With this tool, ChatGPT handles that work and the caller gets back a clean result.

### Design loop (image gen)

Image gen lets Codex or Claude iterate on visual designs without you having to shuttle screenshots manually. Generated images are embedded in the tool response so the caller can critique, suggest changes, and re-prompt.

The tradeoff with both is speed. Browser automation is slower than a direct API call. If you're multitasking, it doesn't matter — Claude kicks off the call and you come back to a finished result.

## Setup

```bash
pip install git+https://github.com/kev489/gpt-tool-use.git
playwright install chromium
```

Or clone and install locally:

```bash
git clone https://github.com/kev489/gpt-tool-use.git
cd gpt-tool-use
pip install .
playwright install chromium
```

If you plan to run the launchd service, keep the clone outside `~/Desktop`, `~/Documents`, and `~/Downloads` — see the TCC warning under "One-time setup".

Run once to log into ChatGPT (opens a browser window — sign in, then close the window):

```bash
gpt-tools-login    # or: python login.py from a clone
```

Login state for both ChatGPT and Claude is stored in `chatgpt_profile/` next to the installed code (gitignored in a clone). The helper above signs into ChatGPT; the running service reveals its normally hidden browser when Claude needs its one-time sign-in. With the pip install, set `GPT_TOOLS_HOME` to a writable directory (e.g. `~/.gpt-tools`) so the profile and debug screenshots don't land inside site-packages — set it both for the login command and in the MCP server's environment. You only need to log into each provider once per machine unless its session expires.

Then add to your Claude Code MCP config (`~/.claude.json` under `mcpServers`, or via `claude mcp add`):

```json
{
  "mcpServers": {
    "gpt-tools": {
      "command": "gpt-tools",
      "args": []
    }
  }
}
```

This is the **stdio transport**: Claude Code spawns one MCP server subprocess per session. Simple, but if you run multiple sessions at once, each spawns its own server, and they fight over the shared provider-profile lock — only one session can use image gen at a time. See "Running as a long-lived service" below for the multi-session setup.

## Running as a long-lived service

If you have multiple Codex or Claude Code sessions open and want them all to use `gpt_image_gen` simultaneously, switch from stdio (one server per session) to HTTP (one shared server, all sessions are clients). The browser lives in the server, so there's only ever one Chromium accessing the profile.

### One-time setup

**1. Edit the launchd plist template.** `launchd.plist.template` ships with placeholder paths and a generic Label — replace each one with values for your machine:

- `Label` — `com.example.gpt-tools` → `com.YOURNAME.gpt-tools` (any reverse-DNS string; this is also the filename you'll use in step 2)
- `ProgramArguments[0]` — `/PATH/TO/python3` → your `python3` absolute path (find with `which python3`). If you upgrade Python later, update this path and reload the plist (`launchctl unload` + `launchctl load`); otherwise the service silently fails to start at next login (visible only as a non-zero exit code in `launchctl list | grep gpt-tools`).
- `ProgramArguments[1]` — `/PATH/TO/gpt_tool_use/mcp_server.py` → full path to `mcp_server.py`
- `WorkingDirectory`, `StandardOutPath`, `StandardErrorPath` — replace `/PATH/TO/gpt_tool_use` with the absolute path to this repo

**TCC warning:** the repo must live *outside* `~/Desktop`, `~/Documents`, and `~/Downloads`. macOS blocks launchd background jobs from those folders, so the service dies at spawn with exit code 78 (`EX_CONFIG`) and empty logs — Python never starts. If `launchctl list | grep gpt-tools` shows 78, it's this or a stale Python path.

**2. Install the plist.** Use whatever you set for `Label` as the filename:

```bash
cp launchd.plist.template ~/Library/LaunchAgents/com.YOURNAME.gpt-tools.plist
launchctl load ~/Library/LaunchAgents/com.YOURNAME.gpt-tools.plist
```

The server will now start at login and stay running. Logs go to `debug/launchd-stdout.log` and `debug/launchd-stderr.log` inside the repo.

**3. Switch your MCP config to HTTP.**

Codex (`~/.codex/config.toml`, and any alternate `CODEX_HOME` such as `~/.codex-app-alt/config.toml`):

```toml
[mcp_servers.gpt-tools]
url = "http://127.0.0.1:8788/mcp"
tool_timeout_sec = 1800
startup_timeout_sec = 120
```

Claude Code:

```json
{
  "mcpServers": {
    "gpt-tools": {
      "type": "http",
      "url": "http://127.0.0.1:8788/mcp"
    }
  }
}
```

Restart the MCP client. All sessions now share the long-lived server. Multiple sessions can call `gpt_image_gen` / `gpt_image_gen_batch` at the same time.

### Manual run (no launchd)

```bash
python mcp_server.py --transport http --port 8788
```

Useful for testing the HTTP path before installing as a service. Avoid `--headless` for ChatGPT automation unless you have verified the account is not being stopped by browser verification.

### Reverting to stdio

Unload the launchd agent (`launchctl unload ~/Library/LaunchAgents/com.YOURNAME.gpt-tools.plist`) and put back the original stdio config. No code changes needed — the server supports both transports.

## How `gpt_search` works

1. Your query is sent directly to ChatGPT as a prompt (no system prompt — you control the output). You can pass the prompt directly as `query`, or provide `prompt_file` to read the prompt from a text file.
2. Playwright waits for the response to finish streaming (up to 8 minutes)
3. JavaScript DOM evaluation strips citation buttons, SVGs, accordion dropdowns, and other UI artifacts
4. The cleaned HTML is converted to markdown via `markdownify`
5. Inline citation markers (`[1]`, `[2]`, etc.) are stripped before returning to the MCP client or writing to `output_file`

With `output_json=true`, the tool treats JSON cleanup as a best-effort post-processing step. It does not change the prompt, and it reads the response through the raw text path instead of the markdown conversion path so JSON string escapes survive. If `output_file` is provided, the raw ChatGPT output is saved first; then the tool tries to extract, repair, parse, and format valid JSON. On success it overwrites the file with normalized JSON. On failure it leaves the raw output in place and reports that JSON post-processing did not succeed.

Parameters:
- `query` (optional) — the full prompt to send to ChatGPT
- `prompt_file` (optional) — path to a text file containing the prompt. Use either `query` or `prompt_file`, not both.
- `output_file` (optional) — path where the cleaned markdown response should be saved. Parent directories are created automatically.
- `return_output` (optional) — when `False`, the caller only gets a short saved-path summary. Defaults to `True` unless `output_file` is provided; with `output_file`, it defaults to `False` to avoid filling the caller's context with the full response.
- `output_json` (optional) — when `True`, best-effort normalize the response to valid JSON after ChatGPT returns; if parsing/repair fails, leave the raw output unchanged.

Relative file paths resolve from the MCP server process working directory. Use absolute paths if the server is running as a long-lived HTTP/launchd service.

Example:

```json
{
  "prompt_file": "prompts/research.txt",
  "output_file": "research/chatgpt-answer.md"
}
```

JSON output example:

```json
{
  "query": "List three current low-cost index funds as an array of objects with ticker, fund_name, and expense_ratio.",
  "output_file": "research/funds.json",
  "output_json": true
}
```

For multiple text prompts, use **`gpt_search_batch`**. Each request opens its own ChatGPT tab and runs concurrently inside one MCP call. This is the preferred way to batch text research because some MCP clients serialize multiple separate tool calls to the same server. The server runs at most 3 ChatGPT tabs at a time across all calls and sessions; larger batches queue internally. Each request also accepts an optional `label`, used as its heading in the combined response.

```json
{
  "requests": [
    {
      "prompt_file": "/absolute/path/prompts/topic-1.txt",
      "output_file": "/absolute/path/outputs/topic-1.md"
    },
    {
      "prompt_file": "/absolute/path/prompts/topic-2.txt",
      "output_file": "/absolute/path/outputs/topic-2.json",
      "output_json": true
    }
  ]
}
```

## How `gpt_image_gen` works

1. Your prompt is sent to a fresh ChatGPT chat (each call resets — no context contamination across iterations)
2. Playwright waits up to 8 minutes for the stop button to disappear AND `<img>` elements to settle
3. Image URLs are downloaded via the browser's authenticated session (signed URLs work)
4. Files are saved to `<cwd>/generated/<prefix>.png` (or `<prefix>-1.png`, `<prefix>-2.png`, ... for multiple)
5. The tool returns a text summary with paths, plus (by default) the image bytes inline so Claude can see them

**Concurrent calls** are supported. All tools share a single Chromium context held in module state; each individual request gets its own page (= its own fresh ChatGPT tab/chat). The server caps itself at 3 concurrent ChatGPT tabs across all calls and sessions; anything beyond that queues internally.

To run multiple image-gen prompts in parallel, use **`gpt_image_gen_batch`**. Some MCP clients serialize several separate `gpt_image_gen` calls, but a batch call fans out internally with `asyncio.gather`, running up to 3 tabs at a time and queuing the rest. Pass `requests=[{prompt, filename_prefix?, save_dir?}, ...]`. Account-level rate limits may apply at high concurrency.

If ChatGPT or Claude shows a "Too many requests"/usage-limit modal, the browser automation saves a debug screenshot under `debug/`, clicks "Got it" (with `OK`/`Dismiss` fallbacks), and continues waiting for the response. It raises `ChatGPTRateLimitError` only if the dialog cannot be dismissed, persists after dismissal, or no usable response arrives afterward. Batch tools isolate the affected item.

Parameters:
- `prompt` (required) — the full image-gen prompt
- `filename_prefix` (optional) — descriptive stem for saved files. Falls back to a hash if omitted.
- `save_dir` (optional) — overrides the default `<cwd>/generated/`
- `embed_images` (optional, default `True`) — when `False`, the caller only gets the paths back. Use this during long iteration loops where embedded image bytes would flood the caller's context.

## Files

| File | Purpose |
|------|---------|
| `mcp_server.py` | MCP server entry point (stdio or HTTP transport); registers the tools |
| `browser.py` | Shared ChatGPT/Claude Playwright automation; text and ChatGPT image-gen flows |
| `gpt_search.py` | Standalone CLI wrapper (text only) |
| `login.py` | First-run helper for ChatGPT login |
| `tests/test_mcp_server.py` | Unit tests for the JSON-normalization and prompt helpers |
| `tests/test_browser.py` | Unit tests for provider routing, rate-limit detection, and browser helpers |

## Notes

- The `chatgpt_profile/` directory stores both provider sessions. It's gitignored — you need to log into ChatGPT and Claude once per machine.
- On macOS the browser launches minimized, runs headed for provider compatibility, and stays hidden for text/provider calls. Tabs are created through Chromium's background-target API because Playwright's ordinary `new_page()` briefly foregrounds a hidden Chrome window. Manual login reveals and unminimizes it. Image generation temporarily makes it visible without making it frontmost—fully hiding the process suspends ChatGPT's image renderer—then hides it again when the final concurrent image call finishes.
- Without `save_dir`, `gpt_image_gen` saves under `<server-process-cwd>/generated/`. For the long-lived HTTP service, pass an absolute `save_dir` when files must land in a particular client project.
- Failures raise: send timeouts, stream timeouts, and rate-limit dead ends surface as tool-call errors (with a debug screenshot path under `debug/`) instead of error text masquerading as a response.

## Development

```bash
pip install -e ".[dev]"
python3 -m pytest tests/ -q
```

The tests cover pure helpers plus provider routing and rate-limit detection. Live browser UI flows still require verification against real ChatGPT and Claude.

## Implementation notes (for maintainers)

The non-obvious mechanics and the failure modes that were hit for real. Rules and the live
service wiring for the reference machine are in [AGENTS.md](AGENTS.md).

- `USER_DATA_DIR` in `browser.py` stores the persistent Chromium profile (ChatGPT and Claude login sessions). Gitignored. Both it and `DEBUG_DIR` default to the source dir; the `GPT_TOOLS_HOME` env var relocates them (set it when pip-installed so state doesn't land in site-packages — and set it for both `gpt-tools-login` and the server).
- All paths resolve via `__file__` so the server works regardless of working directory.
- `gpt_image_gen` defaults to `<server-process-cwd>/generated/`. In HTTP mode that is the long-lived service's working directory, so callers must pass an absolute `save_dir` when output belongs in a specific client project.
- Image download uses `page.request.get()` (not raw httpx) so OpenAI-signed image URLs carry the browser session's cookies.
- **Image-gen completion heuristic.** Stop button gone AND the URL count is stable across two polls. The image search runs against `<main>` (excludes sidebar/library) and requires both an OpenAI image-host URL pattern (`backend-api/estuary/content`, `oaiusercontent.com`, etc.) AND `alt` containing "generated" — without the `alt` filter we picked up stray library/sidebar thumbnails.
- **Image-gen response startup can be slow.** Allow up to 120 seconds for the first assistant turn to appear before declaring a send timeout. Image turns may use classic `[data-message-author-role="assistant"]` markup or the current `[data-turn="assistant"]` wrapper, so `_chatgpt_assistant_turn_count()` supports both; the exact `ChatGPT said:` heading remains a fallback. Debug screenshots use their own 5-second timeout so a wedged renderer cannot mask the underlying image-generation error.
- **Current image-gen streaming control.** ChatGPT labels the active-generation button `Stop answering`, not only `Stop generating`. Keep both selectors; missing `Stop answering` makes the zero-image stability heuristic close the tab after four seconds and leaves the chat at `Stopped thinking`.
- **Zero image URLs is not completion by itself.** After `Stop answering` disappears, the generated `<img>` can still take several seconds to mount and load. With zero matching URLs, keep waiting on blank/`Thinking`/`Generating image` turns; finish without an image only when `_image_text_is_terminal_without_urls()` sees a real terminal text response such as a refusal.
- **Image rendering needs a visible macOS window.** Hiding the entire Chrome-for-Testing process suspends ChatGPT's image workflow even though text calls continue to work. `_run_image_in_session()` reference-counts `reveal_for_rendering()` / `hide_after_rendering()`: image calls make the service browser visible without making it frontmost, so it stays behind the user's active app, then hide it after the final concurrent image call finishes.
- **Parallelism.** Multiple concurrent tool calls each get their own `Page` from the shared `BrowserContext`. They share login (one ChatGPT account) but are independent conversations (each navigates to `?model=auto` which starts a new chat). The server caps itself at 3 concurrent tabs (`MAX_CONCURRENT_CHATGPT_TABS` in `mcp_server.py`, one lazily created module-level semaphore shared by all tools); excess requests queue. Account-level rate limits may still apply under sustained load.
- **Failures raise.** `stream_message` raises on send-timeout / stream-timeout / missing-DOM instead of yielding `Error: ...` strings, so tool calls fail loudly rather than saving error text to `output_file` as if it were the answer. Both text and image streaming have an overall deadline (480s default).
- **Enter alone does not reliably submit the composer.** chatgpt.com has shipped composer states where `Enter` inserts a newline instead of sending, stranding the prompt in the box. Nothing verified the send, so it surfaced 30s later as `Timeout waiting for assistant message to appear` — `debug/debug_send.png` showing text still in the composer with the submit button enabled is the tell, and it looks nothing like a login/Cloudflare failure. Bit for real 2026-08-22. `_submit_prompt` clicks the real send button (`SEND_BUTTON_SELECTORS`: `[data-testid="send-button"]`, `#composer-submit-button`, then `aria-label` and `type=submit` fallbacks), falls back to `Enter`, and in both paths confirms the composer emptied before returning. That empty-composer check is the only thing distinguishing a sent prompt from a stranded one — keep it when the selectors inevitably need updating. Failed submits screenshot to `debug/debug_submit_failed.png`; 3 attempts total.
- **Prompt-input hydration race.** Before React hydrates, chatgpt.com renders a plain fallback `<textarea placeholder="Ask anything">` that gets swapped for the real `#prompt-textarea` contenteditable — a locator found pre-swap goes stale and `fill` hangs on a now-hidden element. `_send_prompt` retries up to 3 times with short per-action timeouts, re-finding the locator each attempt.
- **ChatGPT's own error text must never be returned as the answer — and the detector must match the WHOLE message.** ChatGPT intermittently replies with "Something went wrong. If this issue persists…" as the assistant message; that string used to pass straight back, so a caller with `output_file` would write OpenAI's error to disk as research, violating "Failures raise" above. `_detect_chatgpt_error` now raises `ChatGPTResponseError` and `_run_search_in_session` retries once in a **fresh session** (the errored tab usually stays broken; a new tab is a new conversation), raising only if it fails twice. The detector requires the normalized message to *equal* a known error string, tolerating trailing punctuation and a Retry/Regenerate affordance picked up by `inner_text`. `startswith` was tried and is wrong: it hard-failed a valid answer opening with "There was an error generating a response. Users typically see it when…" — strictly worse than the bug it fixed. A real error has nothing after it; a real answer keeps going. Err toward passing text through, and assume any new matcher here is guilty until tested against an answer that *discusses* the error — same false-positive class as the rate-limit detector below. Added 2026-08-22.
- **Rate-limit modal handling.** ChatGPT and Claude rate-limit modals (including "Too many requests" and provider limit variants) are detected by phrase-matching, screenshotted to `debug/`, and dismissed via "Got it"/"OK"/"Dismiss". `ChatGPTRateLimitError` is raised only if dismissal fails or no usable response follows. The detector skips both providers' assistant messages and composers so a response discussing HTTP 429s is not mistaken for a modal, and it requires the candidate element to be visibly rendered because dismissed dialogs can remain in the DOM with matching `innerText`.
- **Tests.** `tests/test_mcp_server.py` covers JSON normalization and prompt helpers; `tests/test_browser.py` covers provider routing, rate-limit detection, and browser helpers. Run `python3 -m pytest tests/ -q`. Live browser UI flows still require manual verification against both providers.
- **Browser lifecycle.** First call launches Chromium and pays the startup cost. Subsequent calls reuse the same browser. If the user closes the browser window manually, `is_alive` flips false and the next call re-launches.
- Browser launches headed (`headless=False`) because ChatGPT blocks headless automation, with `--start-minimized`, then immediately hides its exact macOS process. On macOS, normal Playwright `context.new_page()` calls briefly unhide and foreground the Chrome window even when the process was already hidden; `new_session()` therefore creates tabs through Chromium's `Target.createTarget` protocol with `background: true`. Do not replace that path with `new_page()` for background service calls. Revealing the process also unminimizes its window for image rendering or manual provider login. If a provider login expires, the service reveals the browser for manual authentication and hides it again once the composer appears. Pass `--headless` only for explicit diagnostics; Cloudflare can serve a "Verify you are human" interstitial in that mode.
- **Install location: never under `~/Desktop`, `~/Documents`, or `~/Downloads`.** macOS TCC blocks launchd background jobs from those folders — launchd can't chdir/open logs there, so the service dies at spawn with `EX_CONFIG` (78) and *empty logs* (Python never starts). Bit for real on 2026-07-07 after an OS update reset TCC grants; the repo moved from `~/Desktop` to `~/Customization/gpt_tool_use` as the fix. `launchctl print gui/$UID/com.kchafloque.gpt-tools | grep 'last exit'` is the tell.
- **Transport modes.** `mcp_server.py` accepts `--transport stdio` (default, one server per MCP client session) or `--transport http` (long-lived server shared by Codex, Claude Code, and local pipeline clients). HTTP mode is the only way to run image gen concurrently across multiple sessions, because the persistent Chromium profile only allows one accessing process at a time. With stdio, two sessions = two server processes = profile lock conflict. With HTTP, one server process owns the profile; all sessions go through it. Defaults: host `127.0.0.1`, port `8788`, path `/mcp` (FastMCP's `streamable_http_path`). See `launchd.plist.template` for auto-start.
- **Settings via `mcp.settings`.** FastMCP host/port aren't `run()` args; they're set on `mcp.settings` before calling `run(transport="streamable-http")`.
