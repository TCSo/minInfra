# Chapter 3: Serving with vLLM

Serve Qwen3-0.6B with vLLM on your laptop, talk to it through the OpenAI API, and measure it. Then run the same model on Ollama to see what the engine and the hardware each contribute.

**You will have:** a model answering OpenAI-style requests in Docker, with streaming, a reasoning parser, and Prometheus metrics.

**You will measure:** time to first token and single-stream decode tokens per second, on vLLM (CPU) and Ollama (Metal).

## Files

| File | What it is |
| --- | --- |
| `benchmark.py` | Single-stream TTFT and decode benchmark against any OpenAI-compatible endpoint. Reused in every later chapter. |

## Prerequisites

- Docker Desktop from chapter 1. The kind cluster can stay up; the settings below leave room for it.
- Tested on a 2021 M1 Mac with 16 GB RAM. On x86, use the `vllm/vllm-openai-cpu:v0.29.0` image without the `-arm64` suffix.

## What an inference engine does

A checkpoint on disk does nothing by itself. An engine loads the weights into device memory, runs prefill and decode, allocates and frees KV cache per request, schedules requests into batches, and exposes an API. vLLM's answers, in chapter 2 terms: the KV cache is paged into 16-token blocks, continuous batching adds and drops sequences every decode step, prefix caching reuses blocks across requests, and the server speaks the OpenAI API.

| Engine | Built for | Strengths | In this guide |
| --- | --- | --- | --- |
| vLLM | GPU serving, many users | Paged KV cache, continuous batching, day-one model support, Kubernetes ecosystem | Chapters 4 to 8 |
| SGLang | Same class as vLLM | Strong prefix caching and structured output, competitive throughput | A valid alternative, not covered |
| TensorRT-LLM | NVIDIA only | Compiled engines, highest peak throughput, heavier to operate | Chapter 7 mention |
| llama.cpp / Ollama | One user, any hardware | Runs quantized models on laptops including Metal, simplest setup | This chapter's comparison |

We use vLLM because the target is a GPU cluster serving a team, the Kubernetes tooling later in the guide is built around it, Qwen's own serving recipe for the 27B is a vLLM command, and it integrates with Ray for the tuning chapters. Not because it is fastest on a laptop. This chapter shows it is not.

## 1. Run vLLM in Docker

```
docker pull vllm/vllm-openai-cpu:v0.29.0-arm64
docker image inspect vllm/vllm-openai-cpu:v0.29.0-arm64
```

The image is about 2.7 GB and its entrypoint is `vllm serve`.

```
docker run -d --name vllm -p 8000:8000 --shm-size=1g \
  -v "$HOME/.cache/huggingface:/root/.cache/huggingface" \
  -v "$HOME/.cache/vllm:/root/.cache/vllm" \
  -e VLLM_CPU_KVCACHE_SPACE=2 \
  vllm/vllm-openai-cpu:v0.29.0-arm64 \
  Qwen/Qwen3-0.6B --dtype float16 --max-model-len 4096 --host 0.0.0.0 --port 8000
```

- `--shm-size=1g`: vLLM runs as three processes that talk through shared memory. Docker's default 64 MiB `/dev/shm` is too small (appendix A).
- `VLLM_CPU_KVCACHE_SPACE=2`: on GPU, vLLM takes 90 percent of the card and gives the KV cache what is left after weights. On CPU you state the cache size in GiB. Two is enough for this model and leaves room for the kind cluster.
- `--dtype float16`: M1 chips have no BF16 instructions (appendix B).
- `--max-model-len 4096`: the model supports 40k, but cache divided by request length is the concurrency, and the log shows that arithmetic.
- `--host 0.0.0.0`: the port mapping only works if the server listens on all interfaces.

## 2. Read the startup log

```
docker logs -f vllm
```

1. `APIServer` starts as pid 1. The warning "Model Runner V2 requires Triton; using the V1 model runner" is expected: Triton is a GPU compiler.
2. `EngineCore` starts as a second process and prints the effective config on one line. Find `dtype=torch.float16`, `max_seq_len=4096`, `device_config=cpu`, `enable_prefix_caching=True`, `enable_chunked_prefill=True`. The last two are chapter 7 topics, on by default.
3. `Worker` starts as a third process. Two NUMA warnings are harmless in a single-node VM. Then "Checkpoint size: 1.40 GiB", the download on first run, and "Loading weights took N seconds". 1.40 GiB is the 1.5 GB safetensors file, which also stores the tied output head that the chapter 2 script subtracts.
4. "Initial profiling/warmup run took" is one forward pass to measure memory, then `torch.compile`. This is the slow part; the cache mount makes the second start faster.
5. The KV cache line, the chapter 2 payoff:

