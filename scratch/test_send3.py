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
        
        # Type message
        await page.evaluate('''() => {
            const el = document.getElementById("prompt");
            if (el) {
                el.value = "Hello Playwright!";
                const btn = document.getElementById("btnSend");
                if (btn) btn.click();
            }
        }''')
        
        await page.wait_for_timeout(1000)
        await page.screenshot(path="scratch/after_send.png")
        
        # Wait for model response (approx 4 seconds)
        await page.wait_for_timeout(4000)
        await page.screenshot(path="scratch/after_response.png")
        
        print("ERRORS:", errors)
        print("Done")
        await browser.close()

asyncio.run(main())
