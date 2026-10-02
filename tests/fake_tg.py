"""In-memory fake of the Telegram Bot API (NO network)."""
import json


class FakeAPI:
    def __init__(self):
        self.calls = []        # (method, payload)
        self.sent = []         # sendMessage payloads
        self.docs = []

    def call(self, method, payload=None, files=None, http_timeout=None):
        payload = payload or {}
        self.calls.append((method, payload))
        if method == "sendMessage":
            self.sent.append(payload)
            return {"message_id": len(self.sent)}
        if method == "sendDocument":
            self.docs.append((payload, files))
            return {"message_id": 1}
        if method == "getUpdates":
            return []
        return True

    def last_text(self):
        return self.sent[-1]["text"] if self.sent else ""

    def all_text(self):
        return "\n".join(p["text"] for p in self.sent)

    def buttons(self, payload=None):
        p = payload or self.sent[-1]
        return [b for row in p["reply_markup"]["inline_keyboard"] for b in row]


def msg(text, uid=111, username="example_owner", chat=None, chat_type="private"):
    return {"update_id": 1, "message": {"message_id": 1, "from": {"id": uid, "username": username},
                                        "chat": {"id": chat or uid, "type": chat_type}, "text": text}}


def cb(data, uid=111, username="example_owner"):
    return {"update_id": 2, "callback_query": {"id": "cb1", "from": {"id": uid, "username": username}, "data": data,
                                                "message": {"message_id": 7, "chat": {"id": uid, "type": "private"}}}}
