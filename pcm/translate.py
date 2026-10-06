"""Translation engines.

Every engine translates one *page* at a time (all bubbles together) so LLM
engines see the whole conversation. LLM engines also get a rolling context of
earlier lines and a glossary so names stay consistent between pages.
If an LLM engine fails, the page falls back to the free Google engine; an
engine that keeps failing (bad key, quota) is benched so pages don't wait on it.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from collections import OrderedDict, deque

import httpx

from .config import Settings
from .model import TextBlock

LOG = logging.getLogger("poomcatomanga.translate")

LANG_NAMES = {
    "th": "Thai", "en": "English", "ja": "Japanese", "ko": "Korean", "zh": "Chinese",
    "zh-CN": "Simplified Chinese", "zh-TW": "Traditional Chinese", "vi": "Vietnamese", "id": "Indonesian",
}

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

_KANA = re.compile(r"[ぁ-ゟァ-ヺ]")   # letters only (not ・ or ー)
_HANGUL = re.compile(r"[가-힯]")
_HAN = re.compile(r"[㐀-鿿]")
_LETTER = re.compile(r"[A-Za-zぁ-ゟァ-ヺ가-힯㐀-鿿]")


class TranslateError(RuntimeError):
    pass


class FatalEngineError(TranslateError):
    """Retrying won't help for the rest of the session (bad key, unknown model, daily quota)."""


# ------------------------------------------------------------------ prompt ---

SYSTEM_PROMPT = """You are an expert manga, manhwa and manhua translator and localizer.
You receive the speech bubbles of ONE comic page as JSON, already in reading order, plus
optional context from earlier pages and a glossary. Translate every bubble into {target}.

Rules:
- Translate the meaning and tone, not word by word. Write the way a native {target} comic
  translator would: natural, vivid, easy to read in a small bubble.
- The text comes from OCR. It may contain recognition mistakes or stray characters, and the
  columns of a "vertical" bubble may be out of order. Silently infer the intended text.
- Keep translations about as short as the original; bubbles are small. No notes, no
  explanations, no quotation marks around the text, no romanization in brackets.
- Keep character names, places and special terms consistent with the glossary and the
  earlier context. Transliterate new names naturally; put only names that are not in the
  glossary yet in "new_terms".
- Every bubble gets a {target} translation, however short: dialogue, interjections, moans,
  gasps, laughter, stutters and sound effects too (give a short natural {target} equivalent).
- The ONLY exception is text that is not part of the story (watermarks, scanlator credits,
  website URLs, page numbers): return an empty string for that id.
- If an image is attached, the red numbers mark bubble ids; use it to tell who is speaking.
{register}
{output}"""

OUTPUT_LIST = """Return ONLY JSON matching: {"translations":[{"id":"<id>","text":"<translation>"}],
"new_terms":[{"source":"<original>","target":"<translation>"}]} with exactly one
entry per input id."""

# keyed by bubble id: ~40% fewer output tokens than the list form, and ids can't drift
OUTPUT_KEYED = """Return ONLY compact JSON on one line (no line breaks, no indentation):
{"translations":{"<id>":"<translation>",...},"new_terms":[{"source":"<original>","target":"<translation>"}]}
with exactly one key per input id."""

THAI_REGISTER = """- Thai: choose pronouns and sentence-final particles (ครับ/ค่ะ/นะ/ล่ะ/เหรอ/สิ/เถอะ/วะ/โว้ย)
  from each speaker's personality, gender and the situation; keep them consistent across the
  conversation. Casual speech between friends should sound casual, not textbook-polite.
  Use Thai punctuation habits (no full stop at the end of sentences; keep ! ? … where useful).
"""

# second try, only for the bubbles an LLM left empty although they look like story text
RETRY_NOTE = ("These bubbles were left empty, but they look like story text: dialogue, an interjection, "
              "a moan, a gasp or a sound effect. Translate each one; the rest of this page is at the end "
              "of previous_lines. Return an empty string only for a bubble that is OCR noise from the "
              "artwork or not part of the story (watermark, credits, ads).")

ORDER_RTL = "right-to-left manga"
ORDER_LTR = "top-to-bottom, left-to-right"

# part of the LLM cache key: pages translated with an older prompt are translated again.
# Bump PAYLOAD_REV when what a page sends changes without any text above changing.
PAYLOAD_REV = "2"   # 2: mostly-vertical pages are announced right-to-left
PROMPT_TAG = hashlib.sha1((SYSTEM_PROMPT + OUTPUT_LIST + OUTPUT_KEYED + THAI_REGISTER + RETRY_NOTE
                           + ORDER_RTL + ORDER_LTR + PAYLOAD_REV).encode()).hexdigest()[:6]

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        },
        "new_terms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"source": {"type": "string"}, "target": {"type": "string"}},
                "required": ["source", "target"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["translations", "new_terms"],
    "additionalProperties": False,
}


def page_schema(ids: list[str]) -> dict:
    """RESULT_SCHEMA keyed by this page's bubble ids: every id required, nothing else allowed."""
    ids = list(dict.fromkeys(ids))
    return {
        "type": "object",
        "properties": {
            "translations": {"type": "object", "properties": {i: {"type": "string"} for i in ids},
                             "required": ids, "additionalProperties": False},
            "new_terms": RESULT_SCHEMA["properties"]["new_terms"],
        },
        "required": ["translations", "new_terms"],
        "additionalProperties": False,
    }


