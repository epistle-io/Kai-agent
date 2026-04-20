import os, re, time
from groq import Groq
from dotenv import load_dotenv
load_dotenv()

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# deepseek-r1-distill-llama-70b: reasoning model — thinks step-by-step before answering
# Use for trade analysis where quality matters most
REASONING_MODEL = "deepseek-r1-distill-llama-70b"
# llama-3.1-8b-instant: fast & cheap — use for chat, alerts, non-critical calls
FAST_MODEL      = "llama-3.1-8b-instant"
# fallback if reasoning model hits rate limit
FALLBACK_MODEL  = "llama-3.3-70b-versatile"


def _call(model: str, messages: list, temperature: float, max_tokens: int) -> str:
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content


def _strip_think(raw: str) -> str:
    """Remove <think>...</think> reasoning blocks output by deepseek-r1."""
    return re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()


def chat(messages: list, temperature: float = 0.3, max_tokens: int = 600) -> str:
    """Reasoning model — use for trade analysis. Falls back to 70B if rate limited."""
    for model in [REASONING_MODEL, FALLBACK_MODEL]:
        try:
            raw = _call(model, messages, temperature, max_tokens)
            cleaned = _strip_think(raw)
            return cleaned if cleaned else raw
        except Exception as e:
            err = str(e)
            if "rate_limit_exceeded" in err or "429" in err:
                if model == REASONING_MODEL:
                    continue  # try fallback
                # Both rate limited — wait and retry reasoning model
                time.sleep(10)
                try:
                    raw = _call(REASONING_MODEL, messages, temperature, max_tokens)
                    cleaned = _strip_think(raw)
                    return cleaned if cleaned else raw
                except:
                    return "Rate limit reached. KAI will retry next cycle."
            raise
    return "AI unavailable."


def chat_fast(messages: list, temperature: float = 0.7, max_tokens: int = 400) -> str:
    """Fast 8B model — use for chat replies, trade alerts, non-critical messages."""
    try:
        return _call(FAST_MODEL, messages, temperature, max_tokens)
    except Exception as e:
        err = str(e)
        if "rate_limit_exceeded" in err or "429" in err:
            time.sleep(5)
            try:
                return _call(FAST_MODEL, messages, temperature, max_tokens)
            except:
                return "Rate limit reached. Try again shortly."
        raise
