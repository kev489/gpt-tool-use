import os
import sys
import argparse
import asyncio
import base64
import hashlib
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent, ImageContent

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from browser import ChatGPTBrowser, USER_DATA_DIR

mcp = FastMCP("gpt-tools")

_browser: ChatGPTBrowser | None = None
_browser_lock: asyncio.Lock | None = None
_browser_headless: bool = False


def _get_lock() -> asyncio.Lock:
    global _browser_lock
    if _browser_lock is None:
        _browser_lock = asyncio.Lock()
    return _browser_lock


def _clear_stale_singleton_lock():
    lock = os.path.join(USER_DATA_DIR, "SingletonLock")
    if not os.path.islink(lock):
        return
    try:
        target = os.readlink(lock)
        pid = int(target.rsplit("-", 1)[-1])
    except (OSError, ValueError):
        try:
            os.unlink(lock)
        except OSError:
            pass
        return
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        try:
            os.unlink(lock)
        except OSError:
            pass


async def _get_browser() -> ChatGPTBrowser:
    global _browser
    async with _get_lock():
        if _browser is not None and _browser.is_alive:
            return _browser
        if _browser is not None:
            try:
                await _browser.close()
            except Exception:
                pass
        _clear_stale_singleton_lock()
        _browser = ChatGPTBrowser(headless=_browser_headless)
        await _browser.start()
        return _browser


def _save_and_pack(
    prompt: str,
    result: dict,
    filename_prefix: str | None,
    save_dir: str | None,
    embed_images: bool,
) -> list:
    images = result.get("images", [])
    text_response = result.get("text", "")

    if save_dir:
        save_path = Path(save_dir).expanduser().resolve()
    else:
        save_path = Path.cwd() / "generated"
    save_path.mkdir(parents=True, exist_ok=True)

    if not filename_prefix:
        h = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:8]
        filename_prefix = f"gpt-image-{h}"

    if filename_prefix.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        filename_prefix = filename_prefix.rsplit(".", 1)[0]

    saved_paths: list[str] = []
    contents: list = []
    for i, blob in enumerate(images):
        mime = blob["mime"]
        if "jpeg" in mime or "jpg" in mime:
            ext = "jpg"
        elif "webp" in mime:
            ext = "webp"
        else:
            ext = "png"

        if len(images) == 1:
            fname = f"{filename_prefix}.{ext}"
        else:
            fname = f"{filename_prefix}-{i+1}.{ext}"

        out_path = save_path / fname
        out_path.write_bytes(blob["bytes"])
        saved_paths.append(str(out_path))

        if embed_images:
            b64 = base64.b64encode(blob["bytes"]).decode("ascii")
            contents.append(ImageContent(type="image", data=b64, mimeType=mime))

    if not images:
        summary = f"[{filename_prefix}] ChatGPT did not return any images."
        if text_response:
            summary += f"\n\nText response from ChatGPT:\n{text_response}"
        return [TextContent(type="text", text=summary)]

    lines = [f"[{filename_prefix}] Saved {len(saved_paths)} image(s) to {save_path}:"]
    lines.extend(f"  - {p}" for p in saved_paths)
    if text_response:
        snippet = text_response[:500]
        lines.append(f"\nText also returned by ChatGPT: {snippet}")
    summary = "\n".join(lines)

    return [TextContent(type="text", text=summary)] + contents


@mcp.tool()
async def gpt_search(query: str) -> str:
    """Search the web or research a topic using ChatGPT. Pass your full query as the prompt — it will be sent directly to ChatGPT and the response returned."""
    bot = await _get_browser()
    session = await bot.new_session()
    try:
        result = ""
        async for chunk in session.stream_message(query):
            if chunk["type"] == "final":
                result = chunk["content"]
        import re
        result = re.sub(r'\s*\[\d+\]', '', result)
        return result
    finally:
        await session.close()


