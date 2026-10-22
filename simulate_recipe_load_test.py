#!/usr/bin/env python3
"""
FridgeInventory - End-to-End Multi-Account AI Rescue Recipe Load & Throughput Benchmark
======================================================================================
Automates:
  1. Creating independent family fridge accounts (POST /api/auth/create)
  2. Obtaining session cookies for each virtual user
  3. Simulating users typing real expiring ingredients into the Rescue Recipe chef (POST /rescue)
  4. Executing parallel concurrent requests across all accounts
  5. Recording the full generated recipes (Name, Hindi Name, Ingredients, Steps, Chef Tips)
  6. Measuring real-world combined Requests-Per-Second (RPS) throughput and latency
"""

import os
import sys
import time
import uuid
import json
import statistics
import concurrent.futures
from typing import Dict, List, Any

import requests
import certifi

BASE_URL = os.getenv("TARGET_URL", "https://fridge-a.onrender.com")

RECIPE_TEST_PROMPTS = [
    {
        "family": "Sharma Family",
        "ingredients": ["ripe mango", "chilled milk", "sugar"],
        "dish_hint": "Mango Summer Delight"
    },
    {
        "family": "Verma Family",
        "ingredients": ["fresh palak", "paneer cubes", "garlic"],
        "dish_hint": "Healthy Greens & Protein"
    },
    {
        "family": "Gupta Family",
        "ingredients": ["leftover boiled rice", "mustard seeds", "curry leaves", "lemon"],
        "dish_hint": "Quick Lemon Rice"
    },
    {
        "family": "Kapoor Family",
        "ingredients": ["overripe bananas", "atta flour", "cardamom"],
        "dish_hint": "Banana Wheat Pancakes"
    },
    {
        "family": "Mehta Family",
        "ingredients": ["chopped potatoes", "green peas", "cumin seeds"],
        "dish_hint": "Classic Aloo Matar"
    },
]


def create_account(session: requests.Session, fridge_id: str, pin: str = "1234") -> Dict[str, Any]:
    """Creates a fresh family fridge account and establishes an authenticated session."""
    url = f"{BASE_URL}/api/auth/create"
    t0 = time.perf_counter()
    try:
        resp = session.post(url, json={"fridge_id": fridge_id, "pin": pin}, timeout=15)
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        if resp.status_code == 200:
            data = resp.json()
            return {
                "success": True,
                "fridge_id": data.get("fridge_id", fridge_id),
                "create_latency_ms": elapsed_ms,
            }
        else:
            return {
                "success": False,
                "error": f"HTTP {resp.status_code}: {resp.text[:120]}",
                "create_latency_ms": elapsed_ms,
            }
    except Exception as exc:
        return {"success": False, "error": str(exc), "create_latency_ms": 0}


def query_rescue_recipe(session: requests.Session, fridge_id: str, ingredients: List[str]) -> Dict[str, Any]:
    """Sends expiring ingredients to /rescue to generate an AI zero-waste recipe."""
    url = f"{BASE_URL}/rescue"
    t0 = time.perf_counter()
    try:
        resp = session.post(
            url,
            json={"items": ingredients, "fridge_id": fridge_id},
            timeout=45,
        )
        total_time_ms = round((time.perf_counter() - t0) * 1000, 2)
        if resp.status_code == 200:
            recipe_data = resp.json()
            return {
                "status": "success",
                "http_code": 200,
                "latency_ms": total_time_ms,
                "recipe": recipe_data,
            }
        else:
            return {
                "status": "http_error",
                "http_code": resp.status_code,
                "latency_ms": total_time_ms,
                "error": f"HTTP {resp.status_code}: {resp.text[:160]}",
            }
    except Exception as exc:
        total_time_ms = round((time.perf_counter() - t0) * 1000, 2)
        return {
            "status": "client_exception",
            "http_code": 0,
            "latency_ms": total_time_ms,
            "error": str(exc),
        }


def user_flow(user_idx: int, test_case: Dict[str, Any]) -> Dict[str, Any]:
    """Simulates an entire user: registers account -> submits expiring items -> gets recipe."""
    session = requests.Session()
    session.verify = certifi.where()

    unique_suffix = uuid.uuid4().hex[:6]
    fridge_id = f"Family{user_idx}{unique_suffix}"
    
    # 1. Create family fridge
    acct = create_account(session, fridge_id=fridge_id)
    if not acct["success"]:
        return {
            "user_idx": user_idx,
            "family": test_case["family"],
            "account_created": False,
            "error": acct.get("error"),
            "recipe_result": None,
        }

    fridge_id = acct["fridge_id"]

    # 2. Submit items to recipe chef
    recipe_res = query_rescue_recipe(session, fridge_id=fridge_id, ingredients=test_case["ingredients"])

    return {
        "user_idx": user_idx,
        "family": test_case["family"],
        "fridge_id": fridge_id,
        "ingredients_typed": test_case["ingredients"],
        "account_created": True,
        "account_create_ms": acct["create_latency_ms"],
        "recipe_result": recipe_res,
    }


