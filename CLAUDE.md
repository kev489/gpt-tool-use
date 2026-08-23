# GPT Tools MCP Server

MCP server that drives ChatGPT via Playwright browser automation. Exposes `gpt_search` / `gpt_search_batch` (text research) and `gpt_image_gen` / `gpt_image_gen_batch` (image generation with disk-save). Supports concurrent calls — multiple invocations from Claude run in parallel tabs sharing one browser context.

## Architecture

- `mcp_server.py` — MCP server entry point. Server name is `gpt-tools`. Holds a single shared `ChatGPTBrowser` in module state, lazily initialized via `_get_browser()` (lock-protected so concurrent first-callers don't double-init). Each tool call calls `bot.new_session()` to get a fresh page (= fresh chat tab), then closes the page when done.
  - `gpt_search` — sends a query, returns clean markdown. Accepts `query` or `prompt_file`, optional `output_file` (save to disk and return a short summary instead of the full text), and `output_json` (best-effort parse/repair of the response into valid JSON via the raw text path). Strips inline citation markers (`[1]`, `[2]`, etc.) in the markdown path.
  - `gpt_search_batch` — text equivalent of `gpt_image_gen_batch`: fans out request dicts concurrently; optional per-item `label` names its heading in the combined response.
  - `gpt_image_gen` — sends one image-gen prompt, downloads all returned images via the authenticated browser session, saves to `<cwd>/generated/` (or `save_dir`), and (by default) embeds the image bytes in the response so Claude can analyze them.
  - `gpt_image_gen_batch` — fans out a list of prompt requests concurrently inside a single MCP call. Used when Claude wants real parallel image gen — issuing multiple `gpt_image_gen` calls in one assistant message gets serialized by the Claude Code MCP harness, while a single batch call internally uses `asyncio.gather` to run N sessions in N tabs at once. Failures isolate per item via `return_exceptions=True`. Save logic shared with `gpt_image_gen` via `_save_and_pack`.
- `browser.py` — Playwright-based ChatGPT automation. Two classes:
  - `ChatGPTBrowser` — owns the persistent Chromium context. Long-lived across MCP tool calls. `new_session()` opens a new page, navigates to a fresh chat, and returns a `ChatGPTSession`. `is_alive` reflects whether the context is still usable (closes-event-driven).
  - `ChatGPTSession` — owns a single page. Closed after each tool call. Methods: `stream_message` (text streaming flow with DOM cleanup → markdown), `stream_image_message` (image-gen flow). Both poll for completion in their own way.
- `gpt_search.py` — standalone CLI wrapper (text only). Uses the new browser→session API.
- `login.py` — first-run helper that opens a headed browser so you can sign into ChatGPT once; the persistent profile keeps the session.

## Key details

- `USER_DATA_DIR` in `browser.py` stores the persistent Chromium profile (ChatGPT login session). Gitignored. Both it and `DEBUG_DIR` default to the source dir; the `GPT_TOOLS_HOME` env var relocates them (set it when pip-installed so state doesn't land in site-packages — and set it for both `gpt-tools-login` and the server).
- All paths resolve via `__file__` so the server works regardless of working directory.
- `gpt_image_gen` saves files relative to the *Claude Code* cwd, not the MCP server's source directory — that's the intended behavior so images land in the project the user is working on.
- Image download uses `page.request.get()` (not raw httpx) so OpenAI-signed image URLs carry the browser session's cookies.
- **Image-gen completion heuristic.** Stop button gone AND the URL count is stable across two polls. The image search runs against `<main>` (excludes sidebar/library) and requires both an OpenAI image-host URL pattern (`backend-api/estuary/content`, `oaiusercontent.com`, etc.) AND `alt` containing "generated" — without the `alt` filter we picked up stray library/sidebar thumbnails.
- **Parallelism.** Multiple concurrent tool calls each get their own `Page` from the shared `BrowserContext`. They share login (one ChatGPT account) but are independent conversations (each navigates to `?model=auto` which starts a new chat). The server caps itself at 3 concurrent tabs (`MAX_CONCURRENT_CHATGPT_TABS` in `mcp_server.py`, one lazily created module-level semaphore shared by all tools); excess requests queue. Account-level rate limits may still apply under sustained load.
- **Failures raise.** `stream_message` raises on send-timeout / stream-timeout / missing-DOM instead of yielding `Error: ...` strings, so tool calls fail loudly rather than saving error text to `output_file` as if it were the answer. Both text and image streaming have an overall deadline (480s default).
- **Enter alone does not reliably submit the composer.** chatgpt.com has shipped composer states where `Enter` inserts a newline instead of sending, stranding the prompt in the box. Nothing verified the send, so it surfaced 30s later as `Timeout waiting for assistant message to appear` — `debug/debug_send.png` showing text still in the composer with the submit button enabled is the tell, and it looks nothing like a login/Cloudflare failure. Bit for real 2026-08-22. `_submit_prompt` clicks the real send button (`SEND_BUTTON_SELECTORS`: `[data-testid="send-button"]`, `#composer-submit-button`, then `aria-label` and `type=submit` fallbacks), falls back to `Enter`, and in both paths confirms the composer emptied before returning. That empty-composer check is the only thing distinguishing a sent prompt from a stranded one — keep it when the selectors inevitably need updating. Failed submits screenshot to `debug/debug_submit_failed.png`; 3 attempts total.
- **Prompt-input hydration race.** Before React hydrates, chatgpt.com renders a plain fallback `<textarea placeholder="Ask anything">` that gets swapped for the real `#prompt-textarea` contenteditable — a locator found pre-swap goes stale and `fill` hangs on a now-hidden element. `_send_prompt` retries up to 3 times with short per-action timeouts, re-finding the locator each attempt.
- **ChatGPT's own error text must never be returned as the answer — and the detector must match the WHOLE message.** ChatGPT intermittently replies with "Something went wrong. If this issue persists…" as the assistant message; that string used to pass straight back, so a caller with `output_file` would write OpenAI's error to disk as research, violating "Failures raise" above. `_detect_chatgpt_error` now raises `ChatGPTResponseError` and `_run_search_in_session` retries once in a **fresh session** (the errored tab usually stays broken; a new tab is a new conversation), raising only if it fails twice. The detector requires the normalized message to *equal* a known error string, tolerating trailing punctuation and a Retry/Regenerate affordance picked up by `inner_text`. `startswith` was tried and is wrong: it hard-failed a valid answer opening with "There was an error generating a response. Users typically see it when…" — strictly worse than the bug it fixed. A real error has nothing after it; a real answer keeps going. Err toward passing text through, and assume any new matcher here is guilty until tested against an answer that *discusses* the error — same false-positive class as the rate-limit detector below. Added 2026-08-22.
- **Rate-limit modal handling.** ChatGPT's "Too many requests" modal is detected by phrase-matching, screenshotted to `debug/`, and dismissed via its "Got it" button; `ChatGPTRateLimitError` is raised only if dismissal fails or no usable response follows. The detector skips elements inside or containing chat messages and the composer (`[data-message-author-role]`, `#prompt-textarea`) — responses can legitimately *discuss* rate limits (e.g. HTTP 429 research), and matching them would kill valid calls.
- **Tests.** `tests/test_mcp_server.py` covers the JSON-normalization helpers (`_clean_json_result` and friends). Run `python3 -m pytest tests/ -q`. The browser flows have no automated coverage — verify those manually.
- **Browser lifecycle.** First call launches Chromium and pays the startup cost. Subsequent calls reuse the same browser. If the user closes the browser window manually, `is_alive` flips false and the next call re-launches.
- Browser launches headed (`headless=False`) by default so ChatGPT login can be completed on first run. Pass `--headless` to `mcp_server.py` to run headless. **Don't use `--headless` for the launchd service** — Cloudflare's bot detection on chatgpt.com flags headless Chromium and serves a "Verify you are human" interstitial, which means `#prompt-textarea` never renders and every tool call fails with `Failed to find chat input on new session`. Symptom shows up in `debug/debug_session_init.png`. Run the launchd service headed even though it parks a Chromium window on the desktop.
- **Install location: never under `~/Desktop`, `~/Documents`, or `~/Downloads`.** macOS TCC blocks launchd background jobs from those folders — launchd can't chdir/open logs there, so the service dies at spawn with `EX_CONFIG` (78) and *empty logs* (Python never starts). Bit for real on 2026-07-07 after an OS update reset TCC grants; the repo moved from `~/Desktop` to `~/Customization/gpt_tool_use` as the fix. `launchctl print gui/$UID/com.kchafloque.gpt-tools | grep 'last exit'` is the tell.
- **Transport modes.** `mcp_server.py` accepts `--transport stdio` (default, one server per Claude Code session) or `--transport http` (long-lived server, multiple Claude Code sessions share it as clients). HTTP mode is the only way to run image gen concurrently across multiple Claude Code sessions, because the persistent Chromium profile only allows one accessing process at a time. With stdio, two sessions = two server processes = profile lock conflict. With HTTP, one server process owns the profile; all sessions go through it. Defaults: host `127.0.0.1`, port `8788`, path `/mcp` (FastMCP's `streamable_http_path`). See `launchd.plist.template` for auto-start.
- **Settings via `mcp.settings`.** FastMCP host/port aren't `run()` args; they're set on `mcp.settings` before calling `run(transport="streamable-http")`.

## Setup on a new machine

Clone somewhere launchd can reach — **not** Desktop/Documents/Downloads (TCC; see Key details).

```
git clone <repo>
cd gpt-tool-use
pip install -r requirements.txt
playwright install chromium
python login.py      # first run: log into ChatGPT in the browser window
```

Then add to Claude Code MCP settings (`~/.claude.json` under `mcpServers`, or via `claude mcp add`):
```json
{
  "mcpServers": {
    "gpt-tools": {
      "command": "python",
      "args": ["/path/to/gpt_tool_use/mcp_server.py"]
    }
  }
}
```
