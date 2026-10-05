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

LOG = logging.getLogger("ghostmanga.translate")

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
- The text comes from OCR. It may contain recognition mistakes, broken vertical columns or
  stray characters. Silently infer the intended text.
- Keep translations about as short as the original; bubbles are small. No notes, no
  explanations, no quotation marks around the text, no romanization in brackets.
- Keep character names, places and special terms consistent with the glossary and the
  earlier context. Transliterate new names naturally and report them in "new_terms".
- Sound effects / onomatopoeia: give a short natural {target} equivalent.
- Text that is not part of the story (page numbers, watermarks, scanlator credits, website
  URLs) -> return an empty string for that id.
- If an image is attached, the red numbers mark bubble ids; use it to tell who is speaking.
{register}
Return ONLY JSON matching: {{"translations":[{{"id":"<id>","text":"<translation>"}}],
"new_terms":[{{"source":"<original>","target":"<translation>"}}]}} with exactly one
entry per input id."""

THAI_REGISTER = """- Thai: choose pronouns and sentence-final particles (ครับ/ค่ะ/นะ/ล่ะ/เหรอ/สิ/เถอะ/วะ/โว้ย)
  from each speaker's personality, gender and the situation; keep them consistent across the
  conversation. Casual speech between friends should sound casual, not textbook-polite.
  Use Thai punctuation habits (no full stop at the end of sentences; keep ! ? … where useful).