def lang_name(code: str) -> str:
    return LANG_NAMES.get(code, LANG_NAMES.get(code.split("-")[0], code))


def system_prompt(target: str, keyed: bool = False) -> str:
    return SYSTEM_PROMPT.format(target=lang_name(target),
                                register=THAI_REGISTER if target.startswith("th") else "",
                                output=OUTPUT_KEYED if keyed else OUTPUT_LIST)


def reading_order(src: str, blocks=()) -> str:
    """The rule ocr._reading_order numbers bubbles by: right-to-left for Japanese and for pages
    that are mostly vertical text (vertical Chinese is laid out like manga)."""
    rtl = (src or "").split("-")[0] == "ja" or sum(b.vertical for b in blocks) * 2 > len(blocks)
    return ORDER_RTL if rtl else ORDER_LTR


def parse_json_loose(text: str) -> dict:
    """Parse model output that should be JSON but may be wrapped in fences/thinking."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    a, b = text.find("{"), text.rfind("}")
    if a >= 0 and b > a:
        return json.loads(text[a:b + 1])
    raise TranslateError("model did not return JSON")


def untranslated(src: str, out: str, target: str) -> bool:
    """True if `out` still looks like the source language (wrong source hint, model echo)."""
    if not out.strip() or not _LETTER.search(src):   # "……", "!!", "・・・" are fine as they are
        return False
    if out.strip() == src.strip() and len(src.strip()) > 1:
        return True
    t = target.split("-")[0]
    if t != "ja" and _KANA.search(out):
        return True
    if t != "ko" and _HANGUL.search(out):
        return True
    if t not in ("ja", "zh") and len(_HAN.findall(out)) >= 2:
        return True
    return False


_NON_STORY = re.compile(
    r"https?:|www\.|\w\.(?:com|net|org|io|co|cc|me|tv|xyz|top|vip|info|site|club|online|fun|link|app|cn|jp|kr)\b"
    r"|汉化|漢化|翻译|翻譯|嵌字|校对|校對|扫图|掃圖|图源|圖源|转载|轉載|版权|版權|翻訳|写植|転載|번역|식자|스캔|무단"
    r"|QQ|微博|公众号|公眾號|感谢观看|感謝觀看|本章完|未完待[续續]"
    r"|scanlat|translat|credits?\b|patreon|discord|copyright|©"
    r"|^\W*(?:p\.?|page|第)?\s*\d+\s*(?:页|頁|ページ|페이지)?\W*$", re.I)

# one-letter bubbles that are real interjections; any other lone letter is usually OCR noise
_INTERJ = set("嗯啊哈唔呃哼哦噢喔呀哇嘿咦欸诶唉哎呜嘤喂呵嘻"
              "あぁいぃうぅえぇおぉんはひふへほアァイィウゥエェオォンハヒフヘホ"
              "아어오우음응흥헉헐윽앗엣흑")


def lone_noise(src: str) -> bool:
    """A single letter that isn't an interjection ("し", "ノ", "川"): Lens reading the artwork."""
    letters = _LETTER.findall(src)
    return len(letters) == 1 and letters[0] not in _INTERJ


def blank_story(src: str, out: str) -> bool:
    """True if the engine left a bubble empty although it is story text. Only watermarks,
    credits, URLs, page numbers and stray letters may stay empty; a non-empty answer ("…!") is kept."""
    if out.strip() or not _LETTER.search(src) or lone_noise(src):   # "……" / "!!": nothing to translate
        return False
    return not _NON_STORY.search(src)


class Memory:
    """Rolling context + glossary for one reading session. The user's own glossary is
    always sent in full; learned names fill the remaining room."""
    LEARNED_CAP = 200
    SEND_LEARNED = 60

    def __init__(self, glossary: list[tuple[str, str]]):
        self.lines: deque[tuple[str, str]] = deque(maxlen=40)
        self.user_terms: dict[str, str] = dict(glossary)
        self.learned: "OrderedDict[str, str]" = OrderedDict()

    @property
    def terms(self) -> dict[str, str]:
        return {**self.learned, **self.user_terms}

    def payload(self, blocks: list[TextBlock], src: str, note: str = "") -> str:
        learned = [(k, v) for k, v in self.learned.items() if k not in self.user_terms][-self.SEND_LEARNED:]
        glossary = list(self.user_terms.items()) + learned
        return json.dumps({
            "source_language": lang_name(src) if src and src != "auto" else "auto-detect",
            "reading_order": reading_order(src, blocks),
            "glossary": [{"source": k, "target": v} for k, v in glossary],
            "previous_lines": [{"source": s, "translation": t} for s, t in list(self.lines)[-12:]],
            **({"note": note} if note else {}),
            "bubbles": [{"id": b.id, "text": b.text, **({"vertical": True} if b.vertical else {})} for b in blocks],
        }, ensure_ascii=False, separators=(",", ":"))   # ~20% fewer prompt tokens than ", " / ": "

    def learn(self, blocks: list[TextBlock], result: dict[str, str], new_terms) -> None:
        for b in blocks:
            t = result.get(b.id, "")
            if t:
                self.lines.append((b.text, t))
        for item in new_terms or []:
            try:
                s, t = str(item["source"]).strip(), str(item["target"]).strip()
            except Exception:
                continue
            if s and t and len(s) <= 40 and s not in self.user_terms and s not in self.learned:
                self.learned[s] = t
                while len(self.learned) > self.LEARNED_CAP:
                    self.learned.popitem(last=False)


