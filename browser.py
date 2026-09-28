import os
import asyncio
import subprocess
import sys
from playwright.async_api import async_playwright

_STATE_HOME = os.environ.get("GPT_TOOLS_HOME") or os.path.dirname(os.path.abspath(__file__))
USER_DATA_DIR = os.path.join(_STATE_HOME, "chatgpt_profile")
DEBUG_DIR = os.path.join(_STATE_HOME, "debug")
PROVIDER_URLS = {
    "chatgpt": "https://chatgpt.com/?model=auto",
    "claude": "https://claude.ai/new",
}
TEMPORARY_CHATGPT_URL = "https://chatgpt.com/?model=auto&temporary-chat=true"
PROMPT_SELECTORS = {
    "chatgpt": [
        "#prompt-textarea",
        '[data-testid="prompt-textarea"]',
        'div[contenteditable="true"][role="textbox"]',
        'div[contenteditable="true"]',
        'textarea[placeholder*="Ask"]',
    ],
    "claude": [
        'div.ProseMirror[contenteditable="true"]',
        '[contenteditable="true"][role="textbox"]',
        'fieldset div[contenteditable="true"]',
    ],
}
SEND_BUTTON_SELECTORS = {
    "chatgpt": [
        '[data-testid="send-button"]',
        "#composer-submit-button",
        'button[aria-label*="Send prompt" i]',
        'button[aria-label*="Send message" i]',
        'button[aria-label*="Send" i]',
        'form button[type="submit"]',
    ],
    "claude": [
        'button[aria-label="Send Message"]',
        'button[aria-label*="Send message" i]',
        'button[aria-label*="Send" i]',
        'fieldset button[type="button"]',
    ],
}
ASSISTANT_MESSAGE_SELECTORS = {
    "chatgpt": '[data-message-role="assistant"], [data-message-author-role="assistant"], [data-turn="assistant"], [data-assistant-markdown]',
    "claude": '[data-is-streaming]',
}
STOP_GENERATING_SELECTORS = {
    "chatgpt": [
        '[data-testid="stop-button"]',
        '[aria-label="Stop generating"]',
        '[aria-label="Stop answering"]',
        '[aria-label="Stop thinking"]',
        'button[aria-label*="Stop" i]',
        'button:has-text("Stop answering")',
        'button:has-text("Stop thinking")',
        'button:has-text("Stop generating")',
        ".result-streaming",
        ".result-thinking",
        '[data-is-streaming="true"]',
    ],
    "claude": [
        'button[aria-label="Stop response"]',
        'button:has-text("Stop response")',
        '[data-is-streaming="true"]',
    ],
}
LOGIN_REQUIRED_SELECTORS = {
    "chatgpt": [
        '[data-testid="modal-no-auth-login"]',
        '[data-testid="login-button"]',
        'input[aria-label="Email address"]',
    ],
    "claude": [
        'button:has-text("Continue with Google")',
        'button:has-text("Continue with email")',
        'a:has-text("Log in")',
        'input[type="email"]',
    ],
}
TEMPORARY_CHAT_SELECTORS = [
    "button:has-text('Temporary')",
    "[data-testid*='temporary']",
    "[aria-label*='Temporary']",
    "[aria-label*='temporary']",
]
RATE_LIMIT_TITLES = [
    "too many requests",
    "you've hit your limit",
    "you have hit your limit",
]
RATE_LIMIT_SNIPPETS = [
    "making requests too quickly",
    "temporarily limited access to your conversations",
    "please wait a few minutes before trying again",
    "you've hit your limit",
    "you have hit your limit",
    "usage limit",
]
RATE_LIMIT_REQUIRED_SNIPPETS = 2
RATE_LIMIT_EXCLUDE_SELECTOR = ", ".join([
    '[data-message-role]',
    '[data-message-author-role]',
    '#prompt-textarea',
    '[data-testid="prompt-textarea"]',
    '[data-is-streaming]',
    'div.ProseMirror[contenteditable="true"]',
    '[contenteditable="true"][role="textbox"]',
])
# Full error strings ChatGPT renders *as the assistant message*. Matched against the whole
# trimmed response, never as a substring: a legitimate answer can discuss these verbatim.
CHATGPT_ERROR_RESPONSES = [
    "something went wrong. if this issue persists please contact us through our help center at help.openai.com",
    "something went wrong while generating the response. if this issue persists please contact us through our help center at help.openai.com",
    "there was an error generating a response",
    "our systems have detected unusual traffic from your computer network",
]
CHATGPT_ERROR_MAX_CHARS = max(len(s) for s in CHATGPT_ERROR_RESPONSES) + 30


