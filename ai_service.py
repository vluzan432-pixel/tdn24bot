"""
ai_service.py — тонкий async-клієнт до Google Gemini API (безкоштовний рівень).

Працює через aiohttp (він уже є в requirements.txt) — нових залежностей не треба.

Змінні середовища (Render → Environment):
    GEMINI_API_KEY   — ключ з https://aistudio.google.com (Get API key)
    GEMINI_MODEL     — модель для коротких записів (за замовч. "gemini-3.5-flash-lite")
    GEMINI_MODEL_LONG— модель для звіту/тез (за замовч. = GEMINI_MODEL)
    GEMINI_MODEL_FALLBACK — (необов'язково) запасні моделі через кому на випадок 503/429/404

Безкоштовний ліміт ОДИН на весь ключ (тобто на всю групу разом), тому:
  * одночасно виконуємо не більше 2 запитів (Semaphore);
  * на 429/5xx робимо кілька повторів із наростаючою паузою;
  * якщо все одно не вийшло — кидаємо AIUnavailable, а хендлер ввічливо
    каже студенту "спробуй за хвилину", бот не падає.
"""

import asyncio
import json
import logging
import os
import random
import re
import time

import aiohttp

log = logging.getLogger("schedule-bot.ai")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL_FAST = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
MODEL_LONG = os.environ.get("GEMINI_MODEL_LONG", MODEL_FAST)
# Запасні моделі через кому: якщо основна перевантажена (503), вичерпала ліміт (429) або недоступна,
# запит автоматично йде на наступну. Приклад:
#   GEMINI_MODEL_FALLBACK=gemini-3.1-flash-lite,gemma-4-31b-it,gemini-2.5-flash-lite
MODEL_FALLBACKS = [m.strip() for m in os.environ.get("GEMINI_MODEL_FALLBACK", "").split(",") if m.strip()]

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_semaphore = asyncio.Semaphore(2)
_exhausted: dict = {}  # модель -> час (monotonic), до якого вона вважається вичерпаною за добу


class AIUnavailable(Exception):
    """ШІ тимчасово недоступний (ліміт, мережа, порожня відповідь)."""


def ai_configured() -> bool:
    return bool(GEMINI_API_KEY)


def _model_chain(long: bool) -> list:
    primary = MODEL_LONG if long else MODEL_FAST
    chain = [primary] + [m for m in MODEL_FALLBACKS if m != primary]
    now = time.monotonic()
    alive = [m for m in chain if _exhausted.get(m, 0) <= now]
    return alive or chain


def _build_body(model: str, system: str, prompt: str, temperature: float, max_tokens: int,
                json_mode: bool, thinking) -> dict:
    # запас на «роздуми» моделі: вони рахуються в maxOutputTokens і без запасу
    # відповідь виходить порожньою (finishReason = MAX_TOKENS)
    config = {"temperature": temperature, "maxOutputTokens": max_tokens + 2048}
    if model.startswith("gemma"):
        # Gemma: без systemInstruction, JSON-режиму й thinkingConfig — системний текст
        # додаємо на початок запиту (промти й так вимагають «ЛИШЕ JSON»).
        return {
            "contents": [{"role": "user", "parts": [{"text": f"{system}\n\n{prompt}"}]}],
            "generationConfig": config,
        }
    if json_mode:
        config["responseMimeType"] = "application/json"
    if thinking:
        config["thinkingConfig"] = thinking
    return {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": config,
    }