def _collect(blocks: list[TextBlock], data) -> tuple[dict[str, str], list]:
    """Accepts the keyed form {"translations": {"1": "..."}} and the list form
    {"translations": [{"id": "1", "text": "..."}]} (servers without json_schema may send either)."""
    out: dict[str, str] = {}
    if isinstance(data, list):
        data = {"translations": data}
    if not isinstance(data, dict):
        raise TranslateError("model did not return a JSON object")
    items = data.get("translations")
    if items is None and any(b.id in data for b in blocks):   # bare {"1": "...", ...}
        items = data
    if isinstance(items, dict):  # {"1": "...", ...}
        items = [{"id": k, "text": v} for k, v in items.items()]
    for it in items or []:
        try:
            text = it.get("text") or ""
            if isinstance(text, dict):   # {"1": {"text": "..."}}
                text = text.get("text") or ""
            out[str(it["id"])] = str(text).strip()
        except Exception:
            continue
    ids = {b.id for b in blocks}
    out = {k: v for k, v in out.items() if k in ids}
    if not out:
        raise TranslateError("model returned no usable translations")
    return out, data.get("new_terms") or []


def _http_error(name: str, r: httpx.Response) -> TranslateError:
    body = r.text[:300]
    low = r.text.lower()   # classify on the full body (quota ids sit deep inside error.details)
    if r.status_code in (401, 403) or "api key not valid" in low or "invalid api key" in low or "incorrect api key" in low:
        return FatalEngineError(f"{name}: API key ไม่ถูกต้องหรือไม่มีสิทธิ์ (HTTP {r.status_code})")
    if r.status_code == 404 or ("model" in low and ("not found" in low or "does not exist" in low)):
        return FatalEngineError(f"{name}: ไม่พบโมเดลนี้ ตรวจสอบชื่อโมเดลในหน้าตั้งค่า (HTTP {r.status_code})")
    if r.status_code == 429 and ("perday" in low.replace("_", "").replace("-", "") or "daily" in low):
        return FatalEngineError(f"{name}: โควตาฟรีของวันนี้หมดแล้ว")
    return TranslateError(f"{name} HTTP {r.status_code}: {body}")


def _model_gone(r: httpx.Response) -> bool:
    low = r.text.lower()
    return r.status_code == 404 or ("model" in low and any(
        k in low for k in ("not found", "does not exist", "not loaded", "no models")))


def _thought(data: dict, msg: dict) -> bool:
    """Did the model think before answering? (LM Studio: reasoning_content; Ollama: reasoning or <think>)"""
    det = (data.get("usage") or {}).get("completion_tokens_details") or {}
    return bool(msg.get("reasoning_content") or msg.get("reasoning") or det.get("reasoning_tokens")
                or "<think>" in (msg.get("content") or ""))


# ---------------------------------------------------------------- engines ---

class GoogleFree:
    """Unofficial Google Translate web endpoints (no key). Same quality as Google Translate.

    Primary: the Chrome-extension endpoint, which accepts many `q` params in one GET
    (a whole page per request) and detects the language of each text separately.
    Backup: the classic `gtx` endpoint, one text per request.
    """
    name = "google"
    model = ""
    BATCH_URL = "https://clients5.google.com/translate_a/t"
    GTX_URL = "https://translate.googleapis.com/translate_a/single"
    MAX_QUERY = 6000  # url-encoded characters per batch request

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.sem = asyncio.Semaphore(3)

    async def _get(self, url: str, params, attempts: int = 4):
        last = None
        for attempt in range(attempts):
            try:
                async with self.sem:
                    r = await self.client.get(url, params=params, headers={"User-Agent": BROWSER_UA}, timeout=25)
                if r.status_code == 429 or r.status_code >= 500:
                    last = TranslateError(f"Google Translate HTTP {r.status_code}")
                else:
                    r.raise_for_status()
                    return r.json()
            except (httpx.HTTPError, ValueError) as e:
                last = e
            if attempt < attempts - 1:
                await asyncio.sleep(2.0 * (attempt + 1))
        raise TranslateError(f"Google Translate: {last}")

    async def _batch(self, texts: list[str], src: str, tgt: str) -> list[str]:
        params = [("client", "dict-chrome-ex"), ("sl", src or "auto"), ("tl", tgt)]
        params += [("q", t) for t in texts]
        data = await self._get(self.BATCH_URL, params)
        if isinstance(data, str):
            data = [data]
        if not isinstance(data, list) or len(data) != len(texts):
            raise TranslateError("Google Translate: unexpected batch response")
        return [(item[0] if isinstance(item, list) and item else str(item)).strip() for item in data]

    async def _gtx(self, text: str, src: str, tgt: str) -> str:
        params = {"client": "gtx", "sl": src or "auto", "tl": tgt, "dt": "t", "q": text}
        data = await self._get(self.GTX_URL, params, attempts=2)
        return "".join(seg[0] for seg in (data[0] or []) if seg and seg[0]).strip()

    async def translate_texts(self, texts: list[str], src: str, tgt: str) -> list[str]:
        from urllib.parse import quote
        out = [""] * len(texts)
        chunks, cur, size = [], [], 0
        for i, t in enumerate(texts):
            if not t.strip():
                continue
            n = len(quote(t)) + 3
            if cur and size + n > self.MAX_QUERY:
                chunks.append(cur)
                cur, size = [], 0
            cur.append(i)
            size += n
        if cur:
            chunks.append(cur)
        for chunk in chunks:
            try:
                res = await self._batch([texts[i] for i in chunk], src, tgt)
            except TranslateError as e:
                LOG.info("Google batch ใช้ไม่ได้ (%s) ลองแบบทีละช่อง", e)
                res = await asyncio.gather(*(self._gtx(texts[i], src, tgt) for i in chunk))
            for i, t in zip(chunk, res):
                out[i] = t
        return out

    async def translate(self, blocks: list[TextBlock], src: str, tgt: str, memory: Memory, image: bytes | None):
        texts = await self.translate_texts([b.text for b in blocks], src, tgt)
        return {b.id: t for b, t in zip(blocks, texts)}


