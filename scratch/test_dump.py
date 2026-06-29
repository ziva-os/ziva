import json
import uuid

event = {"type": "turn_start", "session_id": str(uuid.uuid4())}
payload = {"session_id": event["session_id"], "seq": 1, "ts": 12345}
payload.update(event)
print(json.dumps(payload, ensure_ascii=False, default=str))
