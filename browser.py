import os
import asyncio
from playwright.async_api import async_playwright

_STATE_HOME = os.environ.get("GPT_TOOLS_HOME") or os.path.dirname(os.path.abspath(__file__))
USER_DATA_DIR = os.path.join(_STATE_HOME, "chatgpt_profile")
DEBUG_DIR = os.path.join(_STATE_HOME, "debug")
PROMPT_SELECTORS = [
    "#prompt-textarea",
    '[data-testid="prompt-textarea"]',
    'div[contenteditable="true"][role="textbox"]',
    'div[contenteditable="true"]',
    'textarea[placeholder*="Ask"]',
]
SEND_BUTTON_SELECTORS = [
    '[data-testid="send-button"]',
    "#composer-submit-button",
    'button[aria-label*="Send prompt" i]',
    'button[aria-label*="Send message" i]',
    'button[aria-label*="Send" i]',
    'form button[type="submit"]',
]
RATE_LIMIT_TITLE = "too many requests"
RATE_LIMIT_SNIPPETS = [
    "making requests too quickly",
    "temporarily limited access to your conversations",
    "please wait a few minutes before trying again",
]
RATE_LIMIT_REQUIRED_SNIPPETS = 2
# Full error strings ChatGPT renders *as the assistant message*. Matched against the whole
# trimmed response, never as a substring: a legitimate answer can discuss these verbatim.
CHATGPT_ERROR_RESPONSES = [
    "something went wrong. if this issue persists please contact us through our help center at help.openai.com",
    "something went wrong while generating the response. if this issue persists please contact us through our help center at help.openai.com",
    "there was an error generating a response",
    "an error occurred while processing your request",
    "our systems have detected unusual activity",
    "the message you submitted was too long",
]
CHATGPT_ERROR_MAX_CHARS = 400


class ChatGPTRateLimitError(RuntimeError):
    pass


class ChatGPTResponseError(RuntimeError):
    """ChatGPT answered with one of its own error messages instead of a real response."""
    pass


def _debug_path(name: str) -> str:
    os.makedirs(DEBUG_DIR, exist_ok=True)
    return os.path.join(DEBUG_DIR, name)


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


async def _get_rate_limit_message(page) -> str | None:
    try:
        return await page.evaluate(
            '''
            ({ title, snippets, requiredSnippets }) => {
                const bodyText = document.body?.innerText || '';
                const normalized = bodyText.toLowerCase();
                const snippetHits = snippets.filter(snippet => normalized.includes(snippet)).length;
                if (!normalized.includes(title) && snippetHits < requiredSnippets) return null;

                const excludeSel = '[data-message-author-role], #prompt-textarea, [data-testid="prompt-textarea"]';
                const candidates = [
                    ...document.querySelectorAll('[role="dialog"], [aria-modal="true"], div')
                ].filter(el => !el.closest(excludeSel) && !el.querySelector(excludeSel))
                 .map(el => (el.innerText || '').trim())
                 .filter(text => {
                    const lower = text.toLowerCase();
                    const localHits = snippets.filter(snippet => lower.includes(snippet)).length;
                    return lower.includes(title) || localHits >= requiredSnippets;
                 })
                 .sort((a, b) => a.length - b.length);

                return candidates.length ? candidates[0].replace(/\\s+/g, ' ') : null;
            }
            ''',
            {
                "title": RATE_LIMIT_TITLE,
                "snippets": RATE_LIMIT_SNIPPETS,
                "requiredSnippets": RATE_LIMIT_REQUIRED_SNIPPETS,
            },
        )
    except Exception:
        return None


async def _dismiss_rate_limit_if_present(page, debug_name: str) -> str | None:
    message = await _get_rate_limit_message(page)
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
        raise ChatGPTRateLimitError(f"ChatGPT rate limit detected, but the dialog could not be dismissed: {message}. Screenshot: {shot_path}")

    await page.wait_for_timeout(1000)
    if await _get_rate_limit_message(page):
        raise ChatGPTRateLimitError(f"ChatGPT rate limit dialog persisted after dismissal: {message}. Screenshot: {shot_path}")

    return message


