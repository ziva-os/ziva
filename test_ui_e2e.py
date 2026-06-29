import asyncio
import time
from playwright.async_api import async_playwright

async def main():
    print("Starting Playwright browser...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        url = "http://127.0.0.1:4097"
        print(f"Navigating to {url} ...")
        
        # Wait up to 10 seconds for the server to be ready
        for _ in range(10):
            try:
                await page.goto(url, wait_until="domcontentloaded")
                break
            except Exception as e:
                print("Waiting for server to come up...")
                time.sleep(1)
        else:
            print("Server did not come up!")
            return

        print("Page loaded successfully.")
        
        # Give JS a moment to initialize
        await page.wait_for_timeout(1000)

        print("Testing Thinking Mode in Settings...")
        # Check settings
        await page.click("#btnSettings")
        await page.wait_for_selector("#s_thinking_mode", state="visible", timeout=3000)
        
        mode_visible = await page.is_visible("#s_thinking_mode")
        budget_visible = await page.is_visible("#s_thinking_budget")
        print(f"Thinking Mode dropdown visible: {mode_visible}")
        print(f"Thinking Budget tokens input visible: {budget_visible}")

        # Ensure we can select a thinking mode
        if mode_visible:
            await page.select_option("#s_thinking_mode", "high")
            print("Successfully selected 'high' thinking mode.")

        print("Closing Settings Modal...")
        await page.click("#settingsSaveBtn")
        
        # Small wait for modal animation
        await page.wait_for_timeout(500)

        print("Testing Scheduled Tasks (HH:MM:SS) input...")
        # Open scheduled tasks modal
        await page.click("#btnScheduled")
        await page.wait_for_selector("#automationTimeInput", state="visible", timeout=3000)
        
        time_visible = await page.is_visible("#automationTimeInput")
        print(f"Time input (HH:MM:SS) visible: {time_visible}")

        if time_visible:
            await page.fill("#automationTimeInput", "09:15:30")
            print("Successfully filled '09:15:30' in HH:MM:SS format.")
            
        print("Closing Scheduled Tasks Modal...")
        await page.keyboard.press("Escape")

        await browser.close()
        print("End-to-End Test Completed Successfully!")

if __name__ == "__main__":
    asyncio.run(main())
