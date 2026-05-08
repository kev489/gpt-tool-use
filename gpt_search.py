import sys
import asyncio
from browser import ChatGPTBrowser

async def main():
    query = sys.argv[1]
    bot = ChatGPTBrowser()
    await bot.start()
    try:
        session = await bot.new_session()
        try:
            async for chunk in session.stream_message(query):
                if chunk["type"] == "final":
                    print(chunk["content"])
                    if chunk["sources"]:
                        print("\n--- Sources ---")
                        for i, src in enumerate(chunk["sources"], 1):
                            print(f"[{i}] {src}")
        finally:
            await session.close()
    finally:
        await bot.close()

if __name__ == "__main__":
    asyncio.run(main())
