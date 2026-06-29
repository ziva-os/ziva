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
        await page.wait_for_timeout(1000)
        
        # Click new session
        await page.click("button.new-chat-btn")
        await page.wait_for_timeout(500)
        
        # Type message
        await page.fill("#prompt", "Hello test")
        await page.click("#btnSend")
        await page.wait_for_timeout(1000)
        
        # Take screenshot to see if message appeared
        await page.screenshot(path="scratch/after_send.png")
        
        # Wait for model response (approx 3 seconds)
        await page.wait_for_timeout(3000)
        await page.screenshot(path="scratch/after_response.png")
        
        print("ERRORS:", errors)
        print("Done")
        await browser.close()

asyncio.run(main())
