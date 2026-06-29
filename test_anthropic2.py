import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath('./src'))

from ziva_runtime.adapters.anthropic.provider import AnthropicChatAdapter
from ziva_runtime.shared_types import ChatMessage

async def main():
    import logging
    logging.basicConfig(level=logging.DEBUG)
    adapter = AnthropicChatAdapter()
    messages = [ChatMessage(role="user", content="Think step by step and say hi.")]
    cfg = {"enabled": True, "reasoning_effort": "low", "budget_tokens": 1024}
    
    stream = adapter.chat_stream(messages, model="claude-3-7-sonnet-20250219", system_prompt="You are a helpful assistant.", thinking_config=cfg)
    
    async for delta in stream:
        print("DELTA:", delta)

asyncio.run(main())
