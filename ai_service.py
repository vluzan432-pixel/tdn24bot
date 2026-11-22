"""
ai_service.py — тонкий async-клієнт до Google Gemini API (безкоштовний рівень).

Працює через aiohttp (він уже є в requirements.txt) — нових залежностей не треба.

Змінні середовища (Render → Environment):
    GEMINI_API_KEY   — ключ з https://aistudio.google.com (Get API key)
    GEMINI_MODEL     — модель для коротких записів (за замовч. "gemini-2.5-flash")
    GEMINI_MODEL_LONG— модель для звіту/тез (за замовч. = GEMINI_MODEL)

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
import re

import aiohttp

log = logging.getLogger("schedule-bot.ai")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL_FAST = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
MODEL_LONG = os.environ.get("GEMINI_MODEL_LONG", MODEL_FAST)

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_semaphore = asyncio.Semaphore(2)


class AIUnavailable(Exception):
    """ШІ тимчасово недоступний (ліміт, мережа, порожня відповідь)."""


def ai_configured() -> bool:
    return bool(GEMINI_API_KEY)


async def generate(
    system: str,
    prompt: str,
    *,
    long: bool = False,
    json_mode: bool = False,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    retries: int = 3,
) -> str:
    """Повертає текст відповіді моделі або кидає AIUnavailable."""
    if not GEMINI_API_KEY:
        raise AIUnavailable("GEMINI_API_KEY не задано")

    model = MODEL_LONG if long else MODEL_FAST
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    # "Роздуми" моделі їдять ліміт вихідних токенів і ціну ліміту — для наших
    # задач вони не потрібні. Якщо модель такого параметра не підтримує (HTTP 400),
    # нижче один раз повторюємо запит без нього.
    body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}

    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=120)
    last_error = "невідома помилка"

    async with _semaphore:
        for attempt in range(retries):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(API_URL.format(model=model), json=body, headers=headers) as resp:
                        data = await resp.json(content_type=None)
                        if resp.status == 200:
                            text = _extract_text(data)
                            if text:
                                return text
                            last_error = "порожня відповідь (можливо, спрацював фільтр безпеки)"
                            break  # повтор тут не допоможе
                        last_error = f"HTTP {resp.status}"
                        if resp.status == 400 and "thinkingConfig" in body["generationConfig"]:
                            del body["generationConfig"]["thinkingConfig"]
                            continue
                        if resp.status not in (429, 500, 502, 503, 504):
                            log.error("Gemini помилка %s: %s", resp.status, str(data)[:300])
                            break
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = f"мережа: {e.__class__.__name__}"
            await asyncio.sleep(4 * (attempt + 1))

    raise AIUnavailable(last_error)


def _extract_text(data: dict) -> str:
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip()
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
    text = await generate(system, full_name, temperature=0.0, max_tokens=100)
    return text.strip().strip("\"'«».")