class ChatGPTRateLimitError(RuntimeError):
    pass


class ChatGPTResponseError(RuntimeError):
    """ChatGPT answered with one of its own error messages instead of a real response."""
    pass


def _debug_path(name: str) -> str:
    os.makedirs(DEBUG_DIR, exist_ok=True)
    return os.path.join(DEBUG_DIR, name)


async def _chatgpt_assistant_turn_count(page) -> int:
    return int(await page.evaluate('''
        () => {
            const role = document.querySelectorAll('[data-message-role="assistant"]').length;
            const classic = document.querySelectorAll('[data-message-author-role="assistant"]').length;
            const modernTurns = document.querySelectorAll('[data-turn="assistant"]').length;
            const modern = [...document.querySelectorAll('h1, h2, h3, h4, h5, h6')]
                .filter(el => (el.innerText || '').trim().toLowerCase() === 'chatgpt said:')
                .length;
            return Math.max(role, classic, modernTurns, modern);
        }
    '''))


def _detect_chatgpt_error(text: str) -> str | None:
    """Return the error text if the response *is* a ChatGPT error, else None.

    Requires the whole normalized message to EQUAL a known error string. Prefix matching
    was tried and is wrong: an answer that legitimately quotes an error on its first line
    ("There was an error generating a response. Users see it when...") matched and turned
    a valid response into a hard failure. A real error message has nothing after it; a
    real answer keeps going. Errs toward returning text rather than killing valid calls,
    so a reworded error may pass through — that is the safe direction.
    """
    normalized = " ".join((text or "").split())
    if not normalized or len(normalized) > CHATGPT_ERROR_MAX_CHARS:
        return None

    candidate = normalized.lower()
    # inner_text picks up the Retry/Regenerate affordance rendered beside the error
    while True:
        stripped = candidate.rstrip(" .!?")
        for token in ("retry", "regenerate", "try again"):
            if stripped.endswith(token):
                stripped = stripped[: -len(token)].rstrip(" .!?")
                break
        else:
            candidate = stripped
            break
        candidate = stripped

    for phrase in CHATGPT_ERROR_RESPONSES:
        if candidate == phrase.rstrip(" ."):
            return normalized
    return None


def _image_text_is_terminal_without_urls(text: str) -> bool:
    normalized = " ".join((text or "").split()).lower()
    if normalized.startswith("chatgpt said:"):
        normalized = normalized.removeprefix("chatgpt said:").strip()
    if not normalized:
        return False
    return not any(
        phrase in normalized
        for phrase in ("thinking", "generating image", "hang tight", "working on")
    )


async def _get_rate_limit_message(page, provider: str = "chatgpt") -> str | None:
    try:
        return await page.evaluate(
            '''
            ({ titles, snippets, requiredSnippets, excludeSelector }) => {
                const bodyText = document.body?.innerText || '';
                const normalized = bodyText.toLowerCase();
                const titleHit = titles.some(title => normalized.includes(title));
                const snippetHits = snippets.filter(snippet => normalized.includes(snippet)).length;
                if (!titleHit && snippetHits < requiredSnippets) return null;

                const excludeSel = excludeSelector;
                const isVisible = el => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        Number(style.opacity || 1) !== 0 &&
                        rect.width > 0 && rect.height > 0;
                };
                const candidates = [
                    ...document.querySelectorAll('[role="dialog"], [aria-modal="true"], div')
                ].filter(el => isVisible(el) && !el.closest(excludeSel) && !el.querySelector(excludeSel))
                 .map(el => (el.innerText || '').trim())
                 .filter(text => {
                    const lower = text.toLowerCase();
                    const localTitleHit = titles.some(title => lower.includes(title));
                    const localHits = snippets.filter(snippet => lower.includes(snippet)).length;
                    return localTitleHit || localHits >= requiredSnippets;
                 })
                 .sort((a, b) => a.length - b.length);

                return candidates.length ? candidates[0].replace(/\\s+/g, ' ') : null;
            }
            ''',
            {
                "titles": RATE_LIMIT_TITLES,
                "snippets": RATE_LIMIT_SNIPPETS,
                "requiredSnippets": RATE_LIMIT_REQUIRED_SNIPPETS,
                "excludeSelector": RATE_LIMIT_EXCLUDE_SELECTOR,
            },
        )
    except Exception:
        return None


