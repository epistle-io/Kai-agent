import os, time
from groq import Groq
from dotenv import load_dotenv
load_dotenv()

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# llama-3.1-8b-instant: 500k tokens/day, 6k/min — very generous
# llama-3.3-70b-versatile: 100k tokens/day — use as fallback only
PRIMARY_MODEL  = "llama-3.1-8b-instant"
FALLBACK_MODEL = "llama-3.3-70b-versatile"

def chat(messages: list, temperature: float = 0.7, max_tokens: int = 512) -> str:
    for model in [PRIMARY_MODEL, FALLBACK_MODEL]:
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return response.choices[0].message.content
        except Exception as e:
            err = str(e)
            if "rate_limit_exceeded" in err or "429" in err:
                if model == PRIMARY_MODEL:
                    continue  # try fallback model
                # Both rate limited — wait briefly and try primary again
                time.sleep(10)
                try:
                    r = client.chat.completions.create(
                        model=PRIMARY_MODEL,
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                    return r.choices[0].message.content
                except:
                    return "Rate limit reached. KAI will retry next cycle."
            raise
    return "AI unavailable."