async def _find_prompt_locator(page, timeout_ms: int = 30000):
    deadline = timeout_ms
    while deadline > 0:
        await _dismiss_rate_limit_if_present(page, "debug_rate_limit_prompt.png")
        for selector in PROMPT_SELECTORS:
            locator = page.locator(selector).first
            try:
                if await locator.count() and await locator.is_visible():
                    return locator
            except Exception:
                continue
        await page.wait_for_timeout(500)
        deadline -= 500
    raise TimeoutError(f"Failed to find visible ChatGPT prompt input using selectors: {', '.join(PROMPT_SELECTORS)}")


async def _composer_text(page) -> str:
    for selector in PROMPT_SELECTORS:
        locator = page.locator(selector).first
        try:
            if await locator.count() and await locator.is_visible():
                text = await locator.evaluate("el => el.value ?? el.innerText ?? ''")
                return (text or "").strip()
        except Exception:
            continue
    return ""


async def _wait_for_composer_clear(page, timeout_ms: int = 4000) -> bool:
    waited = 0
    while waited < timeout_ms:
        if not await _composer_text(page):
            return True
        await page.wait_for_timeout(250)
        waited += 250
    return not await _composer_text(page)


async def _click_send_button(page) -> bool:
    for selector in SEND_BUTTON_SELECTORS:
        locator = page.locator(selector).first
        try:
            if not await locator.count() or not await locator.is_visible():
                continue
            if await locator.is_disabled():
                continue
            await locator.click(timeout=5000)
            return True
        except Exception:
            continue
    return False


async def _submit_prompt(page, prompt) -> bool:
    """Submit the composer. Returns True only once the text has actually left it.

    Enter alone is not reliable: chatgpt.com's composer has shipped states where Enter
    inserts a newline instead of submitting, which silently strands the prompt and shows
    up 30s later as a stream timeout. Click the real submit button first, and in both
    paths confirm the composer emptied rather than assuming the keystroke landed.
    """
    if await _click_send_button(page) and await _wait_for_composer_clear(page):
        return True
    try:
        await prompt.press("Enter", timeout=5000)
    except Exception:
        return False
    return await _wait_for_composer_clear(page)


