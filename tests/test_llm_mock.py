"""Exercise the OpenAI-compatible (LM Studio / Ollama) engine against a mock server.

Mock behaviour: rejects response_format=json_schema (like some local servers), accepts
json_object, wraps its JSON in a <think> block + code fence, and omits one id so the
Google fill-in path runs too.
"""
import asyncio
import http.server
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pcm.config import Settings
from pcm.model import TextBlock
from pcm.translate import Translator

SEEN = []


class Mock(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            self._send(200, {"data": [{"id": "text-embedding-nomic"}, {"id": "gemma-3-12b-it"}]})
        else:
            self._send(404, {})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append(req)
        rf = (req.get("response_format") or {}).get("type")
        if rf == "json_schema":
            return self._send(400, {"error": "response_format json_schema is not supported"})
        page = json.loads(req["messages"][1]["content"])
        tr = [{"id": b["id"], "text": "[AI] แปลช่อง " + b["id"]} for b in page["bubbles"][:-1]]
        content = "<think>planning…</think>```json\n" + json.dumps(
            {"translations": tr, "new_terms": [{"source": "タロウ", "target": "ทาโร่"}]}, ensure_ascii=False) + "\n```"
        self._send(200, {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]})


async def main():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8766), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    s = Settings()
    s.engine = "local"
    s.local_base_url = "http://127.0.0.1:8766/v1"
    s.local_model = ""
    s.glossary = "ハナ = ฮานะ"
    tr = Translator(s)
    blocks = [TextBlock(str(i), t, [], (0, 0, 1, 1)) for i, t in enumerate(["おい、待てよ！", "タロウはどこ？", "行くぞ！"], 1)]
    used = await tr.translate_page(blocks, "ja", "ja", "th")
    print("engine used:", used, "| json mode now:", tr.engine.json_mode, "| model:", tr.engine.model)
    for b in blocks:
        print(f"  {b.id}: {b.text} -> {b.translation}")
    print("requests:", [(r.get("model"), (r.get("response_format") or {}).get("type")) for r in SEEN])
    sent = json.loads(SEEN[-1]["messages"][1]["content"])
    print("glossary sent:", sent["glossary"])
    print("learned terms:", tr.memory.terms)
    # second page: previous lines must be included as context
    blocks2 = [TextBlock("1", "ありがとう", [], (0, 0, 1, 1)), TextBlock("2", "またね", [], (0, 0, 1, 1))]
    await tr.translate_page(blocks2, "ja", "ja", "th")
    sent2 = json.loads(SEEN[-1]["messages"][1]["content"])
    print("context lines on page 2:", len(sent2["previous_lines"]), "| glossary:", sent2["glossary"])
    await tr.aclose()
    srv.shutdown()
    assert blocks[0].translation.startswith("[AI]") and blocks[2].translation and not blocks[2].translation.startswith("[AI]")
    assert used == "local" and tr.engine.json_mode == "json_object" and tr.engine.model == "gemma-3-12b-it"
    assert any(t["source"] == "タロウ" for t in sent2["glossary"])
    print("OK")


if __name__ == "__main__":
    asyncio.run(main())