def match_model(name: str, ids: list[str]) -> str | None:
    """Find the server's model id for what the user typed ("Gemma 4 12B" -> "google/gemma-4-12b")."""
    if name in ids:
        return name
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    if not key:
        return None
    norm = {i: re.sub(r"[^a-z0-9]", "", i.lower()) for i in ids}
    for pick in (lambda n: n == key, lambda n: n.endswith(key), lambda n: key in n):
        hits = [i for i in ids if pick(norm[i])]
        if hits:
            return min(hits, key=len)
    return None


class OpenAICompatible:
    """OpenAI-style /chat/completions: LM Studio, Ollama, OpenAI, DeepSeek, OpenRouter..."""
    RECHECK = 30.0      # seconds between looks at which models LM Studio has loaded (model field blank)...
    IDLE = 8.0          # ...and the first page after a pause this long looks first (that's when models get swapped)

    def __init__(self, client: httpx.AsyncClient, base_url: str, api_key: str, model: str, local: bool,
                 thinking: bool = True):
        self.client, self.base, self.key, self.model, self.local = client, base_url.rstrip("/"), api_key, model, local
        self.wanted = model             # what the user typed ("" = whatever the server has loaded)
        self.name = "local" if local else "openai"  # must match Settings.engine
        self.json_mode = "json_schema"  # downgraded automatically if the server rejects it
        self.send_temperature = local   # many hosted reasoning models reject temperature
        # local reasoning models think for 600-3000 tokens per page first; translation doesn't need it
        self.send_no_think = local and not thinking
        self.send_max_tokens = local    # runaway guard, dropped if the server rejects it
        self.thinks_anyway = False      # the server ignores reasoning_effort=none: leave room to think
        self._pick_lock = asyncio.Lock()
        self._prep_error: Exception | None = None
        self._picked = False            # self.model resolved against the server's list (probe once)
        self._checked = 0.0             # when we last looked at what the server has loaded
        self._seen: set[str] = set()    # the models loaded then
        self._check_lock = asyncio.Lock()
        self._busy = 0                  # requests in flight
        self._last_reply = 0.0

    @staticmethod
    def token_cap(n: int, thinking: bool, chars: int = 0) -> int:
        """max_tokens for n bubbles holding `chars` characters. A short bubble is ~12 tokens of keyed
        output, a long caption ~1.1 tokens per Chinese character into Thai (Gemma; tokenizers that
        handle Thai worse need more), so this only stops a runaway (a local model repeating one line
        until the context is full takes minutes)."""
        return (8192 if thinking else 512) + 160 * n + 3 * chars

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.key:
            h["Authorization"] = f"Bearer {self.key}"
        return h

    async def prepare(self) -> str:
        async with self._pick_lock:
            if self._prep_error is not None:      # already known to be unreachable: fail fast
                raise self._prep_error
            if self._picked or (self.wanted and not self.local):
                return self.model
            try:
                # one quick probe: a server that isn't running must not stall every page
                r = await self.client.get(f"{self.base}/models", headers=self._headers(),
                                          timeout=httpx.Timeout(10, connect=3))
                r.raise_for_status()
                models = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                self._prep_error = FatalEngineError(
                    f"เชื่อมต่อ {self.base} ไม่ได้ — ยังไม่ได้เปิด LM Studio/Ollama หรือยังไม่กด Start Server")
                raise self._prep_error from e
            except Exception as e:
                self._prep_error = FatalEngineError(f"อ่านรายชื่อโมเดลจาก {self.base} ไม่ได้: {e}")
                raise self._prep_error from e
            chat = [m for m in models if "embed" not in m.lower()]
            if self.wanted:                        # user typed a name: map it to the server's id
                found = match_model(self.wanted, models)
                if found is None:
                    self._prep_error = FatalEngineError(
                        f"ไม่พบโมเดล \"{self.wanted}\" บนเซิร์ฟเวอร์ — มี: {', '.join(chat) or '-'} "
                        "(เว้นช่องชื่อโมเดลว่างไว้ = ใช้ตัวที่โหลดอยู่)")
                    raise self._prep_error
                self.model = found
            else:
                if not chat:
                    self._prep_error = FatalEngineError("ยังไม่ได้โหลดโมเดลใน LM Studio/Ollama")
                    raise self._prep_error
                loaded = await self._loaded_models()
                self.model = next((m for m in chat if m in loaded), chat[0])
                self._seen = set(loaded)
            self._picked = True
            self._checked = time.monotonic()
            LOG.info("ใช้โมเดล: %s", self.model)
            return self.model

    async def _loaded_models(self) -> list[str]:
        """LM Studio lists every downloaded model on /v1/models; its /api/v0 says which ones are in memory."""
        root = re.sub(r"/v1$", "", self.base)
        try:
            r = await self.client.get(f"{root}/api/v0/models", timeout=httpx.Timeout(5, connect=3))
            r.raise_for_status()
            return [m["id"] for m in r.json().get("data", []) if m.get("state") == "loaded" and m.get("id")
                    and m.get("type") != "embeddings" and "embed" not in m["id"].lower()]
        except Exception:
            return []                              # Ollama and others: no such endpoint

    def _adopt(self, model: str, why: str) -> None:
        if model and model != self.model:
            LOG.info("โมเดลบนเซิร์ฟเวอร์เปลี่ยนเป็น %s (%s) — แปลด้วยตัวนี้ต่อ (เดิม %s)", model, why, self.model)
            self.model = model

    def _check_due(self) -> bool:
        now = time.monotonic()
        return (now - self._checked >= self.RECHECK
                or (not self._busy and now - max(self._checked, self._last_reply) >= self.IDLE))

    async def _recheck(self) -> None:
        """Model field left blank: follow a model swapped in LM Studio. It doesn't fail on the old
        name but JIT-loads that model again, so look at what is loaded: before the first page after
        a pause, and every RECHECK seconds while pages keep coming."""
        if self.wanted or not self.local or not self._picked:
            return
        if not self._check_lock.locked() and not self._check_due():
            return
        async with self._check_lock:               # pages arriving meanwhile wait for the answer
            if not self._check_due():
                return
            self._checked = time.monotonic()
            loaded = await self._loaded_models()
            new = [m for m in loaded if m not in self._seen and m != self.model]
            self._seen = set(loaded)
            if not loaded:                         # idle unload (or not LM Studio): keep ours, JIT brings it back
                return
            if self.model not in loaded:
                self._adopt((new or loaded)[0], "โหลดอยู่ใน LM Studio")
            elif new:                              # loaded since we last looked; ours was JIT-loaded back meanwhile
                self._adopt(new[0], "เพิ่งโหลดใน LM Studio")

    async def translate(self, blocks, src, tgt, memory: Memory, image: bytes | None, note: str = ""):
        await self.prepare()
        await self._recheck()
        model = self.model
        user_text = memory.payload(blocks, src, note)
        content: object = user_text
        if image:
            content = [{"type": "text", "text": user_text},
                       {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(image).decode()}}]
        base_body = {
            "model": model,
            "messages": [{"role": "system", "content": system_prompt(tgt, keyed=True)}, {"role": "user", "content": content}],
        }
        schema = page_schema([b.id for b in blocks])
        chars = sum(len(b.text) for b in blocks)
        reprobed = False
        for _ in range(8):
            mode, temp, no_think, cap = self.json_mode, self.send_temperature, self.send_no_think, self.send_max_tokens
            thinking = not no_think or self.thinks_anyway
            req = dict(base_body)
            if temp:
                req["temperature"] = 0.3
            if no_think:
                req["reasoning_effort"] = "none"
            if cap:
                req["max_tokens"] = self.token_cap(len(blocks), thinking, chars)
            if mode == "json_schema":
                req["response_format"] = {"type": "json_schema",
                                          "json_schema": {"name": "page_translation", "strict": True, "schema": schema}}
            elif mode == "json_object":
                req["response_format"] = {"type": "json_object"}
            self._busy += 1
            try:
                r = await self.client.post(f"{self.base}/chat/completions", json=req, headers=self._headers(),
                                           timeout=httpx.Timeout(300 if self.local else 120, connect=10))
            except httpx.ConnectError as e:
                raise TranslateError(f"เชื่อมต่อ {self.base} ไม่ได้") from e
            finally:
                self._busy -= 1
                self._last_reply = time.monotonic()
            if r.status_code in (400, 422):
                low = r.text.lower()
                if temp and "temperature" in low:
                    if self.send_temperature == temp:
                        self.send_temperature = False
                    continue
                # before the json check: LM Studio's message for it says "...does not satisfy the schema"
                if cap and any(k in low for k in ("max_tokens", "maxpredictedtokens", "max tokens")):
                    self.send_max_tokens = False
                    continue
                if no_think and "reasoning" in low:
                    self.send_no_think = False
                    continue
                if mode != "none" and any(k in low for k in ("response_format", "json", "schema")):
                    nxt = "json_object" if mode == "json_schema" else "none"
                    if self.json_mode == mode:  # another worker may already have downgraded
                        self.json_mode = nxt
                        LOG.info("server ไม่รองรับ %s, ใช้โหมด %s", mode, nxt)
                    continue
            if r.status_code >= 400:
                if not reprobed and (self.local or not self.wanted) and _model_gone(r):
                    # server restarted / model swapped since the first probe: look again once
                    reprobed = True
                    if base_body["model"] == self.model:   # not already re-probed by another worker
                        self._picked = False
                        LOG.info("ไม่พบโมเดล %s บนเซิร์ฟเวอร์แล้ว — ตรวจรายชื่อโมเดลใหม่", self.model)
                    base_body["model"] = await self.prepare()
                    continue
                raise _http_error(self.base, r)
            body = r.json()
            choice = body["choices"][0]
            msg = choice.get("message") or {}
            served = body.get("model")
            if (self.local and not self.wanted and isinstance(served, str) and served
                    and served != base_body["model"] and self.model == base_body["model"]):
                self._adopt(served, "เซิร์ฟเวอร์ตอบด้วยโมเดลนี้")   # LM Studio routed it to what is loaded
            if no_think and not self.thinks_anyway and _thought(body, msg):
                self.thinks_anyway = True
                LOG.info("โมเดลยังคิดก่อนตอบแม้ปิดไว้ — เผื่อความยาวคำตอบให้มากขึ้น")
            if choice.get("finish_reason") == "length":
                if cap and not thinking and self.thinks_anyway:
                    continue                      # cut off while thinking: once more with room for it
                raise TranslateError("คำตอบยาวเกิน (ถูกตัด)")
            data = parse_json_loose(msg.get("content") or "")
            result, terms = _collect(blocks, data)
            memory.learn(blocks, result, terms)
            return result
        raise TranslateError(f"{self.base}: server ปฏิเสธคำขอ")


