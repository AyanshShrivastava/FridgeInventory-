#!/usr/bin/env python3
"""
Verify all user-provided models on live Render deployment for:
1. Groq
2. Google Gemini
3. OpenRouter
"""

import urllib.request
import json
import ssl

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

BASE_URL = "https://fridge-a.onrender.com/api/benchmark/ai-throughput"

groq_candidates = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "llama-4-scout",
    "gpt-oss-120b",
    "qwen3-32b",
    "qwen/qwen3.8-27b",
    "gemma-2-9b-it",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "allam-2-7b",
]

gemini_candidates = [
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemma-4-26b-a4b-it",
    "gemma-4-31b-it",
]

openrouter_candidates = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3.5-lightning:free",
    "poolside/laguna-s-2.1:free",
    "cohere/north-mini-code:free",
    "poolside/laguna-xs-2.1:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "qwen/qwen3.8-27b:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "minimax/minimax-m3.1-flash-20261019:free",
    "minimax/minimax-m3.1-flash:free",
    "respan/span-01-lite:free",
]

def test_model(engine, model):
    url = f"{BASE_URL}?engine={engine}&model={urllib.parse.quote(model)}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, context=ctx, timeout=25) as resp:
            data = json.loads(resp.read())
            res = data.get("results", {}).get(engine, {})
            status = res.get("status")
            lat = res.get("latency_ms")
            err = res.get("error")
            return status, lat, err
    except Exception as e:
        return "network_error", None, str(e)

print("=" * 60)
print("VERIFYING GROQ CANDIDATES")
print("=" * 60)
groq_verified = []
for m in groq_candidates:
    st, lat, err = test_model("groq", m)
    print(f"Groq {m:<25}: status={st:<10} latency={str(lat)+'ms':<10} err={err}")
    if st == "success":
        groq_verified.append(m)

print("\n" + "=" * 60)
print("VERIFYING GEMINI CANDIDATES")
print("=" * 60)
gemini_verified = []
for m in gemini_candidates:
    st, lat, err = test_model("gemini", m)
    print(f"Gemini {m:<25}: status={st:<10} latency={str(lat)+'ms':<10} err={err}")
    if st == "success":
        gemini_verified.append(m)

print("\n" + "=" * 60)
print("VERIFYING OPENROUTER CANDIDATES")
print("=" * 60)
openrouter_verified = []
for m in openrouter_candidates:
    st, lat, err = test_model("openrouter", m)
    print(f"OpenRouter {m:<35}: status={st:<10} latency={str(lat)+'ms':<10} err={err}")
    if st == "success":
        openrouter_verified.append(m)

print("\n" + "=" * 60)
print("SUMMARY OF VERIFIED WORKING MODELS")
print("=" * 60)
print("Groq verified:", groq_verified)
print("Gemini verified:", gemini_verified)
print("OpenRouter verified:", openrouter_verified)