async def _dismiss_rate_limit_if_present(
    page,
    debug_name: str,
    provider: str = "chatgpt",
) -> str | None:
    message = await _get_rate_limit_message(page, provider)
    if not message:
        return None

    shot_path = _debug_path(debug_name)
    try:
        await page.screenshot(path=shot_path)
    except Exception:
        pass

    dismissed = False
    buttons = [
        page.get_by_role("button", name="Got it"),
        page.get_by_role("button", name="OK"),
        page.get_by_role("button", name="Dismiss"),
        page.locator('button:has-text("Got it")'),
        page.locator('button').filter(has_text="Got it"),
    ]
    for button in buttons:
        try:
            if await button.count() and await button.first.is_visible():
                await button.first.click()
                dismissed = True
                break
        except Exception:
            continue

    if not dismissed:
        raise ChatGPTRateLimitError(f"{provider.title()} rate limit detected, but the dialog could not be dismissed: {message}. Screenshot: {shot_path}")

    await page.wait_for_timeout(1000)
    if await _get_rate_limit_message(page, provider):
        raise ChatGPTRateLimitError(f"{provider.title()} rate limit dialog persisted after dismissal: {message}. Screenshot: {shot_path}")

    return message


async def _find_prompt_locator(
    page,
    provider: str = "chatgpt",
    timeout_ms: int = 30000,
):
    deadline = timeout_ms
    while deadline > 0:
        await _dismiss_rate_limit_if_present(
            page,
            f"debug_rate_limit_{provider}_prompt.png",
            provider,
        )
        for selector in PROMPT_SELECTORS[provider]:
            locator = page.locator(selector).first
            try:
                if await locator.count() and await locator.is_visible():
                    return locator
            except Exception:
                continue
        await page.wait_for_timeout(500)
        deadline -= 500
    raise TimeoutError(
        f"Failed to find visible {provider.title()} prompt input using selectors: "
        f"{', '.join(PROMPT_SELECTORS[provider])}"
    )


async def _composer_text(page, provider: str = "chatgpt") -> str:
    for selector in PROMPT_SELECTORS[provider]:
        locator = page.locator(selector).first
        try:
            if await locator.count() and await locator.is_visible():
                text = await locator.evaluate("el => el.value ?? el.innerText ?? ''")
                return (text or "").strip()
        except Exception:
            continue
    return ""


async def _wait_for_composer_clear(
    page,
    provider: str = "chatgpt",
    timeout_ms: int = 15000,
) -> bool:
    waited = 0
    while waited < timeout_ms:
        if not await _composer_text(page, provider):
            return True
        await page.wait_for_timeout(250)
        waited += 250
    return not await _composer_text(page, provider)


async def _click_send_button(page, provider: str = "chatgpt") -> bool:
    for selector in SEND_BUTTON_SELECTORS[provider]:
        locator = page.locator(selector).first
        try:
            if not await locator.count() or not await locator.is_visible():
                continue
            if await locator.is_disabled():
                continue
            try:
                await locator.click(timeout=5000)
            except Exception:
                await locator.click(timeout=5000, force=True)
            return True
        except Exception:
            continue
    return False


async def _is_response_started(page, provider: str = "chatgpt") -> bool:
    for selector in STOP_GENERATING_SELECTORS.get(provider, []):
        locator = page.locator(selector).first
        try:
            if await locator.count() and await locator.is_visible():
                return True
        except Exception:
            continue
    assistant_selector = ASSISTANT_MESSAGE_SELECTORS.get(provider)
    if assistant_selector:
        try:
            if await page.locator(assistant_selector).count():
                return True
        except Exception:
            pass
    return False


async def _submit_prompt(page, prompt, provider: str = "chatgpt") -> bool:
    """Submit the composer. Returns True once text left composer or response started."""
    if await _is_response_started(page, provider):
        return True
    if await _click_send_button(page, provider):
        if await _wait_for_composer_clear(page, provider) or await _is_response_started(page, provider):
            return True
    try:
        await prompt.press("Enter", timeout=5000)
    except Exception:
        return False
    return await _wait_for_composer_clear(page, provider) or await _is_response_started(page, provider)


