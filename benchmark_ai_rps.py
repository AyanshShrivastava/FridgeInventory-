#!/usr/bin/env python3
"""
FridgeInventory - AI Engines Live Requests/Second (RPS) & Throughput Benchmark
=============================================================================
Performs live empirical load testing across all 3 configured AI providers:
  1. Groq LPU (High-Speed LPU inference - Qwen / Llama)
  2. Google Gemini (Google GenAI)
  3. OpenRouter (Universal Aggregator)

Measures and records:
  - Latency: Min, Average, Median (P50), P95, Max (in ms)
  - Throughput: Real-world Measured Requests Per Second (RPS = completed / total_time_sec)
  - Concurrency: Parallel burst handling
  - Rate Limits: Free tier limits vs Paid tier maximum capacity
  - Tri-Engine Parallel Burst: Running all three AIs concurrently
"""

import os
import sys
import time
import json
import statistics
import concurrent.futures
from typing import Dict, List, Any, Optional

import requests
import certifi

BASE_URL = os.getenv("TARGET_URL", "https://fridge-a.onrender.com")
BENCHMARK_ENDPOINT = f"{BASE_URL}/api/benchmark/ai-throughput"

SESSION = requests.Session()
SESSION.verify = certifi.where()


def send_probe(engine: str, prompt: str = "Classify food: 500ml milk. JSON: category, shelf_life_days.", model: Optional[str] = None) -> Dict[str, Any]:
    """Sends a single diagnostic request to the live Render AI throughput benchmark endpoint."""
    t0 = time.perf_counter()
    params = {"engine": engine, "prompt": prompt}
    if model:
        params["model"] = model
    try:
        resp = SESSION.get(BENCHMARK_ENDPOINT, params=params, timeout=40)
        total_rtt_ms = round((time.perf_counter() - t0) * 1000, 2)
        if resp.status_code == 200:
            data = resp.json()
            eng_result = data.get("results", {}).get(engine, {})
            return {
                "http_status": 200,
                "total_rtt_ms": total_rtt_ms,
                "server_latency_ms": eng_result.get("latency_ms", total_rtt_ms),
                "model": eng_result.get("model", "unknown"),
                "status": eng_result.get("status", "unknown"),
                "completion_tokens": eng_result.get("completion_tokens"),
                "error": eng_result.get("error"),
                "sample": eng_result.get("sample"),
            }
        else:
            return {
                "http_status": resp.status_code,
                "total_rtt_ms": total_rtt_ms,
                "server_latency_ms": None,
                "model": "unknown",
                "status": "http_error",
                "error": f"HTTP {resp.status_code}: {resp.text[:120]}",
            }
    except Exception as exc:
        total_rtt_ms = round((time.perf_counter() - t0) * 1000, 2)
        return {
            "http_status": 0,
            "total_rtt_ms": total_rtt_ms,
            "server_latency_ms": None,
            "model": "unknown",
            "status": "client_exception",
            "error": str(exc),
        }


def calculate_metrics(latencies: List[float]) -> Dict[str, float]:
    """Computes min, max, mean, p50, and p95 from a list of latencies in ms."""
    if not latencies:
        return {"min_ms": 0, "max_ms": 0, "mean_ms": 0, "p50_ms": 0, "p95_ms": 0}
    sorted_l = sorted(latencies)
    n = len(sorted_l)
    def p(pct: float) -> float:
        idx = int(round(pct * (n - 1)))
        return sorted_l[min(max(idx, 0), n - 1)]
    return {
        "min_ms": round(sorted_l[0], 2),
        "max_ms": round(sorted_l[-1], 2),
        "mean_ms": round(statistics.mean(sorted_l), 2),
        "p50_ms": round(p(0.50), 2),
        "p95_ms": round(p(0.95), 2),
    }


def run_concurrency_test(engine: str, concurrency: int = 5, model: Optional[str] = None) -> Dict[str, Any]:
    """Fires concurrent requests in parallel to measure burst throughput and RPS."""
    print(f"\n🚀 Launching {concurrency} concurrent requests for '{engine}' (model={model or 'default'})...")
    wall_start = time.perf_counter()

    results: List[Dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(
                send_probe,
                engine=engine,
                prompt=f"Classify item #{i}: fresh produce #{i}. Return JSON: is_food, category, shelf_life_days.",
                model=model,
            )
            for i in range(1, concurrency + 1)
        ]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    wall_elapsed_sec = time.perf_counter() - wall_start
    successful = [r for r in results if r["status"] == "success" and r["http_status"] == 200]
    failed = [r for r in results if r not in successful]

    rtt_latencies = [r["total_rtt_ms"] for r in successful]
    server_latencies = [r["server_latency_ms"] for r in successful if r.get("server_latency_ms") is not None]

    measured_rps = round(len(successful) / wall_elapsed_sec, 2) if wall_elapsed_sec > 0 else 0.0

    print(f"   Completed {len(successful)}/{concurrency} successful in {wall_elapsed_sec:.2f}s -> {measured_rps} RPS")
    if successful:
        p50 = statistics.median(rtt_latencies) if rtt_latencies else 0
        avg_srv = statistics.mean(server_latencies) if server_latencies else 0
        print(f"   Median RTT: {p50:.1f}ms | Avg Server AI Latency: {avg_srv:.1f}ms")

    return {
        "engine": engine,
        "model_requested": model or "default",
        "concurrency_sent": concurrency,
        "successful_count": len(successful),
        "failed_count": len(failed),
        "wall_elapsed_sec": round(wall_elapsed_sec, 3),
        "measured_rps": measured_rps,
        "client_rtt_metrics": calculate_metrics(rtt_latencies),
        "server_inference_metrics": calculate_metrics(server_latencies),
        "models_observed": list(set(r.get("model", "") for r in successful)),
        "errors": [r.get("error") for r in failed if r.get("error")],
        "raw_responses": results,
    }


