import asyncio

import browser as browser_module
from browser import (
    ChatGPTBrowser,
    STOP_GENERATING_SELECTORS,
    _browser_pid_from_ps_output,
    _chatgpt_assistant_turn_count,
    _dismiss_rate_limit_if_present,
    _image_text_is_terminal_without_urls,
    _new_background_page,
)


def test_browser_pid_from_ps_output_uses_main_process_for_profile():
    profile = "/tmp/shared browser profile"
    output = """
      101 /path/Google Chrome for Testing --user-data-dir=/tmp/other
      202 /path/Google Chrome for Testing --user-data-dir=/tmp/shared browser profile about:blank
      203 /path/Helper --type=renderer --user-data-dir=/tmp/shared browser profile
    """

    assert _browser_pid_from_ps_output(output, profile) == 202


class _MissingButton:
    first = None

    def __init__(self):
        self.first = self

    async def count(self):
        return 0

    async def is_visible(self):
        return False

    def filter(self, **kwargs):
        return self


class _GotItButton(_MissingButton):
    async def count(self):
        return 1

    async def is_visible(self):
        return True

    async def click(self):
        self.page.dismissed = True


class _RateLimitPage:
    def __init__(self):
        self.dismissed = False
        self.got_it = _GotItButton()
        self.got_it.page = self

    async def evaluate(self, script, args):
        if self.dismissed:
            return None
        assert "too many requests" in args["titles"]
        assert "getBoundingClientRect" in script
        return "Too many requests. Please wait a few minutes before trying again."

    async def screenshot(self, path):
        return None

    def get_by_role(self, role, name):
        if role == "button" and name == "Got it":
            return self.got_it
        return _MissingButton()

    def locator(self, selector):
        return _MissingButton()

    async def wait_for_timeout(self, milliseconds):
        return None


def test_rate_limit_dialog_clicks_got_it_for_claude():
    page = _RateLimitPage()

    message = asyncio.run(_dismiss_rate_limit_if_present(
        page,
        "rate-limit.png",
        provider="claude",
    ))

    assert message.startswith("Too many requests")
    assert page.dismissed is True


class _AssistantTurnPage:
    async def evaluate(self, script):
        assert "data-message-author-role" in script
        assert "data-turn" in script
        assert "chatgpt said:" in script
        return 2


def test_chatgpt_assistant_turn_count_supports_modern_turn_heading():
    assert asyncio.run(_chatgpt_assistant_turn_count(_AssistantTurnPage())) == 2


def test_chatgpt_stop_selectors_cover_current_stop_answering_control():
    assert '[aria-label="Stop answering"]' in STOP_GENERATING_SELECTORS["chatgpt"]


def test_image_text_without_urls_waits_for_blank_or_generating_turns():
    assert _image_text_is_terminal_without_urls("ChatGPT said:") is False
    assert _image_text_is_terminal_without_urls("Thinking Generating image... hang tight.") is False


def test_image_text_without_urls_accepts_real_terminal_response():
    assert _image_text_is_terminal_without_urls("I can't create that image.") is True


def test_image_render_visibility_is_reference_counted(monkeypatch):
    calls = []
    monkeypatch.setattr(
        browser_module,
        "_set_browser_hidden",
        lambda pid, hidden: calls.append((pid, hidden)) or True,
    )
    bot = ChatGPTBrowser(headless=False, background=True)
    bot._browser_pid = 42

    bot.reveal_for_rendering()
    bot.reveal_for_rendering()
    bot.hide_after_rendering()
    bot.hide_after_rendering()

    assert calls == [(42, False), (42, True)]


class _PageInfo:
    def __init__(self, page):
        self.value = _ReadyValue(page)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _ReadyValue:
    def __init__(self, value):
        self.value = value

    def __await__(self):
        async def ready():
            return self.value

        return ready().__await__()


class _CDPSession:
    def __init__(self):
        self.calls = []
        self.detached = False

    async def send(self, method, params):
        self.calls.append((method, params))

    async def detach(self):
        self.detached = True


class _Browser:
    def __init__(self, cdp):
        self.cdp = cdp

    async def new_browser_cdp_session(self):
        return self.cdp


class _Context:
    def __init__(self, page, cdp):
        self.page = page
        self.browser = _Browser(cdp)

    def expect_page(self, timeout):
        assert timeout == 10000
        return _PageInfo(self.page)


def test_new_background_page_uses_cdp_background_target():
    page = object()
    cdp = _CDPSession()
    context = _Context(page, cdp)

    result = asyncio.run(_new_background_page(context))

    assert result is page
    assert cdp.calls == [("Target.createTarget", {"url": "about:blank", "background": True})]
    assert cdp.detached is True