@mcp.tool()
async def gpt_image_gen(
    prompt: str,
    filename_prefix: str | None = None,
    save_dir: str | None = None,
    embed_images: bool = True,
) -> list:
    """Generate one or more images via ChatGPT image gen and save them to disk.

    The prompt is sent directly to ChatGPT — phrase it as an image-generation request and let the prompt itself specify how many images you want.

    For running multiple distinct prompts in parallel, use `gpt_image_gen_batch` instead — it fans out concurrently inside a single MCP call. Issuing two `gpt_image_gen` calls from one Claude message executes serially because the MCP harness serializes calls to the same server.

    Args:
        prompt: Full image-gen prompt sent directly to ChatGPT.
        filename_prefix: Stem for saved files. Single image saves as `<prefix>.<ext>`; multiple images get numbered suffixes (`<prefix>-1.<ext>`, `<prefix>-2.<ext>`, ...). Defaults to a hash of the prompt.
        save_dir: Where to save images. Defaults to `<cwd>/generated/` (created if missing).
        embed_images: When True, the saved images are returned in the tool response so Claude can analyze them. Set False during long iteration loops to keep context light — paths are still returned.

    Returns a list of MCP content blocks: a text summary plus, if embed_images is True, the image blobs.
    """
    bot = await _get_browser()
    session = await bot.new_session()
    try:
        result = await session.stream_image_message(prompt)
    finally:
        await session.close()

    return _save_and_pack(prompt, result, filename_prefix, save_dir, embed_images)


@mcp.tool()
async def gpt_image_gen_batch(
    requests: list[dict],
    embed_images: bool = True,
) -> list:
    """Run multiple image-gen prompts in parallel via ChatGPT image gen.

    Each request opens its own ChatGPT tab and runs concurrently with the others. This is how you actually parallelize image gen — issuing multiple separate `gpt_image_gen` tool calls from a single Claude message gets serialized by the MCP harness, but a single `gpt_image_gen_batch` call fans out internally and bypasses that.

    Each item in `requests` is a dict with keys:
      - `prompt` (required, str): the full image-gen prompt
      - `filename_prefix` (optional, str): stem for saved files; falls back to a hash of the prompt
      - `save_dir` (optional, str): override save location for this item; defaults to `<cwd>/generated/`

    `embed_images` is batch-level — applies to all items. Set False during long iteration loops to keep Claude's context light.

    All items run concurrently. If one fails, the others still complete; failed items show up in the response as `[<prefix>] FAILED: <error>`.

    Account-level rate limits may apply at high concurrency. Recommended batch size: 2–3 to start.

    Returns a list of MCP content blocks: per-item text summaries plus, if embed_images is True, the image blobs in order.
    """
    if not requests:
        return [TextContent(type="text", text="No requests provided.")]

    bot = await _get_browser()

    async def _one(req: dict):
        prompt = req["prompt"]
        filename_prefix = req.get("filename_prefix")
        save_dir = req.get("save_dir")

        session = await bot.new_session()
        try:
            result = await session.stream_image_message(prompt)
        finally:
            await session.close()

        return _save_and_pack(prompt, result, filename_prefix, save_dir, embed_images)

    packed = await asyncio.gather(
        *[_one(r) for r in requests],
        return_exceptions=True,
    )

    output: list = []
    for i, p in enumerate(packed):
        req = requests[i]
        if isinstance(p, Exception):
            label = req.get("filename_prefix") or f"request_{i+1}"
            output.append(TextContent(type="text", text=f"[{label}] FAILED: {p}"))
        else:
            output.extend(p)
    return output


def main():
    parser = argparse.ArgumentParser(description="GPT tools MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport mode: stdio (default, one server per Claude Code session) or http (long-lived server, multiple sessions share it)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host (only used with --transport http)")
    parser.add_argument("--port", type=int, default=8788, help="HTTP bind port (only used with --transport http)")
    parser.add_argument("--headless", action="store_true", help="Run Chromium headless (recommended when running as a long-lived service)")
    args = parser.parse_args()

    global _browser_headless
    _browser_headless = args.headless

    if args.transport == "http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        print(f"[gpt-tools] Starting streamable-http MCP server at http://{args.host}:{args.port}{mcp.settings.streamable_http_path} (headless={args.headless})", flush=True)
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