async def _is_login_required(page, provider: str) -> bool:
    for selector in LOGIN_REQUIRED_SELECTORS[provider]:
        locator = page.locator(selector).first
        try:
            if await locator.count() and await locator.is_visible():
                return True
        except Exception:
            continue
    return False


def _browser_pid_from_ps_output(ps_output: str, user_data_dir: str) -> int | None:
    marker = f"--user-data-dir={user_data_dir}"
    for line in ps_output.splitlines():
        stripped = line.strip()
        if marker not in stripped or " --type=" in stripped:
            continue
        pid_text, _, _ = stripped.partition(" ")
        try:
            return int(pid_text)
        except ValueError:
            continue
    return None


def _find_browser_pid(user_data_dir: str) -> int | None:
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return _browser_pid_from_ps_output(result.stdout, user_data_dir)


def _set_browser_hidden(pid: int | None, hidden: bool) -> bool:
    if sys.platform != "darwin" or pid is None:
        return False
    visibility = "false" if hidden else "true"
    script = (
        'tell application "System Events" to tell first process '
        f'whose unix id is {pid}\n'
        f'    set visible to {visibility}\n'
        + (
            '    repeat with browser_window in windows\n'
            '        set value of attribute "AXMinimized" of browser_window to false\n'
            '    end repeat\n'
            if not hidden else ""
        )
        + "end tell"
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


async def _new_background_page(context):
    browser = context.browser
    if browser is None:
        return await context.new_page()

    cdp = await browser.new_browser_cdp_session()
    try:
        async with context.expect_page(timeout=10000) as page_info:
            await cdp.send(
                "Target.createTarget",
                {"url": "about:blank", "background": True},
            )
        return await page_info.value
    finally:
        await cdp.detach()


class ChatGPTBrowser:
    def __init__(self, headless=False, background=True):
        self.headless = headless
        self.background = background
        self.playwright = None
        self.context = None
        self._closed = False
        self._browser_pid = None
        self._render_visibility_users = 0

    async def start(self):
        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-minimized",
            ],
        )
        self.context.on("close", self._on_context_close)
        self._closed = False
        for _ in range(20):
            self._browser_pid = _find_browser_pid(USER_DATA_DIR)
            if self._browser_pid is not None:
                break
            await asyncio.sleep(0.1)
        if self.background and not self.headless:
            _set_browser_hidden(self._browser_pid, True)

    def _on_context_close(self, *_args):
        self._closed = True

    @property
    def is_alive(self) -> bool:
        return self.context is not None and not self._closed

    def reveal_for_rendering(self):
        if not self.background or self.headless:
            return
        self._render_visibility_users += 1
        if self._render_visibility_users == 1:
            _set_browser_hidden(self._browser_pid, False)

    def hide_after_rendering(self):
        if not self.background or self.headless:
            return
        self._render_visibility_users = max(0, self._render_visibility_users - 1)
        if self._render_visibility_users == 0:
            _set_browser_hidden(self._browser_pid, True)

    async def _wait_for_login_if_needed(self, page, provider: str):
        try:
            return await _find_prompt_locator(page, provider, timeout_ms=30000)
        except Exception as first_error:
            if not await _is_login_required(page, provider):
                raise first_error

        if self.background and not self.headless:
            _set_browser_hidden(self._browser_pid, False)
        print(
            f"[gpt-tools] Log in to {provider.title()} in the browser window; "
            "automation will resume and hide it again afterward.",
            flush=True,
        )
        try:
            return await _find_prompt_locator(page, provider, timeout_ms=900000)
        finally:
            if self.background and not self.headless:
                _set_browser_hidden(self._browser_pid, True)

    async def new_session(self, provider: str = "chatgpt", temporary_chat: bool = False):
        if not self.is_alive:
            raise RuntimeError("Browser context is not alive; call start() first.")
        if provider not in PROVIDER_URLS:
            raise ValueError(f"Unsupported provider {provider!r}; expected one of {sorted(PROVIDER_URLS)}")
        if temporary_chat and provider != "chatgpt":
            raise ValueError("Temporary chat is only supported for ChatGPT.")
        try:
            if self.background and not self.headless and sys.platform == "darwin":
                page = await _new_background_page(self.context)
            else:
                page = await self.context.new_page()
        except Exception as e:
            self._closed = True
            raise RuntimeError(f"Failed to open new page (browser likely disconnected): {e}")

        url = TEMPORARY_CHATGPT_URL if temporary_chat else PROVIDER_URLS[provider]
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            await self._wait_for_login_if_needed(page, provider)
            if temporary_chat and "temporary-chat=true" not in page.url:
                for selector in TEMPORARY_CHAT_SELECTORS:
                    locator = page.locator(selector).first
                    try:
                        if await locator.count() and await locator.is_visible():
                            await locator.click()
                            await page.wait_for_timeout(750)
                            await _find_prompt_locator(page, provider, timeout_ms=15000)
                            break
                    except Exception:
                        continue
        except ChatGPTRateLimitError:
            await page.close()
            raise
        except Exception as e:
            shot_path = _debug_path("debug_session_init.png")
            try:
                await page.screenshot(path=shot_path)
            except Exception:
                pass
            await page.close()
            raise Exception(f"Failed to find {provider.title()} input on new session. Screenshot: {shot_path}. Error: {e}")
        if self.background and not self.headless:
            _set_browser_hidden(self._browser_pid, True)
        return ChatGPTSession(page, provider)

    async def close(self):
        self._render_visibility_users = 0
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
        self._closed = True