```
Explicitly set (2.0/9.7) GiB for KV cache on node 0.
GPU KV cache size: 18,688 tokens, Maximum concurrency for 4,096 tokens per request: 4.56x
```

It says GPU even on CPU. Chapter 2's 114,688 bytes per token into 2 GiB gives 18,724 tokens, rounded down to 16-token blocks gives 18,688. The 4.56x is 18,688 / 4,096: how many full-length requests the cache holds at once.

## 3. Send requests

Wait for `Application startup complete`, then:

```
curl -s localhost:8000/v1/models | python3 -m json.tool
```

First chat completion, in OpenAI format:

```
curl -s localhost:8000/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "Qwen/Qwen3-0.6B",
  "messages": [{"role": "user", "content": "Explain in two sentences what a KV cache is."}],
  "max_tokens": 200, "temperature": 0
}' | python3 -m json.tool
```

`choices[0].message.content` starts with a `<think>` block and the answer is cut off by the 200-token limit. Qwen3 thinks by default. Turn it off per request:

```
curl -s localhost:8000/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "Qwen/Qwen3-0.6B",
  "messages": [{"role": "user", "content": "Explain in two sentences what a KV cache is."}],
  "max_tokens": 200, "temperature": 0,
  "chat_template_kwargs": {"enable_thinking": false}
}' | python3 -m json.tool
```

Streaming, which nearly every UI and benchmark uses:

```
curl -sN localhost:8000/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "Qwen/Qwen3-0.6B",
  "messages": [{"role": "user", "content": "Write a haiku about Kubernetes."}],
  "max_tokens": 100, "temperature": 0,
  "chat_template_kwargs": {"enable_thinking": false},
  "stream": true,
  "stream_options": {"include_usage": true}
}'
```

Expected: a rapid stream of chunks, then a final chunk with `usage`.

## 4. Add a reasoning parser

Rather than turning thinking off or parsing `<think>` yourself, let the server split it. Recreate the container with `--reasoning-parser qwen3`:

```
docker rm -f vllm
docker run -d --name vllm -p 8000:8000 --shm-size=1g \
  -v "$HOME/.cache/huggingface:/root/.cache/huggingface" \
  -v "$HOME/.cache/vllm:/root/.cache/vllm" \
  -e VLLM_CPU_KVCACHE_SPACE=2 \
  vllm/vllm-openai-cpu:v0.29.0-arm64 \
  Qwen/Qwen3-0.6B --dtype float16 --max-model-len 4096 --host 0.0.0.0 --port 8000 \
  --reasoning-parser qwen3
```

Send the first request again, without the thinking flag. The message now has the answer in `content` and the thinking in a separate `reasoning` field.

## 5. Read the metrics

vLLM exposes Prometheus metrics. These drive autoscaling in chapter 5.

```
curl -s localhost:8000/metrics | grep -E '^vllm:(num_requests_running|prompt_tokens_total|generation_tokens_total)'
```

## 6. Benchmark

`benchmark.py` sends one warm-up request, then five timed streaming requests, and reports time to first token (prefill, compute-bound) and decode tokens per second (bandwidth-bound). It prefixes every prompt with a timestamp so prefix caching cannot skew the result (appendix C).

```
uv run python chapters/03-serving-with-vllm/benchmark.py --model Qwen/Qwen3-0.6B
```

Then with a prompt of about 1,000 tokens:

```
uv run python chapters/03-serving-with-vllm/benchmark.py --model Qwen/Qwen3-0.6B --prompt "$(head -c 3000 chapters/02-llms-on-infrastructure/README.md)"
```

TTFT rises sharply while decode barely moves. Reference runs, medians of five:

| Prompt tokens | TTFT | Decode |
| --- | --- | --- |
| 44 | 158 ms | 36.9 tok/s |
| 979 | 2185 ms | 32.9 tok/s |

Prefill rate from the two rows: 935 extra tokens in 2.03 extra seconds, about 460 tokens/s. Decode is flat because a decode step costs the same however long the prompt was. Chapter 2 put this machine's bandwidth ceiling at about 168 tokens/s; the CPU backend reaches a fifth of it, because it runs out of arithmetic long before bandwidth.

