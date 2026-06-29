import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        
        errors = []
        page.on("pageerror", lambda err: errors.append(err.message))
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        
        await page.goto("http://localhost:4097/")
        await page.wait_for_timeout(2000)
        
        for err in errors:
            print("ERROR:", err)
            
        print("Done")
        await browser.close()

asyncio.run(main())
