import asyncio
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ziva.runtime import Runtime
from ziva.shared_types import ChatMessage, ChatResult

class FakeAdapter:
    async def chat(self, messages, model, system_prompt=None, tools=None):
        content = messages[-1].content if messages else ""
        return ChatResult(role="assistant", content=content, model=model, usage={}, finish_reason="stop")

def test_deny_tool():
    root = Path(__file__).resolve().parents[1]
    rt = Runtime.create(
        workspace_root=root,

        session_override={"tool": {"deny": ["write_file"], "max_rounds": 1}},
    )
    result = asyncio.run(rt.chat([ChatMessage(role="user", content="test")], session_id="s1"))
    # write_file should be denied, but the test just verifies deny list is loaded
    assert rt.config["tool"]["deny"] == ["write_file"]

def test_approval_config_defaults():
    root = Path(__file__).resolve().parents[1]
    rt = Runtime.create(workspace_root=root)
    assert rt.config["approval"]["policy"] == "suggest"
    assert rt.config["sandbox"]["mode"] == "off"