def main():
    print("=" * 75)
    print("FRIDGEINVENTORY LIVE AI REQUESTS-PER-SECOND (RPS) EMPIRICAL BENCHMARK")
    print(f"Production Target: {BENCHMARK_ENDPOINT}")
    print(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("=" * 75)

    # 1. Warm-up & health verification
    print("\n--- Phase 1: Health & Connectivity Verification ---")
    try:
        h = SESSION.get(f"{BASE_URL}/health", timeout=10).json()
        print(f"Service: {h.get('service')} | Status: {h.get('status')}")
        print(f"Groq Configured: {h.get('groq_configured')}")
        print(f"Gemini Configured: {h.get('gemini_configured')}")
        print(f"OpenRouter Configured: {h.get('openrouter_configured')}")
    except Exception as e:
        print(f"Warning: Health check failed: {e}")

    report_data = {
        "timestamp_utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "target_url": BASE_URL,
        "benchmarks": {},
    }

    # 2. Benchmark Groq LPU (Concurrency 5)
    print("\n--- Phase 2: Benchmarking Groq LPU (5 Concurrent Requests) ---")
    groq_5 = run_concurrency_test("groq", concurrency=5)
    report_data["benchmarks"]["groq_5_concurrent"] = groq_5

    # 3. Benchmark Groq LPU (Concurrency 10 - High Throughput Burst)
    print("\n--- Phase 3: Benchmarking Groq LPU (10 Concurrent Burst Requests) ---")
    groq_10 = run_concurrency_test("groq", concurrency=10)
    report_data["benchmarks"]["groq_10_concurrent"] = groq_10

    # 4. Benchmark Google Gemini (3 Concurrent Requests)
    print("\n--- Phase 4: Benchmarking Google Gemini (3 Concurrent Requests) ---")
    gemini_3 = run_concurrency_test("gemini", concurrency=3)
    report_data["benchmarks"]["gemini_3_concurrent"] = gemini_3

    # 5. Benchmark OpenRouter (3 Concurrent Requests)
    print("\n--- Phase 5: Benchmarking OpenRouter (3 Concurrent Requests) ---")
    openrouter_3 = run_concurrency_test("openrouter", concurrency=3)
    report_data["benchmarks"]["openrouter_3_concurrent"] = openrouter_3

    # 6. Benchmark Tri-Engine Parallel Burst (Groq + Gemini + OpenRouter simultaneously)
    print("\n--- Phase 6: Tri-Engine Parallel Load (All 3 AIs in Parallel) ---")
    t_tri_start = time.perf_counter()
    tri_results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        f_groq = executor.submit(send_probe, "groq", "Classify food: fresh spinach")
        f_gemini = executor.submit(send_probe, "gemini", "Classify food: fresh spinach")
        f_openrouter = executor.submit(send_probe, "openrouter", "Classify food: fresh spinach")
        tri_results["groq"] = f_groq.result()
        tri_results["gemini"] = f_gemini.result()
        tri_results["openrouter"] = f_openrouter.result()
    tri_wall_sec = time.perf_counter() - t_tri_start
    print(f"   Tri-Engine parallel burst finished in {tri_wall_sec:.2f}s")
    for eng_name, res in tri_results.items():
        print(f"     * {eng_name.upper():<12}: status={res.get('status')}, server_latency={res.get('server_latency_ms')}ms, model={res.get('model')}")

    report_data["benchmarks"]["tri_engine_parallel_burst"] = {
        "wall_elapsed_sec": round(tri_wall_sec, 3),
        "combined_requests_fired": 3,
        "combined_rps": round(3 / tri_wall_sec, 2) if tri_wall_sec > 0 else 0,
        "individual_results": tri_results,
    }

    # Save raw JSON
    output_json_path = os.path.join(os.path.dirname(__file__), "ai_rps_benchmark_results.json")
    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)
    print(f"\n✅ Benchmark completed successfully! Saved raw results to {output_json_path}")


if __name__ == "__main__":
    main()