class Gemini:
    name = "gemini"

    def __init__(self, client: httpx.AsyncClient, base_url: str, api_key: str, model: str):
        self.client, self.base, self.key, self.model = client, base_url.rstrip("/"), api_key, model
        if not api_key:
            raise FatalEngineError("ยังไม่ได้ใส่ Gemini API key")
        self.schema_field = "responseJsonSchema"

    async def translate(self, blocks, src, tgt, memory: Memory, image: bytes | None, note: str = ""):
        parts = [{"text": memory.payload(blocks, src, note)}]
        if image:
            parts.insert(0, {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(image).decode()}})
        url = f"{self.base}/models/{self.model}:generateContent"
        for attempt in range(4):
            gen = {"responseMimeType": "application/json", "maxOutputTokens": 8192}
            if self.schema_field:
                gen[self.schema_field] = RESULT_SCHEMA
            body = {"system_instruction": {"parts": [{"text": system_prompt(tgt)}]},
                    "contents": [{"role": "user", "parts": parts}], "generationConfig": gen}
            r = await self.client.post(url, json=body, headers={"x-goog-api-key": self.key}, timeout=120)
            if r.status_code == 400 and self.schema_field and self.schema_field in r.text:
                self.schema_field = None
                continue
            if r.status_code in (429, 500, 503) and attempt < 3:
                err = _http_error("Gemini", r)
                if isinstance(err, FatalEngineError):
                    raise err
                await asyncio.sleep(4 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise _http_error("Gemini", r)
            data = r.json()
            cand = (data.get("candidates") or [{}])[0]
            if cand.get("finishReason") not in (None, "STOP"):
                raise TranslateError(f"Gemini: {cand.get('finishReason')}")
            text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []) if not p.get("thought"))
            result, terms = _collect(blocks, parse_json_loose(text))
            memory.learn(blocks, result, terms)
            return result
        raise TranslateError("Gemini: ใช้งานถี่เกินไป (rate limit)")


