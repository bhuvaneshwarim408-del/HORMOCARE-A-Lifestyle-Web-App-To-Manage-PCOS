# Run from the hormocare-main folder:  python test_groq.py
import os, requests
from dotenv import load_dotenv
load_dotenv()
key = os.getenv("GROQ_API_KEY")
print("Key loaded:", bool(key))
r = requests.post("https://api.groq.com/openai/v1/chat/completions",
    headers={"Authorization": f"Bearer {key}"},
    json={"model": "openai/gpt-oss-120b",
          "messages": [{"role": "user", "content": "Say hi"}]}, timeout=15)
print("Status:", r.status_code)
print(r.text[:300])