"""

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


def lang_name(code: str) -> str:
    return LANG_NAMES.get(code, LANG_NAMES.get(code.split("-")[0], code))


def system_prompt(target: str) -> str:
    return SYSTEM_PROMPT.format(target=lang_name(target),
                                register=THAI_REGISTER if target.startswith("th") else "")


def reading_order(src: str) -> str:
    return "right-to-left manga" if src == "ja" else "top-to-bottom, left-to-right"


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

    def payload(self, blocks: list[TextBlock], src: str) -> str:
        learned = [(k, v) for k, v in self.learned.items() if k not in self.user_terms][-self.SEND_LEARNED:]
        glossary = list(self.user_terms.items()) + learned
        return json.dumps({
            "source_language": lang_name(src) if src and src != "auto" else "auto-detect",
            "reading_order": reading_order(src),
            "glossary": [{"source": k, "target": v} for k, v in glossary],
            "previous_lines": [{"source": s, "translation": t} for s, t in list(self.lines)[-12:]],
            "bubbles": [{"id": b.id, "text": b.text, **({"vertical": True} if b.vertical else {})} for b in blocks],
        }, ensure_ascii=False)

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


def _collect(blocks: list[TextBlock], data: dict) -> tuple[dict[str, str], list]:
    out: dict[str, str] = {}
    items = data.get("translations")
    if isinstance(items, dict):  # {"1": "...", ...}
        items = [{"id": k, "text": v} for k, v in items.items()]
    for it in items or []:
        try:
            out[str(it["id"])] = str(it.get("text") or "").strip()
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


class OpenAICompatible:
    """OpenAI-style /chat/completions: LM Studio, Ollama, OpenAI, DeepSeek, OpenRouter..."""

    def __init__(self, client: httpx.AsyncClient, base_url: str, api_key: str, model: str, local: bool):
        self.client, self.base, self.key, self.model, self.local = client, base_url.rstrip("/"), api_key, model, local
        self.name = "local" if local else "openai"  # must match Settings.engine
        self.json_mode = "json_schema"  # downgraded automatically if the server rejects it
        self.send_temperature = local   # many hosted reasoning models reject temperature
        self._pick_lock = asyncio.Lock()
        self._prep_error: Exception | None = None

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.key:
            h["Authorization"] = f"Bearer {self.key}"
        return h

    async def prepare(self) -> str:
        async with self._pick_lock:
            if self._prep_error is not None:      # already known to be unreachable: fail fast
                raise self._prep_error
            if self.model and not self.local:
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
            if self.model:                         # user picked a model: it just has to be reachable
                return self.model
            chat = [m for m in models if "embed" not in m.lower()]
            if not chat:
                self._prep_error = FatalEngineError("ยังไม่ได้โหลดโมเดลใน LM Studio/Ollama")
                raise self._prep_error
            self.model = chat[0]
            LOG.info("ใช้โมเดล: %s", self.model)
            return self.model

    async def translate(self, blocks, src, tgt, memory: Memory, image: bytes | None):
        model = await self.prepare()
        user_text = memory.payload(blocks, src)
        content: object = user_text
        if image:
            content = [{"type": "text", "text": user_text},
                       {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(image).decode()}}]
        base_body = {
            "model": model,
            "messages": [{"role": "system", "content": system_prompt(tgt)}, {"role": "user", "content": content}],
        }
        for _ in range(6):
            mode, temp = self.json_mode, self.send_temperature
            req = dict(base_body)
            if temp:
                req["temperature"] = 0.3
            if mode == "json_schema":
                req["response_format"] = {"type": "json_schema",
                                          "json_schema": {"name": "page_translation", "strict": True, "schema": RESULT_SCHEMA}}
            elif mode == "json_object":
                req["response_format"] = {"type": "json_object"}
            try:
                r = await self.client.post(f"{self.base}/chat/completions", json=req, headers=self._headers(),
                                           timeout=httpx.Timeout(300 if self.local else 120, connect=10))
            except httpx.ConnectError as e:
                raise TranslateError(f"เชื่อมต่อ {self.base} ไม่ได้") from e
            if r.status_code in (400, 422):
                low = r.text.lower()
                if temp and "temperature" in low:
                    if self.send_temperature == temp:
                        self.send_temperature = False
                    continue
                if mode != "none" and any(k in low for k in ("response_format", "json", "schema")):
                    nxt = "json_object" if mode == "json_schema" else "none"
                    if self.json_mode == mode:  # another worker may already have downgraded
                        self.json_mode = nxt
                        LOG.info("server ไม่รองรับ %s, ใช้โหมด %s", mode, nxt)
                    continue
            if r.status_code >= 400:
                raise _http_error(self.base, r)
            choice = r.json()["choices"][0]
            msg = choice.get("message") or {}
            if choice.get("finish_reason") == "length":
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

    async def translate(self, blocks, src, tgt, memory: Memory, image: bytes | None):
        parts = [{"text": memory.payload(blocks, src)}]
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

    async def translate(self, blocks, src, tgt, memory: Memory, image: bytes | None):
        anthropic = self._anthropic
        content = [{"type": "text", "text": memory.payload(blocks, src)}]
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
            return OpenAICompatible(self.client, s.local_base_url, "", s.local_model, local=True)
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
        if e.name == "claude":
            parts.append(s.claude_effort)
        if e is not self.google:
            gl = "\n".join(f"{k}={v}" for k, v in sorted(self.memory.user_terms.items()))
            parts += [hashlib.sha1(gl.encode()).hexdigest()[:10], "img" if s.send_image_to_ai else ""]
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
        """Fills block.translation. `src_setting` is the user's choice ('auto' or a code);
        `hint` is the page's detected language (only used to brief LLMs).
        Returns the name of the engine that produced the translations."""
        if not blocks:
            return self.engine.name
        if not self._prepared:
            await self.prepare()
        used = self.engine.name
        result: dict[str, str] = {}
        engine = self.engine
        if engine is not self.google and time.monotonic() >= self.benched_until:
            try:
                result = await engine.translate(blocks, src_setting if src_setting != "auto" else hint, tgt,
                                                self.memory, image if self.s.send_image_to_ai else None)
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

        missing = [b for b in blocks if b.id not in result]
        if missing:
            for sl in {g_src(b) for b in missing}:
                part = [b for b in missing if g_src(b) == sl]
                texts = await self.google.translate_texts([b.text for b in part], sl, tgt)
                result.update({b.id: t for b, t in zip(part, texts)})
            if len(missing) == len(blocks):
                used = "google"
        # anything that came back still in the source language: retry with auto-detect
        bad = [b for b in blocks if untranslated(b.text, result.get(b.id, ""), tgt)
               and not (b in missing and g_src(b) == "auto")]       # Google-auto already tried that
        if bad:
            try:
                texts = await self.google.translate_texts([b.text for b in bad], "auto", tgt)
                for b, t in zip(bad, texts):
                    if t and not untranslated(b.text, t, tgt):
                        result[b.id] = t
                if len(bad) == len(blocks):
                    used = "google"     # nothing of the engine's output survived: don't cache under its key
            except TranslateError as e:   # keep what we have rather than failing the page
                LOG.info("แปลซ้ำช่องที่ยังไม่ถูกแปลไม่สำเร็จ: %s", e)
        for b in blocks:
            b.translation = result.get(b.id, "")
        return used

    async def aclose(self):
        if hasattr(self.engine, "aclose"):
            await self.engine.aclose()
        try:
            await self.client.aclose()
        except Exception:
            pass