class Claude:
    name = "claude"
    FALLBACK_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")

    def __init__(self, api_key: str, model: str, effort: str):
        if not api_key:
            raise FatalEngineError("ยังไม่ได้ใส่ Claude API key")
        import anthropic
        self._anthropic = anthropic
        self.client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=3, timeout=180.0)
        self.model, self.effort = model or "claude-opus-5-5", effort or "low"
        self.send_effort = not self.model.startswith("claude-haiku")

    async def translate(self, blocks, src, tgt, memory: Memory, image: bytes | None, note: str = ""):
        anthropic = self._anthropic
        content = [{"type": "text", "text": memory.payload(blocks, src, note)}]
        if image:
            content.insert(0, {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                          "data": base64.b64encode(image).decode()}})
        for _ in range(3):
            eff = self.send_effort        # snapshot: other workers may downgrade concurrently
            output_config = {"format": {"type": "json_schema", "schema": RESULT_SCHEMA}}
            if eff:
                output_config["effort"] = self.effort
            kwargs, betas = {}, []
            if self.model in self.FALLBACK_MODELS:
                betas.append("server-side-fallback-2026-07-01")
                kwargs["fallbacks"] = "default"
            try:
                resp = await self.client.beta.messages.create(
                    model=self.model,
                    max_tokens=16000,
                    system=system_prompt(tgt),
                    messages=[{"role": "user", "content": content}],
                    output_config=output_config,
                    betas=betas,
                    **kwargs,
                )
            except anthropic.BadRequestError as e:
                msg = str(e.message).lower()
                if eff and "effort" in msg:
                    self.send_effort = False
                    continue
                if "model" in msg and ("invalid" in msg or "not found" in msg or "does not exist" in msg):
                    raise FatalEngineError(f"Claude: ไม่พบโมเดล {self.model}") from e
                raise TranslateError(f"Claude: {e.message}") from e
            except anthropic.AuthenticationError as e:
                raise FatalEngineError("Claude API key ไม่ถูกต้อง") from e
            except anthropic.PermissionDeniedError as e:
                raise FatalEngineError("Claude: API key นี้ไม่มีสิทธิ์ใช้โมเดลนี้") from e
            except anthropic.NotFoundError as e:
                raise FatalEngineError(f"Claude: ไม่พบโมเดล {self.model}") from e
            except anthropic.RateLimitError as e:
                raise TranslateError("Claude: ใช้งานถี่เกินไป (rate limit)") from e
            except anthropic.APIStatusError as e:
                raise TranslateError(f"Claude HTTP {e.status_code}: {e.message}") from e
            except anthropic.APIConnectionError as e:
                raise TranslateError("Claude: เชื่อมต่อไม่ได้") from e
            if resp.stop_reason == "refusal":
                raise TranslateError("Claude ปฏิเสธการแปลหน้านี้")
            text = next((b.text for b in resp.content if b.type == "text"), "")
            result, terms = _collect(blocks, parse_json_loose(text))
            memory.learn(blocks, result, terms)
            return result
        raise TranslateError("Claude: request rejected")

    async def aclose(self):
        try:
            await self.client.close()
        except Exception:
            pass


