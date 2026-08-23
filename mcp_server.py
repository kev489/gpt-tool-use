import os
import sys
import argparse
import ast
import asyncio
import base64
import hashlib
import json
import re
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent, ImageContent

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from browser import ChatGPTBrowser, ChatGPTResponseError, USER_DATA_DIR

try:
    from json_repair import repair_json
except ImportError:
    repair_json = None

mcp = FastMCP("gpt-tools")

_browser: ChatGPTBrowser | None = None
_browser_lock: asyncio.Lock | None = None
_browser_headless: bool = False
_tab_semaphore: asyncio.Semaphore | None = None
MAX_CONCURRENT_CHATGPT_TABS = 3


def _get_lock() -> asyncio.Lock:
    global _browser_lock
    if _browser_lock is None:
        _browser_lock = asyncio.Lock()
    return _browser_lock


def _get_tab_semaphore() -> asyncio.Semaphore:
    global _tab_semaphore
    if _tab_semaphore is None:
        _tab_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHATGPT_TABS)
    return _tab_semaphore


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


def _resolve_text_path(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _clean_search_result(markdown: str) -> str:
    return re.sub(r'\s*\[\d+\]', '', markdown)


def _strip_trailing_json_commas(text: str) -> str:
    chars: list[str] = []
    in_string = False
    escape = False
    for i, char in enumerate(text):
        if in_string:
            chars.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            chars.append(char)
            continue

        if char == ",":
            next_i = i + 1
            while next_i < len(text) and text[next_i].isspace():
                next_i += 1
            if next_i < len(text) and text[next_i] in "}]":
                continue

        chars.append(char)
    return "".join(chars)


def _strip_invalid_json_string_escapes(text: str) -> str:
    chars: list[str] = []
    in_string = False
    escape = False
    valid_escapes = {'"', "\\", "/", "b", "f", "n", "r", "t", "u"}
    for char in text:
        if not in_string:
            chars.append(char)
            if char == '"':
                in_string = True
            continue

        if escape:
            if char in valid_escapes:
                chars.append("\\")
            chars.append(char)
            escape = False
            continue

        if char == "\\":
            escape = True
            continue

        chars.append(char)
        if char == '"':
            in_string = False

    if escape:
        chars.append("\\")
    return "".join(chars)


def _json_candidate_attempts(candidate: str):
    stripped = candidate.strip()
    transforms = [
        _strip_invalid_json_string_escapes(stripped),
        stripped,
    ]
    attempts: list[str] = []
    for transformed in transforms:
        if not transformed:
            continue
        attempts.append(transformed)
        attempts.append(_strip_trailing_json_commas(transformed))

    seen: set[str] = set()
    for attempt in attempts:
        if attempt and attempt not in seen:
            seen.add(attempt)
            yield attempt


def _is_json_value(value) -> bool:
    return value is None or isinstance(value, (dict, list, str, int, float, bool))


def _is_plausible_repaired_value(candidate: str, value) -> bool:
    if isinstance(value, (dict, list)):
        return True
    stripped = candidate.strip()
    if not stripped:
        return False
    if value == "" and stripped not in ('""', "''"):
        return False
    return stripped[0] in '"\'-0123456789tfn'


def _load_json_candidate(candidate: str):
    attempts = list(_json_candidate_attempts(candidate))

    def _reject_constant(value: str):
        raise ValueError(f"Invalid JSON constant: {value}")

    for attempt in attempts:
        try:
            return json.loads(attempt, parse_constant=_reject_constant)
        except (json.JSONDecodeError, ValueError):
            pass

    for attempt in attempts:
        try:
            value = ast.literal_eval(attempt)
        except (SyntaxError, ValueError):
            continue
        if _is_json_value(value):
            return value

    if repair_json is not None:
        for attempt in attempts:
            try:
                value = repair_json(attempt, return_objects=True)
            except Exception:
                continue
            if _is_json_value(value) and _is_plausible_repaired_value(attempt, value):
                return value

    raise ValueError("candidate is not valid JSON")


def _iter_balanced_json_blocks(text: str):
    stack: list[str] = []
    start: int | None = None
    in_string: str | None = None
    escape = False
    pairs = {"}": "{", "]": "["}

    for i, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == in_string:
                in_string = None
            continue

        if char in ("'", '"'):
            if stack:
                in_string = char
            continue

        if char in "{[":
            if not stack:
                start = i
            stack.append(char)
            continue

        if char in "}]":
            if not stack or stack[-1] != pairs[char]:
                stack = []
                start = None
                continue

            stack.pop()
            if not stack and start is not None:
                yield text[start:i + 1]
                start = None


def _iter_json_candidates(text: str):
    stripped = text.strip().removeprefix("\ufeff").strip()
    fenced: list[tuple[int, str]] = []
    for match in re.finditer(r"```(?P<lang>[a-zA-Z0-9_-]*)\s*(?P<body>.*?)```", stripped, re.DOTALL):
        lang = match.group("lang").lower()
        priority = 0 if lang in ("json", "jsonc") else 1
        fenced.append((priority, match.group("body").strip()))

    candidates = [stripped]
    candidates.extend(body for _, body in sorted(fenced, key=lambda item: item[0]))
    candidates.extend(sorted(_iter_balanced_json_blocks(stripped), key=len, reverse=True))

    seen: set[str] = set()
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            yield candidate


def _clean_json_result(result: str) -> str:
    for candidate in _iter_json_candidates(result):
        try:
            value = _load_json_candidate(candidate)
        except ValueError:
            continue
        return json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"

    preview = result.strip().replace("\n", " ")[:200]
    raise ValueError(f"ChatGPT response could not be converted to valid JSON. Response preview: {preview}")


def _try_clean_json_result(result: str) -> str | None:
    try:
        return _clean_json_result(result)
    except ValueError:
        return None


def _process_search_result(
    raw_result: str,
    output_file: str | None,
    output_json: bool,
) -> tuple[str, Path | None, bool | None]:
    if output_json:
        saved_path = _save_search_result(raw_result, output_file)
        json_result = _try_clean_json_result(raw_result)
        if json_result is not None:
            if saved_path is not None:
                saved_path.write_text(json_result, encoding="utf-8")
            return json_result, saved_path, True
        return raw_result, saved_path, False

    result = _clean_search_result(raw_result)
    saved_path = _save_search_result(result, output_file)
    return result, saved_path, None


def _saved_search_summary(saved_path: Path, json_cleaned: bool | None) -> str:
    if json_cleaned is True:
        return f"Saved ChatGPT response to {saved_path} (normalized to valid JSON)"
    if json_cleaned is False:
        return f"Saved raw ChatGPT response to {saved_path} (JSON post-processing did not produce valid JSON)"
    return f"Saved ChatGPT response to {saved_path}"


def _completed_search_summary(json_cleaned: bool | None) -> str:
    if json_cleaned is True:
        return "ChatGPT response completed and normalized to valid JSON. return_output=False and no output_file was provided."
    if json_cleaned is False:
        return "ChatGPT response completed, but JSON post-processing did not produce valid JSON. return_output=False and no output_file was provided."
    return "ChatGPT response completed. return_output=False and no output_file was provided."


def _read_search_prompt(query: str | None, prompt_file: str | None) -> str:
    if query and prompt_file:
        raise ValueError("Provide either query or prompt_file, not both.")
    if not query and not prompt_file:
        raise ValueError("Provide query or prompt_file.")
    if prompt_file:
        prompt_path = _resolve_text_path(prompt_file)
        return prompt_path.read_text(encoding="utf-8")
    return query or ""


def _save_search_result(result: str, output_file: str | None) -> Path | None:
    if not output_file:
        return None
    saved_path = _resolve_text_path(output_file)
    saved_path.parent.mkdir(parents=True, exist_ok=True)
    saved_path.write_text(result, encoding="utf-8")
    return saved_path


async def _new_session_with_retry():
    bot = await _get_browser()
    try:
        return await bot.new_session()
    except RuntimeError:
        bot = await _get_browser()
        return await bot.new_session()


async def _run_search_in_session(query: str, raw_output: bool = False) -> str:
    # ChatGPT intermittently answers with its own "Something went wrong" error. That chat
    # tab usually stays broken, so recover by retrying the query in a brand-new session
    # rather than re-sending into the same one. Persistent failures still raise.
    last_error = None
    for _ in range(2):
        async with _get_tab_semaphore():
            session = await _new_session_with_retry()
            try:
                result = ""
                async for chunk in session.stream_message(query, raw_output=raw_output):
                    if chunk["type"] == "final":
                        result = chunk["content"]
                return result
            except ChatGPTResponseError as e:
                last_error = e
            finally:
                await session.close()
    raise ChatGPTResponseError(f"ChatGPT returned an error response twice in fresh sessions: {last_error}")


async def _run_image_in_session(prompt: str) -> dict:
    async with _get_tab_semaphore():
        session = await _new_session_with_retry()
        try:
            return await session.stream_image_message(prompt)
        finally:
            await session.close()


@mcp.tool()
async def gpt_search(
    query: str | None = None,
    prompt_file: str | None = None,
    output_file: str | None = None,
    return_output: bool | None = None,
    output_json: bool = False,
) -> str:
    """Search the web or research a topic using ChatGPT.

    Provide either `query` or `prompt_file`. The prompt is sent directly to ChatGPT.

    Args:
        query: Full prompt to send to ChatGPT.
        prompt_file: Path to a text file containing the prompt. Relative paths resolve from the MCP server process working directory.
        output_file: Optional path where the cleaned markdown response should be saved. Parent directories are created if needed.
        return_output: When True, return the full response to the MCP client. When False, return only a short saved-path summary. Defaults to True unless `output_file` is provided, in which case it defaults to False to keep client context light.
        output_json: When True, try to parse/repair the response as JSON after ChatGPT returns. If `output_file` is provided, raw output is saved first and overwritten only when JSON post-processing succeeds. If post-processing fails, raw output is left in place.
    """
    query = _read_search_prompt(query, prompt_file)
    result, saved_path, json_cleaned = _process_search_result(
        await _run_search_in_session(query, raw_output=output_json),
        output_file,
        output_json,
    )

    if return_output is None:
        return_output = saved_path is None

    if not return_output:
        if saved_path is not None:
            return _saved_search_summary(saved_path, json_cleaned)
        return _completed_search_summary(json_cleaned)

    return result


@mcp.tool()
async def gpt_search_batch(
    requests: list[dict],
    return_output: bool | None = None,
    output_json: bool = False,
) -> str:
    """Run multiple ChatGPT search/research prompts concurrently.

    Each request opens its own ChatGPT tab and runs concurrently with the others. This is the text equivalent of `gpt_image_gen_batch`. The server runs at most 3 ChatGPT tabs at a time across all calls and sessions; larger batches queue internally.

    Each item in `requests` is a dict with keys:
      - `query` (optional, str): the full prompt
      - `prompt_file` (optional, str): path to a text file containing the prompt
      - `output_file` (optional, str): path where the cleaned markdown response should be saved
      - `label` (optional, str): heading used for this item in the combined response; defaults to `output_file`, then `prompt_file`, then `request_<n>`
      - `return_output` (optional, bool): overrides the batch-level `return_output` for this item
      - `output_json` (optional, bool): overrides the batch-level `output_json` for this item

    Provide either `query` or `prompt_file` for each item. Relative file paths resolve from the MCP server process working directory.

    `return_output` is batch-level unless overridden per item. When omitted, each item defaults to returning the full output only if it has no `output_file`.
    `output_json` is batch-level unless overridden per item. When enabled, the response is parsed/repaired after ChatGPT returns. For file outputs, raw output is saved first and overwritten only when JSON post-processing succeeds.

    Returns a markdown summary. If outputs are returned, they are grouped under per-request headings.
    """
    if not requests:
        return "No requests provided."

    async def _one(index: int, req: dict) -> str:
        label = req.get("label") or req.get("output_file") or req.get("prompt_file") or f"request_{index + 1}"
        query = _read_search_prompt(req.get("query"), req.get("prompt_file"))
        item_output_json = req.get("output_json", output_json)
        result, saved_path, json_cleaned = _process_search_result(
            await _run_search_in_session(query, raw_output=item_output_json),
            req.get("output_file"),
            item_output_json,
        )

        item_return_output = req.get("return_output", return_output)
        if item_return_output is None:
            item_return_output = saved_path is None

        if item_return_output:
            if saved_path is not None:
                return f"## {label}\n\n{_saved_search_summary(saved_path, json_cleaned)}\n\n{result}"
            return f"## {label}\n\n{result}"

        if saved_path is not None:
            return f"[{label}] {_saved_search_summary(saved_path, json_cleaned)}"
        return f"[{label}] {_completed_search_summary(json_cleaned)}"

    packed = await asyncio.gather(
        *[_one(i, r) for i, r in enumerate(requests)],
        return_exceptions=True,
    )

    output: list[str] = []
    for i, item in enumerate(packed):
        req = requests[i]
        label = req.get("label") or req.get("output_file") or req.get("prompt_file") or f"request_{i + 1}"
        if isinstance(item, Exception):
            output.append(f"[{label}] FAILED: {item}")
        else:
            output.append(item)
    return "\n\n".join(output)


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
    result = await _run_image_in_session(prompt)
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

    All items run concurrently, capped at 3 ChatGPT tabs at a time server-wide; larger batches queue internally. If one fails, the others still complete; failed items show up in the response as `[<prefix>] FAILED: <error>`.

    Account-level rate limits may still apply under sustained concurrency.

    Returns a list of MCP content blocks: per-item text summaries plus, if embed_images is True, the image blobs in order.
    """
    if not requests:
        return [TextContent(type="text", text="No requests provided.")]

    async def _one(req: dict):
        prompt = req["prompt"]
        result = await _run_image_in_session(prompt)
        return _save_and_pack(prompt, result, req.get("filename_prefix"), req.get("save_dir"), embed_images)

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
    parser.add_argument("--headless", action="store_true", help="Run Chromium headless. Not recommended for ChatGPT/launchd because bot checks may block the prompt UI.")
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
