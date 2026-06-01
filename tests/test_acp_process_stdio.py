import json
import os
import subprocess


def test_acp_stdio_process_roundtrip():
    root = "/Users/wangxinxin/code/ziva"
    env = dict(os.environ)
    env["PYTHONPATH"] = "src"

    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "unknown", "params": {}},
    ]
    payload = "\n".join(json.dumps(r) for r in reqs) + "\n"

    proc = subprocess.run(
        ["python", "-m", "ziva_runtime.app.cli", "acp", "serve", "--workspace", "."],
        cwd=root,
        env=env,
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) >= 3

    init_rsp = json.loads(lines[0])
    ping_rsp = json.loads(lines[1])
    unknown_rsp = json.loads(lines[2])

    assert init_rsp["result"]["name"] == "ziva-acp"
    assert ping_rsp["result"]["pong"] is True
    assert unknown_rsp["error"]["data"]["classification"] == "method_not_found"