class Translator:
    """Front door used by the pipeline: picks the engine, falls back to Google."""
    COOLDOWN = 300  # seconds an engine is benched after repeated failures

    def __init__(self, settings: Settings):
        self.s = settings
        self.client = httpx.AsyncClient(follow_redirects=True, http2=False)
        self.google = GoogleFree(self.client)
        self.memory = Memory(settings.glossary_pairs())
        self.failures = 0
        self.benched_until = 0.0
        self._prep_lock = asyncio.Lock()
        self._prepared = False
        try:
            self.engine = self._make_engine()
        except Exception as e:  # e.g. no API key entered, SDK missing
            LOG.warning("%s — ใช้ Google แปลแทน", e)
            self.engine = self.google

    def _make_engine(self):
        s = self.s
        if s.engine == "local":
            return OpenAICompatible(self.client, s.local_base_url, "", s.local_model, local=True,
                                    thinking=s.local_thinking)
        if s.engine == "openai":
            return OpenAICompatible(self.client, s.openai_base_url, s.openai_api_key, s.openai_model, local=False)
        if s.engine == "gemini":
            return Gemini(self.client, "https://generativelanguage.googleapis.com/v1beta", s.gemini_api_key, s.gemini_model)
        if s.engine == "claude":
            return Claude(s.claude_api_key, s.claude_model, s.claude_effort)
        return self.google

    async def prepare(self):
        """Resolve anything needed before the first page (e.g. is the local server up, which
        model is loaded). Runs once; pages wait for it so a dead server costs one probe only."""
        async with self._prep_lock:
            if self._prepared:
                return
            prep = getattr(self.engine, "prepare", None)
            if prep:
                try:
                    await prep()
                except Exception as e:
                    LOG.warning("%s — ใช้ Google แปลแทน", e)
                    self.engine = self.google
            self._prepared = True

    @property
    def cache_tag(self) -> str:
        """Identifies everything that changes the translation, for the disk cache key."""
        e, s = self.engine, self.s
        parts = [e.name, getattr(e, "model", "") or ""]
        if e.name in ("local", "openai"):
            parts.append(e.base)
        if e.name == "local":
            parts.append("think" if s.local_thinking else "fast")
        if e.name == "claude":
            parts.append(s.claude_effort)
        if e is not self.google:
            gl = "\n".join(f"{k}={v}" for k, v in sorted(self.memory.user_terms.items()))
            parts += [hashlib.sha1(gl.encode()).hexdigest()[:10], "img" if s.send_image_to_ai else "", PROMPT_TAG]
        return ":".join(parts)

    def _bench(self, err: Exception):
        if isinstance(err, FatalEngineError):
            LOG.warning("%s — ใช้ Google แปลแทนจนกว่าจะเริ่มใหม่", err)
            self.engine = self.google
            return
        self.failures += 1
        if self.failures >= 3:
            self.benched_until = time.monotonic() + self.COOLDOWN
            self.failures = 0
            LOG.warning("ตัวแปล %s ล้มเหลวหลายครั้ง — พักไว้ 5 นาที ใช้ Google แทนชั่วคราว", self.engine.name)

    async def translate_page(self, blocks: list[TextBlock], src_setting: str, hint: str, tgt: str,
                             image: bytes | None = None) -> str:
        """Fills block.translation and block.via (who wrote it: the engine's name or "google";
        "" where the original art is kept on purpose). `src_setting` is the user's choice
        ('auto' or a code); `hint` is the page's detected language (only used to brief LLMs).
        Sets blocks.holes when a story bubble is still untranslated after every fallback.
        Returns the engine the page may be cached under: the engine's name, or "google" when
        Google wrote every bubble."""
        if not blocks:
            return self.engine.name
        if not self._prepared:
            await self.prepare()
        result: dict[str, str] = {}
        by_google: set[str] = set()
        engine = self.engine
        src = src_setting if src_setting != "auto" else hint
        image = image if self.s.send_image_to_ai else None
        if engine is not self.google and time.monotonic() >= self.benched_until:
            try:
                result = await engine.translate(blocks, src, tgt, self.memory, image)
                self.failures = 0
            except Exception as e:
                LOG.warning("ตัวแปล %s ล้มเหลว: %s — ใช้ Google แปลหน้านี้แทน", engine.name, e)
                self._bench(e)
        # Google: let it detect each bubble's language, unless the user fixed the source
        # language AND the bubble really is in that language (a fixed "ja" on a Korean page
        # would otherwise produce nonsense).
        def g_src(b: TextBlock) -> str:
            if src_setting == "auto":
                return "auto"
            bl = (b.lang or "").split("-")[0]
            if not bl or bl == src_setting.split("-")[0] or (src_setting == "ja" and bl == "zh"):
                return src_setting
            return "auto"

        # an empty answer for story text would leave the original text in the bubble: ask the same
        # engine again for just those bubbles (Google turns moans and gasps into "yes" / "hello")
        blank = [b for b in blocks if b.id in result and blank_story(b.text, result[b.id])]
        kept: list[str] = []
        if blank:
            LOG.info("ตัวแปล %s เว้นว่างไว้ %d ช่อง (%s) — ถามซ้ำเฉพาะช่องเหล่านั้น", engine.name, len(blank),
                     ", ".join(b.id for b in blank))
            try:
                again = await engine.translate(blank, src, tgt, self.memory, image, note=RETRY_NOTE)
            except Exception as e:
                LOG.info("ถามซ้ำไม่สำเร็จ (%s) — ใช้ Google แปลช่องเหล่านั้น", e)
                again = {}
            for b in blank:
                if b.id in again:
                    result[b.id] = again[b.id]
                else:
                    del result[b.id]         # no second answer: Google below
            # empty twice although told it looks like story text: noise or an ad, leave the artwork alone
            kept = [b.id for b in blank if b.id in result and not result[b.id]]
            if kept:
                LOG.info("ช่อง %s ว่างอีกครั้ง — ถือว่าไม่ใช่เนื้อเรื่อง คงภาพเดิมไว้", ", ".join(kept))
            blank = [b for b in blank if b.id not in result]
        missing = [b for b in blocks if b.id not in result]
        if missing:
            try:
                for sl in {g_src(b) for b in missing}:
                    part = [b for b in missing if g_src(b) == sl]
                    texts = await self.google.translate_texts([b.text for b in part], sl, tgt)
                    result.update({b.id: t for b, t in zip(part, texts)})
                    by_google.update(b.id for b in part)
            except TranslateError as e:
                if any(b not in blank for b in missing):
                    raise
                # only the blanked bubbles are left: keep the page (blocks.holes keeps it out of the cache)
                LOG.warning("แปลช่องที่เว้นว่างไม่สำเร็จ (%s) — ช่อง %s ยังเป็นต้นฉบับ", e, ", ".join(b.id for b in blank))
        # anything that came back still in the source language: retry with auto-detect.
        # Where Google's auto-detect gives the very same text back, asking again won't change it
        # ("你好" is the same in Simplified and Traditional Chinese; OCR noise): not a hole.
        settled = {b.id for b in missing if b.id in by_google and g_src(b) == "auto" and result.get(b.id)}
        bad = [b for b in blocks if untranslated(b.text, result.get(b.id, ""), tgt) and b.id not in settled]
        if bad:
            try:
                texts = await self.google.translate_texts([b.text for b in bad], "auto", tgt)
                for b, t in zip(bad, texts):
                    if t and not untranslated(b.text, t, tgt):
                        result[b.id] = t
                        by_google.add(b.id)
                    elif t and t == result.get(b.id):
                        settled.add(b.id)
            except TranslateError as e:   # keep what we have rather than failing the page
                LOG.info("แปลซ้ำช่องที่ยังไม่ถูกแปลไม่สำเร็จ: %s", e)
        for b in blocks:
            b.translation = result.get(b.id, "")
            b.via = ("google" if b.id in by_google else engine.name) if b.translation else ""
        holes = [b.id for b in blocks if b.id not in kept and b.id not in settled
                 and (blank_story(b.text, b.translation) or untranslated(b.text, b.translation, tgt))]
        try:
            blocks.holes = bool(holes)    # an OCRResult; tests pass plain lists
        except AttributeError:
            pass
        wrote = {b.via for b in blocks if b.via}
        return "google" if wrote == {"google"} else engine.name

    async def aclose(self):
        if hasattr(self.engine, "aclose"):
            await self.engine.aclose()
        try:
            await self.client.aclose()
        except Exception:
            pass
