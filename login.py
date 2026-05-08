import os
import asyncio
from playwright.async_api import async_playwright

USER_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chatgpt_profile")


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
    print("Sign in to ChatGPT in the browser window.")
    print("The browser will stay open for 120 seconds — close it when you're done.")
    try:
        await page.wait_for_event("close", timeout=120000)
    except Exception:
        pass
    await browser.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(login())