def main():
    print("=" * 80)
    print("FRIDGEINVENTORY: REAL-WORLD MULTI-ACCOUNT RECIPE LOAD & RPS BENCHMARK")
    print(f"Target URL: {BASE_URL}")
    print(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("=" * 80)

    print("\n[Step 1] Initializing 5 concurrent family accounts and submitting recipes...")
    start_wall = time.perf_counter()

    results: List[Dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(RECIPE_TEST_PROMPTS)) as executor:
        futures = [
            executor.submit(user_flow, idx + 1, test_case)
            for idx, test_case in enumerate(RECIPE_TEST_PROMPTS)
        ]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    total_wall_sec = time.perf_counter() - start_wall

    print(f"\n[Step 2] All {len(RECIPE_TEST_PROMPTS)} user requests finished in {total_wall_sec:.2f} seconds.")
    print("=" * 80)
    print("GENERATED RECIPE ANSWERS & ENGINE ATTRIBUTIONS:")
    print("=" * 80)

    successful_recipes = 0
    latencies = []

    for item in sorted(results, key=lambda x: x["user_idx"]):
        user_num = item["user_idx"]
        family = item["family"]
        ing = ", ".join(item.get("ingredients_typed", []))
        rec_res = item.get("recipe_result") or {}
        st = rec_res.get("status")
        lat = rec_res.get("latency_ms", 0)

        print(f"\n👤 [Account #{user_num}: {family}]")
        print(f"   Fridge ID: {item.get('fridge_id')}")
        print(f"   Typed Ingredients: [{ing}]")
        print(f"   Response Status: {st} (Latency: {lat} ms)")

        if st == "success":
            successful_recipes += 1
            latencies.append(lat)
            recipe = rec_res.get("recipe", {})
            r_name = recipe.get("recipe_name", "Untitled")
            r_name_hi = recipe.get("recipe_name_hi", "")
            meal = recipe.get("meal_type", "General")
            tip = recipe.get("chef_tip", "None")
            steps = recipe.get("steps", [])

            print(f"   🍲 Recipe Name: {r_name} ({r_name_hi})")
            print(f"   ⏱️ Meal Type: {meal} | Prep: {recipe.get('prep_time')} | Cook: {recipe.get('cook_time')}")
            print(f"   💡 Zero-Waste Tip: {tip}")
            print(f"   📋 Steps Summary ({len(steps)} steps):")
            for s_idx, s in enumerate(steps[:3], 1):
                print(f"      {s_idx}. {s}")
            if len(steps) > 3:
                print(f"      ... and {len(steps) - 3} more steps")
        else:
            print(f"   ❌ Error: {rec_res.get('error')}")

    # Compute Requests Per Second
    combined_rps = round(successful_recipes / total_wall_sec, 2) if total_wall_sec > 0 else 0.0

    print("\n" + "=" * 80)
    print("FINAL THROUGHPUT (RPS) & PERFORMANCE SUMMARY")
    print("=" * 80)
    print(f"Total Virtual Accounts Tested   : {len(RECIPE_TEST_PROMPTS)}")
    print(f"Successful Recipes Generated    : {successful_recipes}/{len(RECIPE_TEST_PROMPTS)} ({(successful_recipes/len(RECIPE_TEST_PROMPTS))*100:.1f}%)")
    print(f"Total Batch Wall Time           : {total_wall_sec:.2f} seconds")
    print(f"🔥 Real-World Combined Throughput: {combined_rps} RECIPES / SECOND (RPS)")
    if latencies:
        print(f"Latency - Min                   : {min(latencies):.1f} ms")
        print(f"Latency - Mean (Avg)            : {statistics.mean(latencies):.1f} ms")
        print(f"Latency - Median (P50)          : {statistics.median(latencies):.1f} ms")
        print(f"Latency - Max                   : {max(latencies):.1f} ms")
    print("=" * 80)

    # Save output to JSON
    output_path = os.path.join(os.path.dirname(__file__), "recipe_load_test_results.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "total_accounts": len(RECIPE_TEST_PROMPTS),
            "successful_recipes": successful_recipes,
            "wall_elapsed_sec": round(total_wall_sec, 3),
            "measured_rps": combined_rps,
            "results": results,
        }, f, indent=2)
    print(f"\nSaved complete test run & answers to {output_path}")


if __name__ == "__main__":
    main()
