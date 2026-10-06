"""Exercise the OpenAI-compatible (LM Studio / Ollama) engine against a mock server.

1. Server rejects response_format=json_schema (like some local servers), accepts
   json_object, wraps the OLD list-format JSON in a <think> block + code fence, and omits
   one id so the Google fill-in path runs too.
2. Keyed format: a per-page json_schema keyed by bubble id; the model is probed only once.
3. Blank lines: story text left empty is asked again (only those ids); Google only if that
   request fails. A non-empty answer ("…!"), watermarks, URLs, lone OCR-noise letters and
   bubbles left empty twice stay as they are.
4. Downgrade chain json_schema -> json_object -> none plus temperature/reasoning fallbacks.
5. Server restart with another model: the engine re-probes once and carries on.
7. block.via names who wrote each bubble; blocks.holes marks pages a story bubble is missing from
   (not text Google's auto-detect gives back unchanged, e.g. "你好" from Simplified to Traditional Chinese).
8. The reading-order hint follows the same rule as ocr._reading_order (vertical Chinese = right-to-left).
9. A model swapped in LM Studio is followed (field left blank) without a per-page /models probe: after a
   pause, every RECHECK seconds (also when the old model was JIT-loaded back), pages waiting for a look.
10. max_tokens: scaled to the page and its text, much larger when thinking, dropped if the server rejects it.
"""
import asyncio
import hashlib
import http.server
import json
import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pcm.config import Settings
from pcm.model import TextBlock
from pcm.ocr import OCRResult, _reading_order
from pcm.translate import (ORDER_LTR, ORDER_RTL, OUTPUT_KEYED, OUTPUT_LIST, PROMPT_TAG, RETRY_NOTE, SYSTEM_PROMPT,
                           THAI_REGISTER, TranslateError, Translator, _collect, blank_story, lone_noise,
                           parse_json_loose, reading_order)

SEEN = []      # POST bodies
GETS = []      # GET /v1/models probes
V0 = []        # GET /api/v0/models (LM Studio: which models are loaded)
CFG = {}


def tag(who, text):   # stands in for a translation: tied to its source line, no CJK left in it
    return f"[{who}] " + hashlib.md5(text.encode()).hexdigest()[:8]


def legacy_reply(page, req):
    tr = [{"id": b["id"], "text": "[AI] แปลช่อง " + b["id"]} for b in page["bubbles"][:-1]]
    return "<think>planning…</think>```json\n" + json.dumps(
        {"translations": tr, "new_terms": [{"source": "タロウ", "target": "ทาโร่"}]}, ensure_ascii=False) + "\n```"


