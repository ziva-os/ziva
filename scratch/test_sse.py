import asyncio
import aiohttp
import json
import time
import sys

async def main():
    async with aiohttp.ClientSession() as session:
        # Create session
        async with session.post('http://localhost:8080/sessions') as resp:
            data = await resp.json()
            sid = data["id"]
            print("Session ID:", sid)
        
        # Connect to SSE
        async def listen_sse():
            async with session.get('http://localhost:8080/events') as resp:
                print("SSE Connected")
                async for line in resp.content:
                    line_text = line.decode('utf-8').strip()
                    if line_text.startswith("data: "):
                        print("SSE Event:", line_text[6:100], "...")
        
        sse_task = asyncio.create_task(listen_sse())
        await asyncio.sleep(1) # wait for sse
        
        # Send message
        payload = {"messages": [{"role": "user", "content": "你好"}]}
        async with session.post(f'http://localhost:8080/sessions/{sid}/turns', json=payload) as resp:
            print("Send message status:", resp.status)
        
        await asyncio.sleep(20)
        sse_task.cancel()

asyncio.run(main())
