import os, re, time
import httpx
from dotenv import load_dotenv
load_dotenv()

_API_KEY  = os.getenv("OPENROUTER_API_KEY", "")
_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"


def _env_model(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


def _parse_model_list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _unique_models(models: list[str]) -> list[str]:
    seen = set()
    ordered = []
    for model in models or []:
        if model and model not in seen:
            seen.add(model)
            ordered.append(model)
    return ordered


# Free-tier OpenRouter defaults only. Override via env if you want a different free model.
REASONING_MODEL = _env_model("OPENROUTER_REASONING_MODEL", "meta-llama/llama-3.1-8b-instruct:free")
FAST_MODEL      = _env_model("OPENROUTER_FAST_MODEL", "google/gemma-2-9b-it:free")
CHAT_FALLBACK_MODELS = _unique_models(
    _parse_model_list(
        "OPENROUTER_CHAT_FALLBACK_MODELS",
        [
            FAST_MODEL,
            "mistralai/mistral-7b-instruct:free",
            REASONING_MODEL,
        ],
    )
)

DEFAULT_REASONING_MAX_TOKENS = int(os.getenv("OPENROUTER_REASONING_MAX_TOKENS", "280"))
DEFAULT_FAST_MAX_TOKENS = int(os.getenv("OPENROUTER_FAST_MAX_TOKENS", "180"))


def _is_credit_error(err: str) -> bool:
    s = (err or "").lower()
    return any(token in s for token in ["402", "429", "quota", "exhausted", "credits"])


def _extract_affordable_tokens(err: str) -> int:
    """Parse messages like: 'can only afford 476'. Returns 0 if unavailable."""
    m = re.search(r"can\s+only\s+afford\s+(\d+)", err or "", re.IGNORECASE)
    if not m:
        return 0
    try:
        return max(0, int(m.group(1)))
    except Exception:
        return 0


def _next_token_budget(current: int, err: str, floor: int) -> int:
    affordable = _extract_affordable_tokens(err)
    if affordable > 0:
        # Keep a small cushion below provider-reported ceiling.
        candidate = max(floor, affordable - 24)
        return min(current - 1, candidate) if current > floor else current
    # Generic downshift when provider does not return affordable token hint.
    return max(floor, int(current * 0.75))


def _normalize_messages(messages: list) -> list:
    """Ensure messages comply with OpenRouter schema."""
    normalized = []
    for m in messages or []:
        role = (m or {}).get("role")
        content = (m or {}).get("content")
        if role not in {"system", "user", "assistant"}:
            role = "user"
        if content is None:
            continue
        if not isinstance(content, str):
            content = str(content)
        content = content.strip()
        if not content:
            continue
        normalized.append({"role": role, "content": content})
    return normalized


def _call(model_name: str, messages: list, temperature: float, max_tokens: int) -> str:
    payload = {
        "model": model_name,
        "messages": _normalize_messages(messages),
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    response = httpx.post(
        _BASE_URL,
        headers={
            "Authorization": f"Bearer {_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://kai-backend.fly.dev",
            "X-Title": "KAI Trading Assistant",
        },
        json=payload,
        timeout=60.0,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        body = (e.response.text or "").strip()
        if len(body) > 600:
            body = body[:600] + "..."
        raise RuntimeError(
            f"OpenRouter {e.response.status_code} on model '{model_name}': {body or e}"
        ) from e
    payload = response.json()
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError(f"No choices returned by model '{model_name}'")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not content:
        raise RuntimeError(f"Empty content returned by model '{model_name}'")
    return content


def chat(messages: list, temperature: float = 0.3, max_tokens: int = 600) -> str:
    """Reasoning model — use for trade analysis. Free-tier models only."""
    max_tokens = min(max_tokens, DEFAULT_REASONING_MAX_TOKENS)
    floor_tokens = 96
    for model in _unique_models([REASONING_MODEL, FAST_MODEL] + CHAT_FALLBACK_MODELS):
        budget = max_tokens
        attempts = 0
        try:
            return _call(model, messages, temperature, budget)
        except Exception as e:
            err = str(e)
            while _is_credit_error(err) and attempts < 2 and budget > floor_tokens:
                attempts += 1
                budget = _next_token_budget(budget, err, floor=floor_tokens)
                try:
                    return _call(model, messages, temperature, budget)
                except Exception as retry_err:
                    err = str(retry_err)
                    continue
            if _is_credit_error(err):
                continue
            if "404" in err.lower() or "model" in err.lower() or "not found" in err.lower():
                continue
            raise
    return "AI unavailable. KAI will retry next cycle."


def chat_fast(messages: list, temperature: float = 0.7, max_tokens: int = 400) -> str:
    """Fast model — use for chat replies, trade alerts, non-critical messages."""
    max_tokens = min(max_tokens, DEFAULT_FAST_MAX_TOKENS)
    floor_tokens = 64
    last_error = None
    for model in CHAT_FALLBACK_MODELS:
        budget = max_tokens
        attempts = 0
        try:
            return _call(model, messages, temperature, budget)
        except Exception as e:
            last_error = e
            err = str(e)
            while _is_credit_error(err) and attempts < 2 and budget > floor_tokens:
                attempts += 1
                budget = _next_token_budget(budget, err, floor=floor_tokens)
                try:
                    return _call(model, messages, temperature, budget)
                except Exception as retry_err:
                    last_error = retry_err
                    err = str(retry_err)
                    continue
            if _is_credit_error(err):
                time.sleep(2)
                continue
            if "404" in err.lower() or "model" in err.lower() or "not found" in err.lower():
                continue

    if last_error:
        err = str(last_error)
        if _is_credit_error(err):
            return "Rate limit reached. Try again shortly."
        raise last_error

    return "AI unavailable."