def keyed_reply(page, req):
    retry = "note" in page   # second try for bubbles left empty
    blanks = CFG.get("retry_blanks" if retry else "blanks", {})
    t = {b["id"]: blanks.get(b["text"], tag("AI2" if retry else "AI", b["text"])) for b in page["bubbles"]
         if b["text"] not in CFG.get("drop", ())}
    t.update({b["id"]: b["text"] for b in page["bubbles"] if b["text"] in CFG.get("echo", ())})
    return json.dumps({"translations": t, "new_terms": []}, ensure_ascii=False, separators=(",", ":"))


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
        if self.path == "/v1/models":
            GETS.append(self.path)
            self._send(200, {"data": [{"id": m} for m in CFG["models"]]})
        elif self.path == "/api/v0/models":
            V0.append(self.path)
            time.sleep(CFG.get("v0_delay", 0))
            if "loaded" not in CFG:    # like Ollama: no such endpoint
                return self._send(404, {})
            self._send(200, {"data": [{"id": m, "type": "embeddings" if "embed" in m else "llm",
                                       "state": "loaded" if m in CFG["loaded"] else "not-loaded"}
                                      for m in CFG["models"]]})
        else:
            self._send(404, {})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append(req)
        if req.get("model") not in CFG["models"]:
            return self._send(404, {"error": f"model '{req.get('model')}' not found"})
        rf = (req.get("response_format") or {}).get("type")
        reject = CFG.get("reject", ())
        if "temperature" in reject and "temperature" in req:
            return self._send(400, {"error": "Unsupported parameter: 'temperature'"})
        if "max_tokens" in reject and "max_tokens" in req:   # LM Studio's wording mentions "schema" too
            return self._send(400, {"error": "Field with key llm.prediction.maxPredictedTokens does not satisfy the schema"})
        if "reasoning_effort" in reject and "reasoning_effort" in req:
            return self._send(400, {"error": "Unrecognized request argument supplied: reasoning_effort"})
        if rf == "json_schema" and "json_schema" in reject:
            return self._send(400, {"error": "response_format json_schema is not supported"})
        if rf == "json_object" and "json_object" in reject:
            return self._send(400, {"error": "'response_format.type' must be 'json_schema' or 'text'"})
        if CFG.get("down"):
            return self._send(500, {"error": "internal error"})
        page = json.loads(req["messages"][1]["content"])
        if "note" in page and CFG.get("retry_fail"):
            return self._send(500, {"error": "internal error"})
        msg = {"role": "assistant", "content": CFG["reply"](page, req), "reasoning_content": ""}
        finish = CFG.get("finish", "stop")
        if CFG.get("thinks"):   # thinks this many tokens whatever reasoning_effort says
            msg["reasoning_content"] = "Let me think…"
            if req.get("max_tokens", 1 << 30) < CFG["thinks"]:
                msg["content"], finish = "", "length"
        if CFG.get("per_char"):  # output tokens per source character (Gemma into Thai: ~1.1 for Chinese)
            need = CFG["per_char"] * sum(len(b["text"]) for b in page["bubbles"]) + 12 * len(page["bubbles"])
            if req.get("max_tokens", 1 << 30) < need:
                msg["content"], finish = msg["content"][:len(msg["content"]) // 2], "length"
        self._send(200, {"model": CFG.get("serve_as") or req["model"],
                         "choices": [{"message": msg, "finish_reason": finish}]})


def setup(thinking=False, model="", **cfg):
    SEEN.clear()
    GETS.clear()
    V0.clear()
    CFG.clear()
    CFG.update({"models": ["text-embedding-nomic", "gemma-3-12b-it"], "reply": keyed_reply}, **cfg)
    s = Settings()
    s.engine = "local"
    s.local_base_url = "http://127.0.0.1:8766/v1"
    s.local_model = model
    s.local_thinking = thinking
    s.glossary = "ハナ = ฮานะ"
    return Translator(s)


def blocks_of(texts, vertical=False):
    """A page as LensOCR.read returns it (an OCRResult, so translate_page can set .holes)."""
    return OCRResult(TextBlock(str(i), t, [], (0, 0, 1, 1), vertical=vertical) for i, t in enumerate(texts, 1))


def fake_google(calls, fail=False, echo=False):
    async def translate_texts(texts, src, tgt):
        calls.extend(texts)
        if fail:
            raise TranslateError("Google Translate: HTTP 429")
        return list(texts) if echo else [tag("G", t) for t in texts]
    return translate_texts


class Logs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []
        logging.getLogger("poomcatomanga.translate").addHandler(self)
        logging.getLogger("poomcatomanga.translate").setLevel(logging.INFO)

    def emit(self, record):
        self.lines.append(record.getMessage())

    def count(self, text):
        return sum(text in m for m in self.lines)

    def close(self):
        logging.getLogger("poomcatomanga.translate").removeHandler(self)
        super().close()


async def legacy_list_format():
    print("-- 1. old list format, json_schema rejected")
    tr = setup(reject=("json_schema",), reply=legacy_reply)
    blocks = blocks_of(["おい、待てよ！", "タロウはどこ？", "行くぞ！"])
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
    assert blocks[0].translation.startswith("[AI]") and blocks[2].translation and not blocks[2].translation.startswith("[AI]")
    assert used == "local" and tr.engine.json_mode == "json_object" and tr.engine.model == "gemma-3-12b-it"
    assert any(t["source"] == "タロウ" for t in sent2["glossary"])


async def keyed_format():
    print("-- 2. keyed json_schema, model probed once")
    tr = setup()
    pages = [blocks_of(["你到底想怎样啊!!", "那就拜托你了哦", "等一下"], vertical=True),
             blocks_of(["真的可以吗?", "嗯…"]),
             blocks_of(["先把手举起来", "不痛吗？没事吧？", "我、我可以自己来!", "好闷啊…"])]
    for blocks in pages:
        used = await tr.translate_page(blocks, "zh", "zh", "th")
        assert used == "local", used
        req = SEEN[-1]
        rf = req["response_format"]
        schema = rf["json_schema"]["schema"]
        tprops = schema["properties"]["translations"]
        ids = [b.id for b in blocks]
        assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
        assert list(tprops["properties"]) == ids and tprops["required"] == ids, tprops
        assert tprops["additionalProperties"] is False and schema["additionalProperties"] is False
        assert "compact JSON" in req["messages"][0]["content"]
        for b in blocks:   # every id got its own line back
            assert b.translation == tag("AI", b.text), (b.id, b.translation)
    sent = json.loads(SEEN[0]["messages"][1]["content"])
    assert sent["bubbles"][0] == {"id": "1", "text": "你到底想怎样啊!!", "vertical": True}
    print("  requests:", len(SEEN), "| /models probes:", len(GETS), "| model:", tr.engine.model)
    assert len(SEEN) == 3 and len(GETS) == 1, (len(SEEN), len(GETS))
    await tr.aclose()


async def blank_lines():
    print("-- 3. blank story lines are asked again, non-story text may stay empty")
    blanks = {"嗯…": "", "啊啊…": "…!", "し": "", "呜呜呜": "", "川": "", "嗯": "",
              "本漫画由星辰汉化组翻译 禁止转载": "", "www.manhuagui.com": "", "QQ群：123456": "", "……": ""}
    tr = setup(blanks=blanks, retry_blanks={"呜呜呜": ""})
    calls = []
    tr.google.translate_texts = fake_google(calls)
    texts = ["等一下", "嗯…", "啊啊…", "し", "呜呜呜", "川", "嗯",
             "本漫画由星辰汉化组翻译 禁止转载", "www.manhuagui.com", "QQ群：123456", "……"]
    blocks = blocks_of(texts)
    used = await tr.translate_page(blocks, "zh", "zh", "th")
    for b in blocks:
        print(f"  {b.id}: {b.text} -> {b.translation!r}")
    print("  requests:", len(SEEN), "| sent to Google:", calls)
    assert used == "local" and len(SEEN) == 2
    retry = json.loads(SEEN[1]["messages"][1]["content"])
    ids = ["2", "5", "7"]     # only the story-looking blanks go back to the model
    assert [b["id"] for b in retry["bubbles"]] == ids and retry["note"] == RETRY_NOTE
    assert SEEN[1]["response_format"]["json_schema"]["schema"]["properties"]["translations"]["required"] == ids
    assert {"source": "等一下", "translation": tag("AI", "等一下")} in retry["previous_lines"]   # page context
    got = {b.text: b.translation for b in blocks}
    assert got["等一下"] == tag("AI", "等一下") and got["嗯…"] == tag("AI2", "嗯…") and got["嗯"] == tag("AI2", "嗯")
    assert got["啊啊…"] == "…!"                     # a non-empty answer is kept as is
    assert got["し"] == "" and got["川"] == ""        # stray letters: not asked again, artwork left alone
    assert got["呜呜呜"] == "" and calls == [], calls  # empty twice: the model says it isn't story text
    assert [b.translation for b in blocks[7:]] == ["", "", "", ""]
    # who wrote what: "" only where the artwork stays on purpose; nothing missing, so cacheable
    assert [b.via for b in blocks] == ["local", "local", "local", "", "", "", "local", "", "", "", ""]
    assert blocks.holes is False
    # second try fails: Google fills in (a lone letter that isn't an interjection was never blank)
    CFG["retry_fail"] = True
    calls.clear()
    blocks = blocks_of(["等一下", "嗯…", "し", "嗯"])
    used = await tr.translate_page(blocks, "zh", "zh", "th")
    print("  retry down ->", used, [b.translation for b in blocks])
    assert used == "local" and calls == ["嗯…", "嗯"], calls
    assert [b.translation for b in blocks] == [tag("AI", "等一下"), tag("G", "嗯…"), "", tag("G", "嗯")]
    assert [b.via for b in blocks] == ["local", "google", "", "google"] and blocks.holes is False
    # Google down too: keep the page, but holes keeps it out of the cache ("used" still names the LLM)
    calls.clear()
    tr.google.translate_texts = fake_google(calls, fail=True)
    blocks = blocks_of(["等一下", "嗯…"])
    used = await tr.translate_page(blocks, "zh", "zh", "th")
    print("  retry + Google down ->", used, "holes:", blocks.holes, [b.translation for b in blocks])
    assert used == "local" and blocks.holes is True
    assert blocks[0].translation == tag("AI", "等一下") and blocks[1].translation == ""
    assert [b.via for b in blocks] == ["local", ""]
    await tr.aclose()


async def downgrade_chain():
    print("-- 4. json_schema -> json_object -> none, temperature/reasoning dropped")

    def bare_keyed(page, req):   # no schema to follow: model answers {"1": "...", ...} in a fence
        return "```json\n" + json.dumps({b["id"]: tag("AI", b["text"]) for b in page["bubbles"]},
                                         ensure_ascii=False, indent=2) + "\n```"

    tr = setup(reject=("temperature", "reasoning_effort", "json_schema", "json_object"), reply=bare_keyed)
    blocks = blocks_of(["你好", "再见"])
    used = await tr.translate_page(blocks, "zh", "zh", "th")
    e = tr.engine
    print("  requests:", [(sorted(k for k in r if k in ("temperature", "reasoning_effort")),
                           (r.get("response_format") or {}).get("type")) for r in SEEN])
    assert used == "local" and e.json_mode == "none" and not e.send_temperature and not e.send_no_think
    assert [b.translation for b in blocks] == [tag("AI", "你好"), tag("AI", "再见")]
    assert len(SEEN) == 5 and "response_format" not in SEEN[-1]
    # later pages go straight to the working request
    await tr.translate_page(blocks_of(["谢谢"]), "zh", "zh", "th")
    assert len(SEEN) == 6
    await tr.aclose()


async def server_restart():
    print("-- 5. model swapped after a server restart: re-probe once")
    tr = setup()
    await tr.translate_page(blocks_of(["你好"]), "zh", "zh", "th")
    assert tr.engine.model == "gemma-3-12b-it" and len(GETS) == 1
    CFG["models"] = ["qwen3-8b"]
    blocks = blocks_of(["再见", "谢谢"])
    used = await tr.translate_page(blocks, "zh", "zh", "th")
    print("  model now:", tr.engine.model, "| probes:", len(GETS), "| requests:", [r["model"] for r in SEEN])
    assert used == "local" and tr.engine.model == "qwen3-8b" and len(GETS) == 2
    assert blocks[1].translation == tag("AI", "谢谢")
    await tr.translate_page(blocks_of(["好"]), "zh", "zh", "th")
    assert len(GETS) == 2 and SEEN[-1]["model"] == "qwen3-8b"
    await tr.aclose()


async def via_and_holes():
    print("-- 7. via names who wrote each bubble; holes marks a story bubble still missing")
    tr = setup()
    calls = []
    tr.google.translate_texts = fake_google(calls)
    page = blocks_of(["你好", "再见"])
    used = await tr.translate_page(page, "zh", "zh", "th")
    assert used == "local" and [b.via for b in page] == ["local", "local"] and page.holes is False
    # an id left out: Google fills it in, the page is still the model's
    CFG["drop"] = {"再见"}
    page = blocks_of(["你好", "再见"])
    used = await tr.translate_page(page, "zh", "zh", "th")
    assert used == "local" and [b.via for b in page] == ["local", "google"] and page.holes is False
    assert calls == ["再见"] and page[1].translation == tag("G", "再见")
    # an echo of the source: Google (auto-detect) fixes it
    CFG["drop"], CFG["echo"] = set(), {"等一下"}
    calls.clear()
    page = blocks_of(["你好", "等一下"])
    used = await tr.translate_page(page, "zh", "zh", "th")
    assert used == "local" and [b.via for b in page] == ["local", "google"] and page.holes is False
    # ...unless Google's auto-detect gives the same text back too: asking again won't change it, no hole
    tr.google.translate_texts = fake_google(calls, echo=True)
    page = blocks_of(["你好", "等一下"])
    used = await tr.translate_page(page, "zh", "zh", "th")
    print("  echo twice ->", used, [(b.via, b.translation) for b in page], "holes:", page.holes)
    assert used == "local" and page[1].translation == "等一下" and page[1].via == "local" and page.holes is False
    # ...but if Google couldn't be asked, the bubble is still in Chinese: the page has a hole
    tr.google.translate_texts = fake_google(calls, fail=True)
    page = blocks_of(["你好", "等一下"])
    used = await tr.translate_page(page, "zh", "zh", "th")
    assert used == "local" and page[1].translation == "等一下" and page[1].via == "local" and page.holes is True
    # the same page translated again cleanly: holes is cleared
    CFG["echo"] = set()
    used = await tr.translate_page(page, "zh", "zh", "th")
    assert page.holes is False and [b.via for b in page] == ["local", "local"]
    # every bubble echoed and Google fixed them all: nothing of the model's survived
    tr.google.translate_texts = fake_google(calls)
    CFG["echo"] = {"你好", "等一下"}
    page = blocks_of(["你好", "等一下"])
    used = await tr.translate_page(page, "zh", "zh", "th")
    assert used == "google" and [b.via for b in page] == ["google", "google"] and page.holes is False
    # the engine fails: Google writes the whole page
    CFG["echo"], CFG["down"] = set(), True
    calls.clear()
    page = blocks_of(["你好", "再见"])
    used = await tr.translate_page(page, "zh", "zh", "th")
    print("  engine down ->", used, [(b.via, b.translation) for b in page])
    assert used == "google" and [b.via for b in page] == ["google", "google"] and calls == ["你好", "再见"]
    # a plain list (no .holes to set) is fine too
    CFG["down"] = False
    plain = [TextBlock("1", "谢谢", [], (0, 0, 1, 1))]
    assert await tr.translate_page(plain, "zh", "zh", "th") == "local" and plain[0].via == "local"

    # Simplified -> Traditional Chinese: many bubbles are written the same in both scripts
    def zh_tw(calls):
        async def translate_texts(texts, src, tgt):
            calls.append((src, list(texts)))
            return [{"我们走吧": "我們走吧", "什么?!": "什麼?!"}.get(t, t) for t in texts]
        return translate_texts
    s = Settings()
    s.engine = "google"
    g = Translator(s)
    texts = ["你好", "等一下", "我们走吧", "什么?!"]
    for src, want in (("zh", [("zh", texts), ("auto", ["你好", "等一下"])]),   # checked once with auto-detect
                      ("auto", [("auto", texts)])):                           # auto-detect already said so
        gcalls = []
        g.google.translate_texts = zh_tw(gcalls)
        page = blocks_of(texts)
        used = await g.translate_page(page, src, "zh", "zh-TW")
        assert used == "google" and page.holes is False and [b.via for b in page] == ["google"] * 4
        assert [b.translation for b in page] == ["你好", "等一下", "我們走吧", "什麼?!"] and gcalls == want, gcalls
    await g.aclose()
    # the model answering the same way: its answer stands, and the page is no hole either
    CFG["echo"] = {"你好"}
    gcalls = []
    tr.google.translate_texts = zh_tw(gcalls)
    page = blocks_of(["你好", "我们走吧"])
    used = await tr.translate_page(page, "zh", "zh", "zh-TW")
    print("  zh -> zh-TW ->", used, [(b.via, b.translation) for b in page], "holes:", page.holes)
    assert used == "local" and [b.via for b in page] == ["local", "local"] and page.holes is False
    assert page[0].translation == "你好" and gcalls == [("auto", ["你好"])]
    await tr.aclose()


async def reading_hint():
    print("-- 8. the reading-order hint follows the bubbles, like ocr._reading_order")
    old_tag = hashlib.sha1((SYSTEM_PROMPT + OUTPUT_LIST + OUTPUT_KEYED + THAI_REGISTER + RETRY_NOTE)
                           .encode()).hexdigest()[:6]
    tr = setup()
    assert PROMPT_TAG != old_tag and tr.cache_tag.endswith(PROMPT_TAG)   # old hints aren't served from cache
    cases = [("zh", [True, True, True], ORDER_RTL),          # vertical Chinese: laid out like manga
             ("zh", [False, False], ORDER_LTR),              # horizontal Chinese (manhua, webtoon)
             ("ja", [False, False], ORDER_RTL),              # Japanese: always right-to-left
             ("ja", [True, True], ORDER_RTL),
             ("zh", [True, False, False], ORDER_LTR),        # a vertical minority doesn't flip it
             ("zh", [True, True, False, False], ORDER_LTR),  # half is not most (same as ocr)
             ("auto", [True, True, False], ORDER_RTL),
             ("ko", [False, False], ORDER_LTR)]
    for src, vert, want in cases:
        page = OCRResult(TextBlock(str(i), "你好" if src != "ja" else "おい", [], (100 * i, 0, 100 * i + 60, 60),
                                   vertical=v) for i, v in enumerate(vert, 1))
        await tr.translate_page(page, src, src, "th")
        sent = json.loads(SEEN[-1]["messages"][1]["content"])
        assert sent["reading_order"] == want == reading_order(src, page), (src, vert, sent["reading_order"])
        # ocr numbers the same row right-to-left exactly when the hint says so
        rtl = [b.box[0] for b in _reading_order(list(page), src)] == sorted((b.box[0] for b in page), reverse=True)
        assert rtl == (want == ORDER_RTL), (src, vert)
    print("  ok:", len(cases), "cases | prompt tag", old_tag, "->", PROMPT_TAG)
    await tr.aclose()


async def model_swap():
    print("-- 9. model swapped in LM Studio (field left blank): followed without per-page probes")
    logs = Logs()
    swapped = "โมเดลบนเซิร์ฟเวอร์เปลี่ยนเป็น"
    tr = setup(models=["text-embedding-nomic", "gemma-3-12b-it", "qwen3-8b"])
    await tr.translate_page(blocks_of(["你好"]), "zh", "zh", "th")
    assert tr.engine.model == "gemma-3-12b-it" and len(GETS) == 1 and len(V0) == 1
    # LM Studio answers with what it has loaded: adopt it, log once even with two pages in flight
    CFG["serve_as"] = "qwen3-8b"
    pages = [blocks_of(["再见"]), blocks_of(["谢谢"])]
    await asyncio.gather(*(tr.translate_page(p, "zh", "zh", "th") for p in pages))
    assert tr.engine.model == "qwen3-8b" and logs.count(swapped) == 1, logs.lines
    assert all(p[0].via == "local" for p in pages)
    await tr.translate_page(blocks_of(["好"]), "zh", "zh", "th")
    assert SEEN[-1]["model"] == "qwen3-8b" and len(GETS) == 1 and len(V0) == 1   # no extra round trips
    # LM Studio JIT-loads whatever is asked for, so a swap back is only seen by looking
    e = tr.engine

    def due(pause=False):   # as if RECHECK seconds of pages went by, or a pause of IDLE seconds
        e._checked -= (e.IDLE if pause else e.RECHECK) + 1
        if pause:
            e._last_reply -= e.IDLE + 1

    async def page(n=1):
        await asyncio.gather(*(tr.translate_page(blocks_of(["好"]), "zh", "zh", "th") for _ in range(n)))
        return SEEN[-1]["model"]

    del CFG["serve_as"]
    CFG["loaded"] = ["gemma-3-12b-it"]
    assert await page() == "qwen3-8b" and len(V0) == 1                         # not yet: pages keep coming
    due()
    assert await page() == "gemma-3-12b-it" and len(V0) == 2 and logs.count(swapped) == 2
    assert await page() == "gemma-3-12b-it" and len(V0) == 2 and len(GETS) == 1
    # nothing loaded (idle auto-unload): keep ours, JIT loads it back
    CFG["loaded"] = []
    due()
    assert await page() == "gemma-3-12b-it" and len(V0) == 3
    # swapped during a pause: the first page after it looks before asking for the old model
    CFG["loaded"] = ["qwen3-8b"]
    due(pause=True)
    assert await page() == "qwen3-8b" and len(V0) == 4 and logs.count(swapped) == 3
    # swapped while pages kept coming: our next page JIT-loaded the old one back next to the new one
    CFG["loaded"] = ["gemma-3-12b-it", "qwen3-8b"]
    assert await page() == "qwen3-8b" and len(V0) == 4
    due()
    assert await page() == "gemma-3-12b-it" and len(V0) == 5 and logs.count(swapped) == 4
    due()
    assert await page() == "gemma-3-12b-it" and len(V0) == 6                   # both still loaded: no flip back
    # pages arriving while a look is under way wait for it instead of asking for the old model
    CFG["loaded"], CFG["v0_delay"] = ["qwen3-8b"], 0.3
    due()
    n = len(SEEN)
    await page(3)
    assert [r["model"] for r in SEEN[n:]] == ["qwen3-8b"] * 3 and len(V0) == 7 and logs.count(swapped) == 5
    CFG["v0_delay"] = 0
    print("  model now:", tr.engine.model, "| /models:", len(GETS), "| /api/v0/models:", len(V0),
          "| requests:", len(SEEN))
    await tr.aclose()
    # two models loaded on purpose before reading: the first one is used and kept
    tr = setup(models=["gemma-3-12b-it", "qwen3-8b"], loaded=["gemma-3-12b-it", "qwen3-8b"])
    await tr.translate_page(blocks_of(["你好"]), "zh", "zh", "th")
    tr.engine._checked -= tr.engine.RECHECK + 1
    await tr.translate_page(blocks_of(["你好"]), "zh", "zh", "th")
    assert [r["model"] for r in SEEN] == ["gemma-3-12b-it"] * 2 and len(V0) == 2 and logs.count(swapped) == 5
    await tr.aclose()
    # the user typed a model: never swapped behind their back
    tr = setup(model="gemma-3-12b-it", serve_as="qwen3-8b", loaded=["qwen3-8b"],
               models=["gemma-3-12b-it", "qwen3-8b"])
    await tr.translate_page(blocks_of(["你好"]), "zh", "zh", "th")
    tr.engine._checked -= tr.engine.RECHECK + 1
    await tr.translate_page(blocks_of(["你好"]), "zh", "zh", "th")
    assert tr.engine.model == "gemma-3-12b-it" and SEEN[-1]["model"] == "gemma-3-12b-it" and len(V0) == 0
    assert logs.count(swapped) == 5
    logs.close()
    await tr.aclose()


async def token_caps():
    print("-- 10. max_tokens: scaled to the page and its text, roomy when thinking, dropped if rejected")
    tr = setup(blanks={"嗯…": "", "呜呜": ""})
    page = blocks_of(["等一下", "嗯…", "呜呜", "好"])
    await tr.translate_page(page, "zh", "zh", "th")
    assert SEEN[0]["reasoning_effort"] == "none" and SEEN[0]["max_tokens"] == 512 + 160 * 4 + 3 * 8
    assert json.loads(SEEN[1]["messages"][1]["content"])["note"] == RETRY_NOTE
    assert SEEN[1]["max_tokens"] == 512 + 160 * 2 + 3 * 4         # the blank re-ask: its own cap
    await tr.aclose()
    # a few long captions: room for their text, also for tokenizers that handle Thai worse than Gemma
    for per_char in (1.1, 1.8, 2.5):
        tr = setup(per_char=per_char)
        page = blocks_of([f"第{i}段：" + "城市的夜晚依旧安静而漫长" * 33 for i in (1, 2, 3)])   # 400 characters each
        need = per_char * 1200 + 12 * 3
        used = await tr.translate_page(page, "zh", "zh", "th")
        assert used == "local" and [b.via for b in page] == ["local"] * 3 and len(SEEN) == 1, (per_char, used)
        assert SEEN[0]["max_tokens"] == 512 + 160 * 3 + 3 * 1200 > need > 512 + 160 * 3   # bubbles alone: cut
        await tr.aclose()
    # thinking on: no reasoning_effort, much more room
    tr = setup(thinking=True)
    await tr.translate_page(blocks_of(["你好", "再见"]), "zh", "zh", "th")
    assert "reasoning_effort" not in SEEN[-1] and SEEN[-1]["max_tokens"] == 8192 + 160 * 2 + 3 * 4
    await tr.aclose()
    # cut off (finish_reason=length): the page goes to Google
    tr = setup(finish="length")
    calls = []
    tr.google.translate_texts = fake_google(calls)
    page = blocks_of(["你好", "再见"])
    used = await tr.translate_page(page, "zh", "zh", "th")
    assert used == "google" and calls == ["你好", "再见"] and len(SEEN) == 1 and tr.failures == 1
    await tr.aclose()
    # a server that thinks although told not to: cut off once, then asked again with room, and roomy after
    logs = Logs()
    tr = setup(thinks=1500)
    page = blocks_of(["你好"])
    used = await tr.translate_page(page, "zh", "zh", "th")
    print("  thinks anyway ->", used, [r["max_tokens"] for r in SEEN])
    assert used == "local" and page[0].via == "local" and tr.engine.thinks_anyway
    assert [r["max_tokens"] for r in SEEN] == [512 + 160 + 6, 8192 + 160 + 6]
    await tr.translate_page(blocks_of(["你好"]), "zh", "zh", "th")
    assert len(SEEN) == 3 and SEEN[-1]["max_tokens"] == 8192 + 160 + 6 and logs.count("คิดก่อนตอบ") == 1
    logs.close()
    await tr.aclose()
    # a server that rejects max_tokens: dropped like temperature, the json mode is left alone
    tr = setup(reject=("max_tokens",))
    used = await tr.translate_page(blocks_of(["你好"]), "zh", "zh", "th")
    e = tr.engine
    assert used == "local" and not e.send_max_tokens and e.json_mode == "json_schema" and e.send_temperature
    assert "max_tokens" in SEEN[0] and "max_tokens" not in SEEN[1] and len(SEEN) == 2
    await tr.translate_page(blocks_of(["你好"]), "zh", "zh", "th")
    assert len(SEEN) == 3 and "max_tokens" not in SEEN[-1]
    await tr.aclose()
    # hosted OpenAI-compatible APIs get no max_tokens (or temperature)
    s = Settings()
    s.engine, s.openai_base_url, s.openai_api_key, s.openai_model = "openai", "http://127.0.0.1:8766/v1", "k", "qwen3-8b"
    await setup(models=["qwen3-8b"]).aclose()
    tr = Translator(s)
    assert await tr.translate_page(blocks_of(["你好"]), "zh", "zh", "th") == "openai"
    assert "max_tokens" not in SEEN[-1] and "temperature" not in SEEN[-1]
    await tr.aclose()


def parsing():
    print("-- 6. parser accepts both formats; blank / noise checks")
    blocks = blocks_of(["a", "b"])
    forms = [
        '{"translations":[{"id":"1","text":"x"},{"id":"2","text":"y"}],"new_terms":[]}',
        '{"translations":{"1":"x","2":"y"},"new_terms":[]}',
        '```json\n{\n  "1": "x",\n  "2": "y"\n}\n```',
        '<think>hm</think>{"translations":{"1":{"text":"x"},"2":{"text":"y"}}}',
        '[{"id":"1","text":"x"},{"id":2,"text":"y"}]',
    ]
    for f in forms:
        res, _ = _collect(blocks, parse_json_loose(f))
        assert res == {"1": "x", "2": "y"}, (f, res)
    try:
        _collect(blocks, parse_json_loose('{"translations":{"7":"x"}}'))
        raise AssertionError("ids from another page must not count")
    except TranslateError:
        pass
    # what counts as a blank story line
    assert blank_story("嗯…", "") and blank_story("ひっ", " ") and blank_story("嗯", "")
    assert not blank_story("し", "") and not blank_story("川", "")
    assert not blank_story("ひっ", "…!") and not blank_story("……", "")
    assert not blank_story("QQ群：123456", "") and not blank_story("关注公众号 感谢观看", "")
    assert lone_noise("し") and lone_noise("ノ") and lone_noise("川")
    assert not lone_noise("嗯…") and not lone_noise("ん") and not lone_noise("ひっ") and not lone_noise("等一下")


async def main():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8766), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        await legacy_list_format()
        await keyed_format()
        await blank_lines()
        await downgrade_chain()
        await server_restart()
        parsing()
        await via_and_holes()
        await reading_hint()
        await model_swap()
        await token_caps()
    finally:
        srv.shutdown()
    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
