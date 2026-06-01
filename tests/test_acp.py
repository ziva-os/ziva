import asyncio

from ziva_runtime.protocols.acp import ACPServer
from ziva_runtime.runtime import Runtime
from ziva_runtime.shared_types import ChatResult


class FakeAdapter:
    async def chat(self, messages, model, system_prompt=None, tools=None):
        user_messages = [m.content for m in messages if not m.content.startswith("Environment context:")]
        txt = " | ".join(user_messages)
        return ChatResult(role="assistant", content=f"ok:{txt}", model=model, usage={"prompt_tokens": 1, "completion_tokens": 1}, finish_reason="stop")


def test_acp_chat(tmp_path):
    async def _run():
        runtime = Runtime.create(workspace_root=tmp_path, model_adapter=FakeAdapter())
        server = ACPServer(runtime)

        init_rsp = await server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert init_rsp["result"]["capabilities"]["chat"] is True

        chat_rsp = await server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "chat",
                "params": {"messages": [{"role": "user", "content": "hello"}]},
            }
        )
        assert chat_rsp["result"]["message"]["content"] == "ok:hello"

        bad_rsp = await server.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "chat",
                "params": {"messages": []},
            }
        )
        assert bad_rsp["error"]["data"]["classification"] == "invalid_params"

    asyncio.run(_run())
