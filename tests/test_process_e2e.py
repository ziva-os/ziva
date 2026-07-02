import socket
import time
import urllib.request
from multiprocessing import Process


def _serve_desktop_process(port: int) -> None:
    import asyncio
    from pathlib import Path

    from ziva.runtime import Runtime
    from ziva.transports.desktop_api.server import DesktopAPIServer
    from ziva.shared_types import ChatResult

    class FakeAdapter:
        async def chat(self, messages, model, system_prompt=None, tools=None):
            return ChatResult(role="assistant", content="ok", model=model, usage={}, finish_reason="stop")

    root = Path("/Users/wangxinxin/code/ziva")
    rt = Runtime.create(workspace_root=root)
    asyncio.run(DesktopAPIServer(rt).run_async(host="127.0.0.1", port=port))


def _wait_port(host: str, port: int, timeout: float = 5.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.1)
    return False


def test_desktop_server_process_e2e():
    port = 4197
    proc = Process(target=_serve_desktop_process, args=(port,), daemon=True)
    proc.start()
    try:
        assert _wait_port("127.0.0.1", port, timeout=8.0)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/sessions", data=b"", timeout=2) as _:
            pass
    finally:
        proc.terminate()
        proc.join(timeout=3)
    assert not proc.is_alive()
