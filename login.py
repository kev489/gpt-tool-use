import asyncio
from playwright.async_api import async_playwright

from browser import USER_DATA_DIR


async def login():
    pw = await async_playwright().start()
    browser = await pw.chromium.launch_persistent_context(
        user_data_dir=USER_DATA_DIR,
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = browser.pages[0] if browser.pages else await browser.new_page()
    await page.goto("https://chatgpt.com")
    print(f"Profile dir: {USER_DATA_DIR}")
    print("Sign in to ChatGPT (or Claude) in the browser window.")
    print("The browser will stay open for 300 seconds — close it when you're done.")
    try:
        await page.wait_for_event("close", timeout=300000)
    except Exception:
        pass
    await browser.close()
    await pw.stop()


def main():
    asyncio.run(login())


if __name__ == "__main__":
    main()
