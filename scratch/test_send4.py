import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        
        await page.goto("http://localhost:4097/")
        await page.wait_for_timeout(2000)
        
        await page.evaluate('''() => {
            const el = document.getElementById("prompt");
            if (el) {
                el.value = "Say hello!";
                const btn = document.getElementById("btnSend");
                if (btn) btn.click();
            }
        }''')
        
        await page.wait_for_timeout(20000)
        
        html = await page.evaluate('() => document.getElementById("messages").innerText')
        print("DOM TEXT:")
        print(html)
        
        await browser.close()

asyncio.run(main())