class ChatGPTSession:
    def __init__(self, page, provider: str = "chatgpt"):
        self.page = page
        self.provider = provider

    async def close(self):
        try:
            await self.page.close()
        except Exception:
            pass

    async def _send_prompt(self, message: str):
        last_error = None
        for _ in range(3):
            prompt = await _find_prompt_locator(
                self.page,
                self.provider,
                timeout_ms=30000,
            )
            existing_text = await _composer_text(self.page, self.provider)
            if not existing_text or existing_text != message.strip():
                try:
                    await prompt.focus()
                    inserted = False
                    if self.provider in ("chatgpt", "claude"):
                        try:
                            inserted = await self.page.evaluate("""(text) => {
                                const el = document.querySelector('#prompt-textarea') || document.querySelector('div.ProseMirror[contenteditable="true"]') || document.querySelector('[contenteditable="true"]');
                                if (!el) return false;
                                el.focus();
                                document.execCommand('selectAll', false, null);
                                document.execCommand('delete', false, null);
                                const ok = document.execCommand('insertText', false, text);
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                return ok;
                            }""", message)
                        except Exception:
                            inserted = False
                    if not inserted or not await _composer_text(self.page, self.provider):
                        await prompt.fill(message, timeout=60000)
                except Exception as e:
                    shot_path = _debug_path("debug_fill_failed.png")
                    try:
                        await self.page.screenshot(path=shot_path)
                    except Exception:
                        pass
                    last_error = e
                    continue
            if not await _submit_prompt(self.page, prompt, self.provider):
                shot_path = _debug_path("debug_submit_failed.png")
                try:
                    await self.page.screenshot(path=shot_path)
                except Exception:
                    pass
                last_error = Exception(
                    f"prompt text stayed in the composer after clicking send and pressing Enter. Screenshot: {shot_path}"
                )
                continue
            await self.page.wait_for_timeout(250)
            return await _dismiss_rate_limit_if_present(
                self.page,
                f"debug_rate_limit_{self.provider}_send.png",
                self.provider,
            )
        raise Exception(f"Failed to send the {self.provider.title()} prompt after 3 attempts: {last_error}")

    async def stream_message(self, message: str, raw_output: bool = False, timeout_ms: int = 480000):
        assistant_selector = ASSISTANT_MESSAGE_SELECTORS[self.provider]
        elements_before = await self.page.query_selector_all(assistant_selector)
        count_before = len(elements_before)

        rate_limit_message = await self._send_prompt(message)

        wait_time = 0
        wait_timeout_ms = 30000 if self.provider == "chatgpt" else min(timeout_ms, 120000)
        while wait_time < wait_timeout_ms:
            rate_limit_message = await _dismiss_rate_limit_if_present(
                self.page,
                f"debug_rate_limit_{self.provider}_text_wait.png",
                self.provider,
            ) or rate_limit_message
            current_count = len(await self.page.query_selector_all(assistant_selector))
            if current_count > count_before:
                break
            await self.page.wait_for_timeout(500)
            wait_time += 500

        if wait_time >= wait_timeout_ms:
            shot_path = _debug_path("debug_send.png")
            try:
                await self.page.screenshot(path=shot_path)
            except Exception:
                pass
            try:
                html_path = _debug_path("debug_send_dom.html")
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(await self.page.content())
            except Exception:
                pass
            if rate_limit_message:
                raise ChatGPTRateLimitError(f"{self.provider.title()} rate limit dialog was dismissed, but no assistant response appeared: {rate_limit_message}. Screenshot: {shot_path}")
            raise Exception(f"Timeout waiting for assistant message to appear. Screenshot: {shot_path}")

        last_text = ""
        stable_count = 0
        elapsed = 0
        while True:
            if elapsed >= timeout_ms:
                shot_path = _debug_path("debug_text_timeout.png")
                try:
                    await self.page.screenshot(path=shot_path)
                except Exception:
                    pass
                raise Exception(f"Timed out waiting for the response to finish streaming after {timeout_ms}ms. Screenshot: {shot_path}")
            await self.page.wait_for_timeout(500)
            elapsed += 500
            rate_limit_message = await _dismiss_rate_limit_if_present(
                self.page,
                f"debug_rate_limit_{self.provider}_text_stream.png",
                self.provider,
            ) or rate_limit_message
            elements = await self.page.query_selector_all(assistant_selector)
            if not elements:
                continue

            current_text = await elements[-1].inner_text()

            if current_text == last_text and current_text.strip() != "":
                 stable_count += 1
            else:
                 stable_count = 0

            last_text = current_text

            if stable_count >= 5:
                is_streaming = False
                for selector in STOP_GENERATING_SELECTORS[self.provider]:
                    locator = self.page.locator(selector).first
                    try:
                        if await locator.count() and await locator.is_visible():
                            is_streaming = True
                            break
                    except Exception:
                        continue

                if not is_streaming:
                    break
                else:
                    stable_count = 3

        shot_path = _debug_path("debug_raw_output.png")
        try:
            await self.page.screenshot(path=shot_path)
        except Exception:
            pass

        elements = await self.page.query_selector_all(assistant_selector)
        if not elements:
            raise Exception("Could not locate the response in the DOM.")

        last_response = elements[-1]

        html_dump = _debug_path("debug_raw_output.html")
        try:
            with open(html_dump, "w", encoding="utf-8") as f:
                f.write(await last_response.evaluate("el => el.innerHTML"))
        except Exception:
            pass

        if self.provider == "chatgpt":
            chatgpt_error = _detect_chatgpt_error(await last_response.inner_text())
            if chatgpt_error:
                raise ChatGPTResponseError(chatgpt_error)

        if raw_output:
            raw_payload = await last_response.evaluate('''
                (el) => {
                    const markdownEl = el.querySelector('[data-assistant-markdown]') || el.querySelector('.markdown') || el;
                    const codeBlocks = [...markdownEl.querySelectorAll('pre code')]
                        .map(node => (node.innerText || node.textContent || '').trim())
                        .filter(Boolean);

                    if (codeBlocks.length === 1) {
                        return codeBlocks[0];
                    }

                    return (markdownEl.innerText || markdownEl.textContent || el.innerText || '').trim();
                }
            ''')
            final_raw = (raw_payload or "").strip()
            print(f"[debug-stream] final_raw len={len(final_raw)} snippet={final_raw[:80]!r}")
            if rate_limit_message and not final_raw:
                raise ChatGPTRateLimitError(f"{self.provider.title()} rate limit dialog was dismissed, but the assistant response was empty: {rate_limit_message}")

            yield {"type": "final", "content": final_raw, "sources": []}
            return

        if self.provider != "chatgpt":
            final_text = (await last_response.inner_text()).strip()
            if rate_limit_message and not final_text:
                raise ChatGPTRateLimitError(
                    f"{self.provider.title()} rate limit dialog was dismissed, but the assistant response was empty: {rate_limit_message}"
                )
            yield {"type": "final", "content": final_text, "sources": []}
            return

        html_payload = await last_response.evaluate('''
            (el) => {
                let cloned = el.cloneNode(true);

                cloned.querySelectorAll('svg').forEach(x => x.remove());

                let sources = [];
                let refs = cloned.querySelectorAll('.citation, a, button, sup');

                refs.forEach(node => {
                    let isCitation = node.tagName === 'BUTTON' || node.tagName === 'SUP' || (node.classList && node.classList.contains('citation'));
                    let isLink = node.tagName === 'A' && node.href;

                    if (isLink) {
                        let link = node.href;
                        if (link.startsWith('http') && !link.includes('chatgpt.com/c/')) {
                            if (!sources.includes(link)) {
                                sources.push(link);
                            }
                            if (node.classList.length > 2 || node.textContent.length < 25) {
                                isCitation = true;
                            }
                        }
                    }

                    if (isCitation) {
                        if (isLink) {
                            let num = sources.indexOf(node.href) + 1;
                            let span = document.createElement('span');
                            span.textContent = ` [${num}]`;
                            node.parentNode.replaceChild(span, node);
                        } else {
                            node.remove();
                        }
                    }
                });

                cloned.querySelectorAll('details, .search-results').forEach(x => x.remove());

                let markdownEl = cloned.querySelector('[data-assistant-markdown]') || cloned.querySelector('.markdown');
                let clean_html = markdownEl ? markdownEl.innerHTML : cloned.innerHTML;

                return {html: clean_html, sources: sources};
            }
        ''')

        from markdownify import markdownify
        final_markdown = markdownify(html_payload["html"], heading_style="ATX").strip()
        if rate_limit_message and not final_markdown:
            raise ChatGPTRateLimitError(f"ChatGPT rate limit dialog was dismissed, but the assistant response was empty: {rate_limit_message}")

        yield {"type": "final", "content": final_markdown, "sources": html_payload["sources"]}

    async def stream_image_message(self, message: str, timeout_ms: int = 480000):
        if self.provider != "chatgpt":
            raise ValueError("Image generation is only supported for ChatGPT.")
        count_before = await _chatgpt_assistant_turn_count(self.page)

        rate_limit_message = await self._send_prompt(message)

        appeared = False
        appear_wait = 0
        appear_timeout_ms = min(timeout_ms, 120000)
        while appear_wait < appear_timeout_ms:
            rate_limit_message = await _dismiss_rate_limit_if_present(
                self.page,
                "debug_rate_limit_chatgpt_image_wait.png",
                self.provider,
            ) or rate_limit_message
            current_count = await _chatgpt_assistant_turn_count(self.page)
            if current_count > count_before:
                appeared = True
                break
            await self.page.wait_for_timeout(500)
            appear_wait += 500
        if not appeared:
            shot_path = _debug_path("debug_image_send.png")
            screenshot_note = ""
            try:
                await self.page.screenshot(path=shot_path, timeout=5000)
            except Exception as exc:
                screenshot_note = f" (debug screenshot failed: {type(exc).__name__}: {exc})"
            if rate_limit_message:
                raise ChatGPTRateLimitError(f"ChatGPT rate limit dialog was dismissed, but no image-gen response appeared: {rate_limit_message}. Screenshot: {shot_path}{screenshot_note}")
            raise Exception(f"Timeout waiting {appear_timeout_ms}ms for assistant message to appear after image-gen prompt. Screenshot: {shot_path}{screenshot_note}")

        completed = False
        stable_after_done = 0
        last_url_count = 0
        elapsed = 0
        while elapsed < timeout_ms:
            await self.page.wait_for_timeout(2000)
            elapsed += 2000
            rate_limit_message = await _dismiss_rate_limit_if_present(
                self.page,
                "debug_rate_limit_chatgpt_image_stream.png",
                self.provider,
            ) or rate_limit_message

            stop_btn = await self.page.query_selector('[data-testid="stop-button"]')
            aria_stop = await self.page.query_selector('[aria-label="Stop generating"]')
            stop_visible = False
            if stop_btn:
                try:
                    stop_visible = await stop_btn.is_visible()
                except Exception:
                    pass
            if not stop_visible and aria_stop:
                try:
                    stop_visible = await aria_stop.is_visible()
                except Exception:
                    pass

            urls = await self.page.evaluate('''
                () => {
                    const root = document.querySelector('main') || document.body;
                    const imgs = root.querySelectorAll('img');
                    const valid = [];
                    const seen = new Set();
                    const patterns = ['backend-api/estuary/content', 'oaiusercontent.com', 'backend-api/files', 'chatgpt.com/backend-api/'];
                    for (const img of imgs) {
                        const src = img.src || img.currentSrc || '';
                        if (!src.startsWith('http')) continue;
                        if (!img.complete) continue;
                        if (img.naturalWidth < 200) continue;
                        if (!patterns.some(p => src.includes(p))) continue;
                        const alt = (img.alt || '').toLowerCase();
                        if (!alt.includes('generated')) continue;
                        if (seen.has(src)) continue;
                        seen.add(src);
                        valid.push(src);
                    }
                    return valid;
                }
            ''')

            if not stop_visible:
                if len(urls) > 0 and len(urls) == last_url_count:
                    stable_after_done += 1
                    if stable_after_done >= 1:
                        completed = True
                        break
                elif len(urls) == 0 and last_url_count == 0:
                    assistant_elements = await self.page.query_selector_all(
                        ASSISTANT_MESSAGE_SELECTORS["chatgpt"]
                    )
                    assistant_text = ""
                    if assistant_elements:
                        try:
                            assistant_text = await assistant_elements[-1].inner_text()
                        except Exception:
                            pass
                    if _image_text_is_terminal_without_urls(assistant_text):
                        stable_after_done += 1
                        if stable_after_done >= 2:
                            completed = True
                            break
                    else:
                        stable_after_done = 0
                last_url_count = len(urls)
            else:
                stable_after_done = 0
                last_url_count = len(urls)

        if not completed:
            shot_path = _debug_path("debug_image_timeout.png")
            screenshot_note = ""
            try:
                await self.page.screenshot(path=shot_path, timeout=5000)
            except Exception as exc:
                screenshot_note = f" (debug screenshot failed: {type(exc).__name__}: {exc})"
            if rate_limit_message:
                raise ChatGPTRateLimitError(f"ChatGPT rate limit dialog was dismissed, but image generation did not complete: {rate_limit_message}. Screenshot: {shot_path}{screenshot_note}")
            raise Exception(f"Image generation timed out after {timeout_ms}ms. Screenshot: {shot_path}{screenshot_note}")

        final_data = await self.page.evaluate('''
            () => {
                const root = document.querySelector('main') || document.body;
                const imgs = root.querySelectorAll('img');
                const valid_urls = [];
                const seen = new Set();
                const patterns = ['backend-api/estuary/content', 'oaiusercontent.com', 'backend-api/files', 'chatgpt.com/backend-api/'];
                for (const img of imgs) {
                    const src = img.src || img.currentSrc || '';
                    if (!src.startsWith('http')) continue;
                    if (!img.complete) continue;
                    if (img.naturalWidth < 200) continue;
                    if (!patterns.some(p => src.includes(p))) continue;
                    const alt = (img.alt || '').toLowerCase();
                    if (!alt.includes('generated')) continue;
                    if (seen.has(src)) continue;
                    seen.add(src);
                    valid_urls.push(src);
                }
                const lastMsg = [...document.querySelectorAll('[data-message-role="assistant"], [data-message-author-role="assistant"], [data-turn="assistant"]')].pop();
                let text = '';
                if (lastMsg) {
                    const cloned = lastMsg.cloneNode(true);
                    cloned.querySelectorAll('img, svg, button').forEach(x => x.remove());
                    text = (cloned.innerText || '').trim();
                }
                return { urls: valid_urls, text };
            }
        ''')
        if rate_limit_message and not final_data["urls"] and not final_data["text"]:
            raise ChatGPTRateLimitError(f"ChatGPT rate limit dialog was dismissed, but image generation returned no images or text: {rate_limit_message}")

        image_blobs = []
        for url in final_data['urls']:
            try:
                resp = await self.page.request.get(url)
                if resp.ok:
                    content_type = (resp.headers.get('content-type') or 'image/png').split(';')[0].strip()
                    image_blobs.append({
                        'bytes': await resp.body(),
                        'mime': content_type,
                        'url': url,
                    })
            except Exception:
                continue

        return {
            'images': image_blobs,
            'text': final_data['text'],
        }
