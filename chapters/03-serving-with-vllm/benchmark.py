"""Latency and throughput benchmark against an OpenAI-compatible endpoint.

Single stream (chapter 3):
    uv run python chapters/03-serving-with-vllm/benchmark.py --model Qwen/Qwen3-0.6B

N concurrent streams (chapter 5):
    uv run python chapters/03-serving-with-vllm/benchmark.py --model Qwen/Qwen3-0.6B --program --max-tokens 1024 --runs 1 --concurrency 8
"""

import argparse
import json
import statistics
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

PROGRAM_PROMPT = (
    "Write a complete Python program: a command-line todo manager with add, list, "
    "done and delete subcommands that stores its items in a JSON file. "
    "Include argparse, docstrings and error handling."
)


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def dollars_per_m(price_per_hour, tok_s):
    return price_per_hour * 1e6 / (tok_s * 3600)


def one_request(url, model, prompt, max_tokens):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": f"{time.time_ns()} {prompt}"}], # Preventing caching of identical prompts by the server
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
        "ignore_eos": True,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        f"{url}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t_send = time.perf_counter()
    t_first = t_last = None
    usage = None
    with urllib.request.urlopen(req) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[len("data:"):])
            if chunk.get("usage"):
                usage = chunk["usage"]
            delta = chunk["choices"][0]["delta"] if chunk["choices"] else {}
            if delta.get("content") or delta.get("reasoning"):
                t_last = time.perf_counter()
                if t_first is None:
                    t_first = t_last
    n = usage["completion_tokens"]
    return {
        "prompt_tokens": usage["prompt_tokens"],
        "completion_tokens": n,
        "ttft_s": t_first - t_send,
        "decode_tok_s": (n - 1) / (t_last - t_first),
        "total_s": t_last - t_send,
    }


def one_round(url, model, prompt, max_tokens, concurrency, slo_ttft_ms, slo_tpot_ms):
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(lambda _: one_request(url, model, prompt, max_tokens), range(concurrency)))
    wall = time.perf_counter() - t0
    tokens = sum(r["completion_tokens"] for r in results)
    tpots = [1000 / r["decode_tok_s"] for r in results]
    good = [r for r in results if r["ttft_s"] * 1000 <= slo_ttft_ms and 1000 / r["decode_tok_s"] <= slo_tpot_ms]
    return {
        "wall_s": wall,
        "aggregate_tok_s": tokens / wall,
        "goodput_tok_s": sum(r["completion_tokens"] for r in good) / wall,
        "good_requests": len(good),
        "ttft_ms": statistics.median(r["ttft_s"] for r in results) * 1000,
        "ttft_p99_ms": pct([r["ttft_s"] for r in results], 0.99) * 1000,
        "tpot_ms": statistics.median(tpots),
        "tpot_p99_ms": pct(tpots, 0.99),
        "total_s": statistics.median(r["total_s"] for r in results),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--model", required=True)
    p.add_argument("--prompt", default="Explain what a KV cache is and why it matters for serving.")
    p.add_argument("--max-tokens", type=int, default=128)
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--program", action="store_true", help="use the fixed program-writing prompt")
    p.add_argument("--slo-ttft-ms", type=float, default=1000)
    p.add_argument("--slo-tpot-ms", type=float, default=50)
    p.add_argument("--price-per-hour", type=float, action="append", default=[], help="GPU-hour price, repeatable")
    args = p.parse_args()
    if args.program:
        args.prompt = PROGRAM_PROMPT

    one_request(args.url, args.model, args.prompt, args.max_tokens)  # warm-up, discarded
    if args.concurrency == 1:
        results = [one_request(args.url, args.model, args.prompt, args.max_tokens) for _ in range(args.runs)]
        for r in results:
            print(f"prompt={r['prompt_tokens']:5d} completion={r['completion_tokens']:4d} "
                  f"ttft={r['ttft_s']*1000:7.0f} ms decode={r['decode_tok_s']:6.1f} tok/s total={r['total_s']:5.1f} s")
        print(f"median ttft {statistics.median(r['ttft_s'] for r in results)*1000:.0f} ms, "
              f"median decode {statistics.median(r['decode_tok_s'] for r in results):.1f} tok/s")
        decode = statistics.median(r["decode_tok_s"] for r in results)
        for price in args.price_per_hour:
            print(f"${price:.2f}/h -> ${dollars_per_m(price, decode):.2f} per M output tokens, single stream")
    else:
        for _ in range(args.runs):
            r = one_round(args.url, args.model, args.prompt, args.max_tokens, args.concurrency,
                          args.slo_ttft_ms, args.slo_tpot_ms)
            print(f"concurrency={args.concurrency:3d} aggregate={r['aggregate_tok_s']:7.1f} tok/s "
                  f"goodput={r['goodput_tok_s']:7.1f} tok/s ({r['good_requests']}/{args.concurrency} in SLO) "
                  f"wall={r['wall_s']:6.1f} s | ttft median={r['ttft_ms']:5.0f} p99={r['ttft_p99_ms']:5.0f} ms | "
                  f"tpot median={r['tpot_ms']:5.1f} p99={r['tpot_p99_ms']:5.1f} ms | total median={r['total_s']:6.1f} s")
            for price in args.price_per_hour:
                print(f"  ${price:.2f}/h -> ${dollars_per_m(price, r['aggregate_tok_s']):.3f} per M tokens at aggregate, "
                      f"${dollars_per_m(price, r['goodput_tok_s']):.3f} at goodput")


if __name__ == "__main__":
    main()
