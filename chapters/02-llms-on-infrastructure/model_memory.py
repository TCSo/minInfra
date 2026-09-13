"""Size a Hugging Face model the way chapter 2 does by hand.

    uv run python chapters/02-llms-on-infrastructure/model_memory.py Qwen/Qwen3.8-27B
    uv run python chapters/02-llms-on-infrastructure/model_memory.py Qwen/Qwen3.8-27B Qwen/Qwen3.8-27B-FP8

For each repo it reads config.json for the KV cache formula, reads the safetensors
headers (a few KB per shard, not the weights) for an exact parameter count, and the
file listing for the real download size. Standard library only.
"""

import json
import math
import struct
import sys
import urllib.error
import urllib.request

HF = "https://huggingface.co"
BYTES_PER_PARAM = {"BF16": 2, "FP8": 1, "INT4": 0.5}
GPUS = [  # name, memory GB, bandwidth TB/s (vendor figures)
    ("RTX 5090", 32, 1.79),
    ("L40S", 48, 0.86),
    ("A100 80GB", 80, 2.04),
    ("H100 SXM", 80, 3.35),
]
UTILIZATION = 0.9  # vLLM's default gpu_memory_utilization
KV_BYTES = 2  # BF16 cache; halve for --kv-cache-dtype fp8


def fetch(url, byte_range=None):
    headers = {"Range": f"bytes={byte_range[0]}-{byte_range[1]}"} if byte_range else {}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as r:
        return r.read()


def load_config(repo):
    full = json.loads(fetch(f"{HF}/{repo}/raw/main/config.json"))
    return full.get("text_config", full), full


def list_safetensors(repo):
    tree = json.loads(fetch(f"{HF}/api/models/{repo}/tree/main"))
    return {f["path"]: f["size"] for f in tree if f["path"].endswith(".safetensors")}


def read_headers(repo, paths):
    """The first 8 bytes of a safetensors file give the JSON header length."""
    tensors = {}
    for path in sorted(paths):
        url = f"{HF}/{repo}/resolve/main/{path}"
        n = struct.unpack("<Q", fetch(url, (0, 7)))[0]
        header = json.loads(fetch(url, (8, 7 + n)))
        header.pop("__metadata__", None)
        tensors.update(header)
    return tensors


def group_of(name):
    if "visual" in name or "vision" in name:
        return "vision tower"
    if name.startswith("mtp"):
        return "draft head (mtp)"
    if "scale" in name:
        return "quant scales"
    if "embed_tokens" in name:
        return "embedding"
    if "lm_head" in name:
        return "lm_head"
    if ".layers." in name:
        return "layers"
    return "other"


def count_params(tensors):
    groups = {}
    for name, t in tensors.items():
        n = math.prod(t["shape"])
        if t["dtype"] == "U8" and "packed" in name:
            n *= 2  # two 4-bit weights per byte
        groups[group_of(name)] = groups.get(group_of(name), 0) + n
    return groups


def kv_bytes_per_token(cfg):
    layer_types = cfg.get("layer_types")
    kv_layers = layer_types.count("full_attention") if layer_types else cfg["num_hidden_layers"]
    head_dim = cfg.get("head_dim") or cfg["hidden_size"] // cfg["num_attention_heads"]
    return kv_layers, kv_layers * 2 * cfg["num_key_value_heads"] * head_dim * KV_BYTES


def gb(n_bytes):
    return n_bytes / 1e9


def report(repo):
    cfg, full = load_config(repo)
    files = list_safetensors(repo)
    tensors = read_headers(repo, files)
    groups = count_params(tensors)
    stored = sum(groups.values())
    tied = cfg.get("tie_word_embeddings", False)
    not_loaded = groups.get("vision tower", 0) + groups.get("draft head (mtp)", 0) + groups.get("quant scales", 0)
    if tied:
        not_loaded += groups.get("lm_head", 0)
    loaded = stored - not_loaded
    file_gb = gb(sum(files.values()))

    print(f"=== {repo} ===")
    print(f"architecture      {full.get('architectures', ['?'])[0]}")
    print(f"safetensors files {len(files)}, {file_gb:.1f} GB on disk")
    print()
    print("Parameters (from tensor shapes in the safetensors headers)")
    for g, n in sorted(groups.items(), key=lambda kv: -kv[1]):
        note = "  (duplicate of embedding, tied)" if g == "lm_head" and tied else ""
        print(f"  {g:18s} {n/1e9:7.3f} B{note}")
    print(f"  {'stored':18s} {stored/1e9:7.3f} B")
    print(f"  {'text model loaded':18s} {loaded/1e9:7.3f} B  (stored minus vision, draft head, scales, tied duplicate)")
    print()
    print("Weights by the rule: parameters x bytes per parameter (text model loaded)")
    for name, b in BYTES_PER_PARAM.items():
        print(f"  {name:5s} {gb(loaded*b):6.1f} GB")
    print(f"  this checkpoint's real files: {file_gb:.1f} GB")
    print()

    kv_layers, per_token = kv_bytes_per_token(cfg)
    max_ctx = cfg.get("max_position_embeddings", 0)
    print("KV cache (BF16 cache, per sequence)")
    print(f"  layers with a KV cache  {kv_layers} of {cfg['num_hidden_layers']}")
    print(f"  kv heads x head dim     {cfg['num_key_value_heads']} x {cfg.get('head_dim') or cfg['hidden_size'] // cfg['num_attention_heads']}")
    print(f"  bytes per token         {per_token:,}  ({per_token/1024:.0f} KB)")
    print(f"  at 32k tokens           {gb(per_token*32768):.1f} GB")
    print(f"  at max context {max_ctx:>7,} {gb(per_token*max_ctx):.1f} GB")
    print()

    print(f"GPU fit using this checkpoint's real size and {UTILIZATION:.0%} utilization")
    print(f"  {'GPU':10s} {'usable':>8s} {'left for KV':>12s} {'KV tokens':>10s} {'decode ceiling':>15s}")
    for name, mem, bw in GPUS:
        usable = mem * UTILIZATION
        left = usable - file_gb
        if left <= 0:
            print(f"  {name:10s} {usable:6.1f} GB {'does not fit':>12s}")
            continue
        tokens = left * 1e9 / per_token
        ceiling = bw * 1e12 / (file_gb * 1e9)
        print(f"  {name:10s} {usable:6.1f} GB {left:9.1f} GB {tokens/1e3:8.0f}k {ceiling:11.0f} tok/s")
    print()


if __name__ == "__main__":
    repos = sys.argv[1:] or ["Qwen/Qwen3.8-27B"]
    for repo in repos:
        try:
            report(repo)
        except urllib.error.HTTPError as e:
            sys.exit(f"{repo}: {e.code} from {e.url}")