class ChatGPTBrowser:
    def __init__(self, headless=False):
        self.headless = headless
        self.playwright = None
        self.context = None
        self._closed = False

    async def start(self):
        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.context.on("close", self._on_context_close)
        self._closed = False

    def _on_context_close(self, *_args):
        self._closed = True

    @property
    def is_alive(self) -> bool:
        return self.context is not None and not self._closed

    async def new_session(self):
        if not self.is_alive:
            raise RuntimeError("Browser context is not alive; call start() first.")
        try:
            page = await self.context.new_page()
        except Exception as e:
            self._closed = True
            raise RuntimeError(f"Failed to open new page (browser likely disconnected): {e}")

        await page.goto("https://chatgpt.com/?model=auto", wait_until="domcontentloaded", timeout=60000)
        try:
            await _find_prompt_locator(page, timeout_ms=30000)
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
            raise Exception(f"Failed to find chat input on new session. Screenshot: {shot_path}. Error: {e}")
        return ChatGPTSession(page)

    async def close(self):
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
    def __init__(self, page):
        self.page = page

    async def close(self):
        try:
            await self.page.close()
        except Exception:
            pass

    async def _send_prompt(self, message: str):
        last_error = None
        for _ in range(3):
            prompt = await _find_prompt_locator(self.page, timeout_ms=30000)
            try:
                await prompt.fill(message, timeout=5000)
            except Exception as e:
                last_error = e
                continue
            if not await _submit_prompt(self.page, prompt):
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
            return await _dismiss_rate_limit_if_present(self.page, "debug_rate_limit_send.png")
        raise Exception(f"Failed to send the ChatGPT prompt after 3 attempts: {last_error}")

    async def stream_message(self, message: str, raw_output: bool = False, timeout_ms: int = 480000):
        elements_before = await self.page.query_selector_all('div[data-message-author-role="assistant"]')
        count_before = len(elements_before)

        rate_limit_message = await self._send_prompt(message)

        wait_time = 0
        while wait_time < 30000:
            rate_limit_message = await _dismiss_rate_limit_if_present(self.page, "debug_rate_limit_text_wait.png") or rate_limit_message
            current_count = len(await self.page.query_selector_all('div[data-message-author-role="assistant"]'))
            if current_count > count_before:
                break
            await self.page.wait_for_timeout(500)
            wait_time += 500

        if wait_time >= 30000:
            shot_path = _debug_path("debug_send.png")
            try:
                await self.page.screenshot(path=shot_path)
            except Exception:
                pass
            if rate_limit_message:
                raise ChatGPTRateLimitError(f"ChatGPT rate limit dialog was dismissed, but no assistant response appeared: {rate_limit_message}. Screenshot: {shot_path}")
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
            rate_limit_message = await _dismiss_rate_limit_if_present(self.page, "debug_rate_limit_text_stream.png") or rate_limit_message
            elements = await self.page.query_selector_all('div[data-message-author-role="assistant"]')
            if not elements:
                continue

            current_text = await elements[-1].inner_text()

            if current_text == last_text and current_text.strip() != "":
                 stable_count += 1
            else:
                 stable_count = 0

            last_text = current_text

            if stable_count >= 5:
                class_attr = await elements[-1].get_attribute("class")
                is_streaming = "result-streaming" in (class_attr or "")

                stop_btn = await self.page.query_selector('[data-testid="stop-button"]')
                aria_stop = await self.page.query_selector('[aria-label="Stop generating"]')

                stop_visible = await stop_btn.is_visible() if stop_btn else False
                aria_visible = await aria_stop.is_visible() if aria_stop else False

                if not is_streaming and not stop_visible and not aria_visible:
                    break
                else:
                    stable_count = 3

        elements = await self.page.query_selector_all('div[data-message-author-role="assistant"]')
        if not elements:
            raise Exception("Could not locate the response in the DOM.")

        last_response = elements[-1]

        chatgpt_error = _detect_chatgpt_error(await last_response.inner_text())
        if chatgpt_error:
            raise ChatGPTResponseError(chatgpt_error)

        if raw_output:
            raw_payload = await last_response.evaluate('''
                (el) => {
                    const markdownEl = el.querySelector('.markdown') || el;
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
            if rate_limit_message and not final_raw:
                raise ChatGPTRateLimitError(f"ChatGPT rate limit dialog was dismissed, but the assistant response was empty: {rate_limit_message}")

            yield {"type": "final", "content": final_raw, "sources": []}
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

                let markdownEl = cloned.querySelector('.markdown');
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
        elements_before = await self.page.query_selector_all('div[data-message-author-role="assistant"]')
        count_before = len(elements_before)

        rate_limit_message = await self._send_prompt(message)

        appeared = False
        appear_wait = 0
        while appear_wait < 30000:
            rate_limit_message = await _dismiss_rate_limit_if_present(self.page, "debug_rate_limit_image_wait.png") or rate_limit_message
            current_count = len(await self.page.query_selector_all('div[data-message-author-role="assistant"]'))
            if current_count > count_before:
                appeared = True
                break
            await self.page.wait_for_timeout(500)
            appear_wait += 500
        if not appeared:
            shot_path = _debug_path("debug_image_send.png")
            await self.page.screenshot(path=shot_path)
            if rate_limit_message:
                raise ChatGPTRateLimitError(f"ChatGPT rate limit dialog was dismissed, but no image-gen response appeared: {rate_limit_message}. Screenshot: {shot_path}")
            raise Exception(f"Timeout waiting for assistant message to appear after image-gen prompt. Screenshot: {shot_path}")

        completed = False
        stable_after_done = 0
        last_url_count = 0
        elapsed = 0
        while elapsed < timeout_ms:
            await self.page.wait_for_timeout(2000)
            elapsed += 2000
            rate_limit_message = await _dismiss_rate_limit_if_present(self.page, "debug_rate_limit_image_stream.png") or rate_limit_message

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
                    stable_after_done += 1
                    if stable_after_done >= 2:
                        completed = True
                        break
                last_url_count = len(urls)
            else:
                stable_after_done = 0
                last_url_count = len(urls)

        if not completed:
            shot_path = _debug_path("debug_image_timeout.png")
            await self.page.screenshot(path=shot_path)
            if rate_limit_message:
                raise ChatGPTRateLimitError(f"ChatGPT rate limit dialog was dismissed, but image generation did not complete: {rate_limit_message}. Screenshot: {shot_path}")
            raise Exception(f"Image generation timed out after {timeout_ms}ms. Screenshot: {shot_path}")

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
                const lastMsg = [...document.querySelectorAll('div[data-message-author-role="assistant"]')].pop();
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