## 7. Compare: the same model on Ollama

Ollama wraps llama.cpp and runs on the Mac's GPU through Metal. It speaks the OpenAI API, so the same script works. Pull the fp16 build so both engines run the same precision.

```
brew install ollama
ollama serve
ollama pull qwen3:0.6b-fp16
uv run python chapters/03-serving-with-vllm/benchmark.py --url http://localhost:11434 --model qwen3:0.6b-fp16
uv run python chapters/03-serving-with-vllm/benchmark.py --url http://localhost:11434 --model qwen3:0.6b-fp16 --prompt "$(head -c 3000 chapters/02-llms-on-infrastructure/README.md)"
```

Ollama ignores `ignore_eos` and `chat_template_kwargs`, so its output tokens are thinking tokens in a `reasoning` field. The script counts those too; a token costs the same in either field.

| Engine | Prompt tokens | TTFT | Decode |
| --- | --- | --- | --- |
| vLLM, CPU in Docker, float16 | 44 | 158 ms | 36.9 tok/s |
| vLLM, CPU in Docker, float16 | 979 | 2185 ms | 32.9 tok/s |
| Ollama, Metal, fp16 | 42 | 160 ms | 113.7 tok/s |
| Ollama, Metal, fp16 | 977 | 455 ms | 106.7 tok/s |

Ollama decodes three times faster and prefills seven times faster (about 3,200 tokens/s). Its 114 tokens/s is two thirds of the 168 ceiling: on a GPU, decode gets close to bandwidth-bound, as chapter 2 said. Its runs also barely vary (113.3 to 114.2) while vLLM's spread from 23 to 40, because the GPU is not sharing cores with the rest of the laptop.

The conclusion is not that vLLM is slow. On a laptop, llama.cpp reaches the hardware that matters. vLLM's CPU backend is a portability path for testing, which is what we use it for. Chapter 6 runs vLLM on the hardware it was built for.

## Checkpoint

```
uv run python chapters/03-serving-with-vllm/benchmark.py --model Qwen/Qwen3-0.6B
```

The number is the median decode rate: 36.9 tokens/s on the test machine. Stop the container when done; the weights stay cached for chapter 4.

```
docker stop vllm
```

## What to remember

- An engine is five jobs: load weights, prefill and decode, manage the KV cache, batch, serve an API.
- The KV cache line at startup is chapter 2 arithmetic. The concurrency next to it is cache divided by max request length.
- Prefill is compute-bound and sets TTFT. Decode is bandwidth-bound and sets tokens per second. A CPU reached a fifth of its bandwidth ceiling; a GPU reached two thirds.
- Prefix caching is on by default and changes what you measure. Benchmarks must defeat it; agents benefit from it.
- Thinking tokens are still tokens. Split them with a reasoning parser so clients only see the answer.

## Appendix

### A. /dev/shm too small

Without `--shm-size`, startup fails with `Insufficient space in /dev/shm: 160 MiB required, 64 MiB free`. The API server, engine core and worker exchange requests through shared memory. Chapter 4 gives the pod a memory-backed volume at `/dev/shm` for the same reason.

### B. BF16 on Apple Silicon

`sysctl hw.optional.arm.FEAT_BF16` prints 0 on M1 chips. With vLLM's default dtype the log shows `Failed to create oneDNN linear, fallback to torch linear` and `mkldnn_matmul failed`, and the warm-up pass ran for over 12 minutes at 900 percent CPU. With `--dtype float16` startup takes about a minute.

### C. Prefix caching fooled the benchmark

With the same 979-token prompt on every run, TTFT was 235 ms instead of 2185 ms: the engine had cached the prompt's KV blocks after the warm-up. The script now prefixes each prompt with a nanosecond timestamp, which invalidates the whole prefix since matching starts at the first block. It costs about 18 prompt tokens, because a long number tokenizes into many pieces. Chapter 7 measures the same effect as a feature.

### D. Thinking tokens

Without a reasoning parser the `<think>` block sits inside `content` and eats the token budget. With `--reasoning-parser qwen3` it moves to a `reasoning` field; Ollama streams it in the same field. The benchmark counts a chunk as a token if it carries either field.

### E. Stopping and restarting

`docker stop vllm` keeps the container and `docker start vllm` brings it back with the same flags in about a minute. `docker run` creates a new container and fails while the name is taken, so use `docker rm -f vllm` first when you want different flags.
