import os
import asyncio
from playwright.async_api import async_playwright

USER_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chatgpt_profile")
DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug")


def _debug_path(name: str) -> str:
    os.makedirs(DEBUG_DIR, exist_ok=True)
    return os.path.join(DEBUG_DIR, name)


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

        await page.goto("https://chatgpt.com/?model=auto")
        try:
            await page.wait_for_selector('#prompt-textarea', timeout=15000)
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

    async def stream_message(self, message: str):
        elements_before = await self.page.query_selector_all('div[data-message-author-role="assistant"]')
        count_before = len(elements_before)

        await self.page.fill('#prompt-textarea', message)
        await self.page.press('#prompt-textarea', 'Enter')

        try:
            wait_time = 0
            while wait_time < 30000:
                current_count = len(await self.page.query_selector_all('div[data-message-author-role="assistant"]'))
                if current_count > count_before:
                    break
                await self.page.wait_for_timeout(500)
                wait_time += 500

            if wait_time >= 30000:
                raise Exception("Timeout waiting for assistant message to appear.")
        except Exception as e:
            await self.page.screenshot(path=_debug_path("debug_send.png"))
            yield {"type": "final", "content": f"Error: Message didn't send or respond. Details: {e}", "sources": []}
            return

        last_text = ""
        stable_count = 0
        while True:
            await self.page.wait_for_timeout(500)
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
            yield {"type": "final", "content": "Error: Could not locate the response in the DOM.", "sources": []}
            return

        last_response = elements[-1]

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

        yield {"type": "final", "content": final_markdown, "sources": html_payload["sources"]}

    async def stream_image_message(self, message: str, timeout_ms: int = 180000):
        elements_before = await self.page.query_selector_all('div[data-message-author-role="assistant"]')
        count_before = len(elements_before)

        await self.page.fill('#prompt-textarea', message)
        await self.page.press('#prompt-textarea', 'Enter')

        appeared = False
        appear_wait = 0
        while appear_wait < 30000:
            current_count = len(await self.page.query_selector_all('div[data-message-author-role="assistant"]'))
            if current_count > count_before:
                appeared = True
                break
            await self.page.wait_for_timeout(500)
            appear_wait += 500
        if not appeared:
            shot_path = _debug_path("debug_image_send.png")
            await self.page.screenshot(path=shot_path)
            raise Exception(f"Timeout waiting for assistant message to appear after image-gen prompt. Screenshot: {shot_path}")

        completed = False
        stable_after_done = 0
        last_url_count = 0
        elapsed = 0
        while elapsed < timeout_ms:
            await self.page.wait_for_timeout(2000)
            elapsed += 2000

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
