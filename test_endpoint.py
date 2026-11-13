import os
from openai import OpenAI

client = OpenAI(base_url=os.environ["BRINE_URL"], api_key=os.environ["BRINE_API_KEY"])
resp = client.chat.completions.create(
    model=os.environ["BRINE_MODEL"],
    messages=[{"role": "user", "content": "Say hello in one sentence."}],
)
print(resp.choices[0].message.content)