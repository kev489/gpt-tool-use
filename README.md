# GPT Tools MCP Server

MCP tools for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that drive ChatGPT via Playwright browser automation. Two tools so far:

- **`gpt_search`** — routes research queries through ChatGPT and returns clean markdown.
- **`gpt_image_gen`** — sends an image-gen prompt, saves the generated images to disk, and returns them so Claude can analyze the result.

## Why

Claude Code can use MCP tools. This lets it delegate web research and image generation to ChatGPT, using your existing ChatGPT subscription instead of a separate API key.

### Token efficiency (search)

When Claude does web research natively, it burns tokens on search results, page fetches, and reasoning about what it found. With this tool, ChatGPT does all the thinking — it searches, reads sources, reasons through the answer, and uses its own thinking tokens. Claude just gets back a clean result.

### Design loop (image gen)

Image gen lets Claude iterate on visual designs without you having to shuttle screenshots manually. Generated images land in `./generated/` and are embedded in the tool response so Claude can critique, suggest changes, and re-prompt.

The tradeoff with both is speed. Browser automation is slower than a direct API call. If you're multitasking, it doesn't matter — Claude kicks off the call and you come back to a finished result.

## Setup

```bash
pip install git+https://github.com/kevin-chafloque/gpt-tool-use.git
playwright install chromium
```

Or clone and install locally:

```bash
git clone https://github.com/kevin-chafloque/gpt-tool-use.git
cd gpt-tool-use
pip install .
playwright install chromium
```

Run once to log into ChatGPT (opens a browser window — sign in, then close the window):

```bash
python login.py
```

Login state is stored in `chatgpt_profile/` (gitignored). You only need to do this once per machine.

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

This is the **stdio transport**: Claude Code spawns one MCP server subprocess per session. Simple, but if you run multiple Claude Code sessions at once, each spawns its own server, and they fight over the persistent ChatGPT profile lock — only one session can use image gen at a time. See "Running as a long-lived service" below for the multi-session setup.

## Running as a long-lived service

If you have multiple Claude Code sessions open and want them all to use `gpt_image_gen` simultaneously, switch from stdio (one server per session) to HTTP (one shared server, all sessions are clients). The browser lives in the server, so there's only ever one Chromium accessing the profile.

### One-time setup

**1. Edit the launchd plist template.** `launchd.plist.template` ships with placeholder paths and a generic Label — replace each one with values for your machine:

- `Label` — `com.example.gpt-tools` → `com.YOURNAME.gpt-tools` (any reverse-DNS string; this is also the filename you'll use in step 2)
- `ProgramArguments[0]` — `/PATH/TO/python3` → your `python3` absolute path (find with `which python3`). If you upgrade Python later, update this path and reload the plist (`launchctl unload` + `launchctl load`); otherwise the service silently fails to start at next login (visible only as a non-zero exit code in `launchctl list | grep gpt-tools`).
- `ProgramArguments[1]` — `/PATH/TO/gpt_tool_use/mcp_server.py` → full path to `mcp_server.py`
- `WorkingDirectory`, `StandardOutPath`, `StandardErrorPath` — replace `/PATH/TO/gpt_tool_use` with the absolute path to this repo

**2. Install the plist.** Use whatever you set for `Label` as the filename:

```bash
cp launchd.plist.template ~/Library/LaunchAgents/com.YOURNAME.gpt-tools.plist
launchctl load ~/Library/LaunchAgents/com.YOURNAME.gpt-tools.plist
```

The server will now start at login and stay running. Logs go to `debug/launchd-stdout.log` and `debug/launchd-stderr.log` inside the repo.

**3. Switch your Claude Code MCP config to HTTP.**

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

Restart Claude Code. All sessions now share the long-lived server. Multiple sessions can call `gpt_image_gen` / `gpt_image_gen_batch` at the same time.

### Manual run (no launchd)

```bash
python mcp_server.py --transport http --port 8788 --headless
```

Useful for testing the HTTP path before installing as a service.

### Reverting to stdio

Unload the launchd agent (`launchctl unload ~/Library/LaunchAgents/com.YOURNAME.gpt-tools.plist`) and put back the original stdio config. No code changes needed — the server supports both transports.

## How `gpt_search` works

1. Your query is sent directly to ChatGPT as a prompt (no system prompt — you control the output)
2. Playwright waits for the response to finish streaming
3. JavaScript DOM evaluation strips citation buttons, SVGs, accordion dropdowns, and other UI artifacts
4. The cleaned HTML is converted to markdown via `markdownify`
5. Inline citation markers (`[1]`, `[2]`, etc.) are stripped before returning to Claude

## How `gpt_image_gen` works

1. Your prompt is sent to a fresh ChatGPT chat (each call resets — no context contamination across iterations)
2. Playwright waits up to 3 minutes for the stop button to disappear AND `<img>` elements to settle
3. Image URLs are downloaded via the browser's authenticated session (signed URLs work)
4. Files are saved to `<cwd>/generated/<prefix>.png` (or `<prefix>-1.png`, `<prefix>-2.png`, ... for multiple)
5. The tool returns a text summary with paths, plus (by default) the image bytes inline so Claude can see them

**Concurrent calls** are supported. Both tools share a single Chromium context held in module state; each call gets its own page (= its own fresh chat).

To actually run multiple image-gen prompts in parallel from Claude, use **`gpt_image_gen_batch`**. Issuing several separate `gpt_image_gen` tool calls from one assistant message gets serialized by the MCP harness — but a batch call fans out internally with `asyncio.gather`, opening N tabs and running all prompts concurrently. Pass `requests=[{prompt, filename_prefix?, save_dir?}, ...]`. Account-level rate limits may apply at high concurrency.

Parameters:
- `prompt` (required) — the full image-gen prompt
- `filename_prefix` (optional) — descriptive stem for saved files. Falls back to a hash if omitted.
- `save_dir` (optional) — overrides the default `<cwd>/generated/`
- `embed_images` (optional, default `True`) — when `False`, Claude only gets the paths back. Use this during long iteration loops where embedded image bytes would flood Claude's context.

## Files

| File | Purpose |
|------|---------|
| `mcp_server.py` | MCP server entry point (stdio transport); registers both tools |
| `browser.py` | Playwright ChatGPT automation; both text-streaming and image-gen flows |
| `gpt_search.py` | Standalone CLI wrapper (text only) |
| `login.py` | First-run helper for ChatGPT login |

## Notes

- The `chatgpt_profile/` directory stores your persistent Chromium session. It's gitignored — you need to log in on each machine.
- Browser runs headed by default so you can complete the ChatGPT login on first run, and so you can watch image gen progress when debugging.
- `gpt_image_gen` saves images relative to the Claude Code cwd, not the MCP server's source directory — so files land in whatever project Claude is working on.
