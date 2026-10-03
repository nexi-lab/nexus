"""Test-side acp-mailbox/1 framing, shared by daemon integration harnesses."""

import json


class SessionMailboxCodec:
    def __init__(self, endpoint):
        assert endpoint["protocol"] == "acp-mailbox/1"
        self.endpoint = endpoint
        self.sent = 0
        self.received = 0

    def encode(self, message):
        self.sent += 1
        frame = {
            "protocol": "acp-mailbox/1",
            "channel_id": self.endpoint["channel_id"],
            "sequence": self.sent,
            "type": "rpc",
            "message": message,
        }
        return json.dumps(
            {
                "from": self.endpoint["controller"],
                "to": self.endpoint["agent"],
                "kind": "session",
                "body": json.dumps(frame),
            }
        ).encode()

    def decode(self, envelope):
        if envelope.get("kind") != "session" or envelope.get("from") == self.endpoint["controller"]:
            return None
        frame = json.loads(envelope["body"])
        if frame["channel_id"] != self.endpoint["channel_id"]:
            return None
        assert envelope["from"] == self.endpoint["agent"]
        assert envelope["to"] == self.endpoint["controller"]
        assert frame["protocol"] == "acp-mailbox/1"
        if frame["sequence"] <= self.received:
            return None
        assert frame["sequence"] == self.received + 1
        self.received = frame["sequence"]
        return frame
