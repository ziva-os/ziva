import asyncio
from ziva_runtime.adapters.anthropic.provider import AnthropicChatAdapter
from ziva_runtime.shared_types import ChatMessage
import json

async def main():
    with open(".ziva/config.yaml") as f:
        # Load the config manually
        pass
        
    adapter = AnthropicChatAdapter()
    
    msgs = [ChatMessage(role="user", content="你好")]
    
    async for chunk in adapter.chat_stream("kimi-k2.6", msgs, "You are helpful", None, None):
        print("Delta:", chunk.content, chunk.usage)

asyncio.run(main())
