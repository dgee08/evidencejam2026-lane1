import os, time
from openai import OpenAI

client = OpenAI(base_url=os.environ["BRINE_URL"], api_key=os.environ["BRINE_API_KEY"],
                timeout=120, max_retries=0)
MODEL = os.environ["BRINE_MODEL"]
ask = 'Return only JSON: {"ok": true}.'

variants = [
    ("default", ask, {}),
    ("kwarg", ask, {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}),
    ("prompt", "/no_think\n" + ask, {}),
]
for name, content, extra in variants:
    t = time.time()
    try:
        r = client.chat.completions.create(model=MODEL, max_tokens=500, temperature=0,
            messages=[{"role": "user", "content": content}], **extra)
        c = r.choices[0]
        print(f"{name}: {time.time()-t:.1f}s, {r.usage.completion_tokens} tokens, finish={c.finish_reason}")
        print("  content:", (c.message.content or "")[:150].replace("\n", " "))
        print("  reasoning field:", str(getattr(c.message, "reasoning_content", None))[:80])
    except Exception as e:
        print(f"{name}: failed {type(e).__name__} {str(e)[:150]}")