async def generate(
    system: str,
    prompt: str,
    *,
    long: bool = False,
    json_mode: bool = False,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    retries: int = 6,
) -> str:
    """Повертає текст відповіді моделі або кидає AIUnavailable.

    Спроби чергуються по ланцюжку моделей (основна → запасні). Нова/«preview» модель
    інколи відповідає 503 «high demand» — тоді просто беремо наступну замість довгого очікування."""
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY не задано у змінних середовища (Render → Environment)")
        raise AIUnavailable("GEMINI_API_KEY не задано")

    chain = _model_chain(long)
    attempts = max(retries, 2 * len(chain))
    # Різні покоління моделей керують «роздумами» по-різному: пробуємо рівень "low",
    # потім бюджет 0, потім без параметра — перехід на наступний варіант при HTTP 400.
    thinking_options = {m: [{"thinkingLevel": "low"}, {"thinkingBudget": 0}, None] for m in chain}
    thinking_now = {m: thinking_options[m].pop(0) for m in chain}

    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=90)
    last_error = "невідома помилка"
    idx = 0

    async with _semaphore:
        for attempt in range(attempts):
            model = chain[idx % len(chain)]
            body = _build_body(model, system, prompt, temperature, max_tokens, json_mode, thinking_now[model])
            wait = 0.0
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(API_URL.format(model=model), json=body, headers=headers) as resp:
                        data = await resp.json(content_type=None)
                        if resp.status == 200:
                            text = _extract_text(data)
                            if text:
                                return text
                            last_error = "порожня відповідь (можливо, спрацював фільтр безпеки)"
                            log.error("Gemini порожня відповідь, модель %s: %s", model, str(data)[:600])
                            idx += 1
                            continue
                        last_error = f"HTTP {resp.status}"
                        log.warning("Gemini %s (модель %s): %s", resp.status, model, str(data)[:300])
                        blob = str(data).lower()
                        if resp.status == 400 and thinking_now.get(model) is not None and not model.startswith("gemma"):
                            opts = thinking_options[model]
                            thinking_now[model] = opts.pop(0) if opts else None
                            continue  # та сама модель, інший формат «роздумів»
                        if resp.status == 429 and ("perday" in blob or "per day" in blob or "daily" in blob):
                            _exhausted[model] = time.monotonic() + 3600  # добовий ліміт — не чіпаємо годину
                            last_error = "HTTP 429 (денний ліміт)"
                            idx += 1
                            continue
                        if resp.status == 404:
                            _exhausted[model] = time.monotonic() + 3600
                            log.error("Gemini: модель %s не знайдена (перевір назву у змінних Render)", model)
                            idx += 1
                            continue
                        # 429 (за хвилину), 5xx, інші 4xx — пробуємо наступну модель з короткою паузою
                        wait = 1.5 + random.random() * 1.5 if resp.status in (429, 500, 502, 503, 504) else 0
                        idx += 1
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = f"мережа: {e.__class__.__name__}"
                idx += 1
                wait = 2.0
            # після повного кола по моделях трохи довша пауза
            if idx and idx % len(chain) == 0:
                wait += 3.0
            if wait:
                await asyncio.sleep(wait)

    log.error("Gemini недоступний: %s", last_error)
    raise AIUnavailable(last_error)


def _extract_text(data: dict) -> str:
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    except (KeyError, IndexError, TypeError):
        return ""


def parse_json(text: str):
    """Достає JSON навіть якщо модель обгорнула його в ```json ... ```."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise AIUnavailable(f"некоректний JSON від ШІ: {e}")


async def generate_json(system: str, prompt: str, **kwargs):
    text = await generate(system, prompt, json_mode=True, **kwargs)
    return parse_json(text)


# --------------------------------------------------------------- прості задачі

async def to_genitive_fio(full_name: str) -> str:
    """'Лузан Владислав Олександрович' -> 'Лузана Владислава Олександровича'."""
    system = (
        "Ти — коректор української мови. Користувач дає ПІБ у називному відмінку. "
        "Поверни ЛИШЕ це ж ПІБ у родовому відмінку (хто? кого?), без пояснень, "
        "лапок і крапки. Зберігай порядок слів: прізвище, ім'я, по батькові."
    )
    text = await generate(system, full_name, temperature=0.0, max_tokens=300)
    return text.strip().strip("\"'«».")
