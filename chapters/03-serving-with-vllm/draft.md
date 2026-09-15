In this chapter we will be serving a small language model, Qwen3-0.6B, with vLLM to get a taste of LLM serving on your laptop.

## What an inference engine does

A checkpoint on disk does nothing by itself. An engine is the program that does five jobs: load the weights into device memory, run prefill and decode, allocate and free KV cache per request, schedule requests into batches, and expose an API. Engines differ in how well they do each job and on which hardware. vLLM's answers, in terms from chapter 2: the KV cache is paged into 16-token blocks so finished sequences give memory back; continuous batching adds and drops sequences at every decode step; prefix caching reuses blocks across requests; and the server speaks the OpenAI API so every existing client works unchanged.

| Engine | Built for | Strengths | In this guide |
| --- | --- | --- | --- |
| vLLM | GPU serving, many users | Paged KV cache, continuous batching, day-one model support, Kubernetes ecosystem | Chapters 4 to 8 |
| SGLang | Same class as vLLM | Strong prefix caching and structured output, competitive throughput | A valid alternative, not covered |
| TensorRT-LLM | NVIDIA only | Compiled engines, highest peak throughput, heavier to operate | Chapter 7 mention |
| llama.cpp / Ollama | One user, any hardware | Runs quantized models on laptops including Metal, simplest setup | This chapter's comparison |

We use vLLM because the target is a GPU cluster serving a team, the Kubernetes tooling later in the guide is built around it, Qwen's own serving recipe for the 27B is a vLLM command, and it integrates with Ray for the tuning chapters. Not because it is fastest on a laptop; this chapter shows it is not.

## Running vLLM in Docker

First we will begin by pulling the vLLM image. Here I am using a M1 Mac with with arm architecture so I will be pulling that version.

```
docker pull vllm/vllm-openai-cpu:v0.29.0-arm64
```

Inspect with:

```
docker image inspect vllm/vllm-openai-cpu:v0.29.0-arm64
```

And observe that the size is around 2.7 GB, with a entrypoint `vllm serve`

Now to run the model it is easy as using the following command:

```
docker run -d --name vllm -p 8000:8000 --shm-size=1g \
  -v "$HOME/.cache/huggingface:/root/.cache/huggingface" \
  -v "$HOME/.cache/vllm:/root/.cache/vllm" \
  -e VLLM_CPU_KVCACHE_SPACE=2 \
  vllm/vllm-openai-cpu:v0.29.0-arm64 \
  Qwen/Qwen3-0.6B --dtype float16 --max-model-len 4096 --host 0.0.0.0 --port 8000
```

Let's explain this command:

- `--shm-size=1g`. vLLM runs as three processes that talk through shared memory, and Docker's default 64 MiB /dev/shm is too small. Try without it and you will hit "Insufficient space in /dev/shm: 160 MiB required, 64 MiB free".
- VLLM_CPU_KVCACHE_SPACE=2. On GPU, vLLM takes 90 percent of the card and gives the KV cache whatever is left after weights. On CPU you state the cache size in GiB explicitly. Two is enough for this model and leaves room for the kind cluster.
- --dtype float16. Apple Silicon has no BF16 instructions so we will be using float16 here
- --max-model-len 4096. The model supports 40k, but the cache budget divided by the per-request length is the concurrency, and the log will show that arithmetic.
- --host 0.0.0.0. The port mapping only works if the server listens on all interfaces inside the container.

Now checkout the vllm container logs:

```
docker logs -f vllm
```

1. APIServer starts, pid 1. The warning "Model Runner V2 requires Triton; using the V1 model runner" is expected: Triton is a GPU compiler, so CPU uses the older runner.
2. EngineCore starts as a second process and prints the whole effective config on one long line. Find dtype=torch.float16, max_seq_len=4096, device_config=cpu, enable_prefix_caching=True, enable_chunked_prefill=True. Those last two are chapter 7 topics that are on by default; note them and move on.
3. Worker starts as a third process. Two NUMA warnings are harmless in a single-node VM. Then "Checkpoint size: 1.40 GiB. Available RAM", the download if it is the first run, and "Loading weights took N seconds". The 1.40 GiB is the 1.5 GB file from chapter 2 in GiB.
4. Profiling and compile. "Initial profiling/warmup run took" is the engine running one forward pass to measure memory, then torch.compile. This is the slow part, and the second run is faster because of the cache mount.
5. The KV cache line, the chapter 2 payoff:

Explicitly set (2.0/9.7) GiB for KV cache on node 0.
GPU KV cache size: 18,688 tokens, Maximum concurrency for 4,096 tokens per request: 4.56x

It says GPU even on CPU. Chapter 2's 114,688 bytes per token into 2 GiB gives 18,724 tokens, and vLLM allocates 16-token blocks, so 18,688 is that rounded down to a block. The 4.56x is 18,688 divided by 4,096: how many full-length requests the cache holds at once. That is the concurrency number section 6 of chapter 2 promised.

After seeing `Application startup complete` let's try interacting with it:

```
% curl -s localhost:8000/v1/models | python3 -m json.tool
{
    "object": "list",
    "data": [
        {
            "id": "Qwen/Qwen3-0.6B",
            "object": "model",
            "created": 1789370237,
            "owned_by": "vllm",
            "root": "Qwen/Qwen3-0.6B",
            "parent": null,
            "max_model_len": 4096,
            "permission": [
                {
                    "id": "modelperm-9d97da25767a00ce",
                    "object": "model_permission",
                    "created": 1789370237,
                    "allow_create_engine": false,
                    "allow_sampling": true,
                    "allow_logprobs": true,
                    "allow_search_indices": false,
                    "allow_view": true,
                    "allow_fine_tuning": false,
                    "organization": "*",
                    "group": null,
                    "is_blocking": false
                }
            ]
        }
    ]
}
```

Now sending your first request, here we are using the OpenAI API format `/v1/chat/completions` which vllm supports out of the box:

```
curl -s localhost:8000/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "Qwen/Qwen3-0.6B",
  "messages": [{"role": "user", "content": "Explain in two sentences what a KV cache is."}],
  "max_tokens": 200, "temperature": 0
}' | python3 -m json.tool
```

Notice that in the result under `choices[0].message.content` there is a big block of text starting with `<think>` and the final answer was cut off due to the 200 token limit. This is because Qwen thinks by default, but not helpful if the entire content would be used by the downstream application who only wants the final answer. Let's try without thinking by adding `"chat_template_kwargs": {"enable_thinking": false}`

```
curl -s localhost:8000/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "Qwen/Qwen3-0.6B",
  "messages": [{"role": "user", "content": "Explain in two sentences what a KV cache is."}],
  "max_tokens": 200, "temperature": 0,
  "chat_template_kwargs": {"enable_thinking": false}
}' | python3 -m json.tool
```

And now you should get a clean definition of KV cache.

Now let's try streaming the result, which almost all benchmarks and applications with UI uses. Add ` "stream": true,
"stream_options": {"include_usage": true}` to the request:

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

And you should see a rapid stream of responses, followed by a final block of usage.

Now, back to the thinking problem, you can always send the thinking flag or parse the response yourself, but the easier way is to use the reasoning parse. Let's tear down the current image and recreate one with the parser:

```
docker stop vllm
docker rm vllm
docker run -d --name vllm -p 8000:8000 --shm-size=1g \
  -v "$HOME/.cache/huggingface:/root/.cache/huggingface" \
  -v "$HOME/.cache/vllm:/root/.cache/vllm" \
  -e VLLM_CPU_KVCACHE_SPACE=2 \
  vllm/vllm-openai-cpu:v0.29.0-arm64 \
  Qwen/Qwen3-0.6B --dtype float16 --max-model-len 4096 --host 0.0.0.0 --port 8000 \
  --reasoning-parser qwen3
```

Now retry the original query without the thinking off flag:

```
curl -s localhost:8000/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "Qwen/Qwen3-0.6B",
  "messages": [{"role": "user", "content": "Explain in two sentences what a KV cache is."}],
  "max_tokens": 200, "temperature": 0
}' | python3 -m json.tool
```

And observe that now the content is separated from the reasoning as two different sections under `message`:

```
"index": 0,
            "message": {
                "role": "assistant",
                "content": "\n\nA KV cache is a type of memory cache that stores key-value pairs for quick access, typically used in machine learning or data processing to speed up data retrieval. It stores frequently accessed data in a temporary storage area, reducing the time needed to access it from the main memory.  \n\nAnother version:",
                "refusal": null,
                "annotations": null,
                "audio": null,
                "function_call": null,
                "reasoning": "\nOkay, the user is asking for a two-sentence explanation of what a KV cache is. Let me start by recalling what I know about caches. Caches are memory storage areas used to hold data temporarily. The KV cache is a specific type of cache, probably a key-value cache. \n\nFirst sentence: I need to mention that it's a type of cache used to store key-value pairs. Maybe explain its purpose, like speeding up access times by storing frequently used data. \n\nSecond sentence: Highlight that it's typically used in machine learning or data processing, where it helps in quickly retrieving data from a larger memory pool. Make sure to keep it concise and in two sentences.\n"
            }
```

While we are at it, we can also get the aggretated metrics from vllm's prometheus metric counter. This would prove to be important as we move into autoscaling in a few chapters.

```
curl -s localhost:8000/metrics | grep -E '^vllm:(num_requests_running|prompt_tokens_total|generation_tokens_total)'
```

Now as a last step, let's measure how good our toy model deployment is. We will be measuring two key measurements: **time to first token (TTFT)** and **Decode latency**. TTFT is the time that it takes for the LLM to process the entire prompt at once with massive parallel matrix multiplications, so its compute bound; while decode generate each new tokens sequentially while keeping existing KV pairs in memory, so is mostly memory bound. How to improve and balance these are one of the key ideas in LLM serving. We have provided a simple benchmark script to measure those metrics for the deployment in `benchmark.py`, and you can invoke it with:

```
uv run python chapters/03-serving-with-vllm/benchmark.py --model Qwen/Qwen3-0.6B
```

Now try it with a longer prompt to see the difference for prefill and its effect on TTFT.

```
uv run python chapters/03-serving-with-vllm/benchmark.py --model Qwen/Qwen3-0.6B --prompt "$(cat chapters/02-llms-on-infrastructure/draft.md | head -c 3000)"
```

You should see that the TTFT increased dramatically while the decode rate barely moved. My runs, medians of five:

| Prompt tokens | TTFT | Decode |
| --- | --- | --- |
| 44 | 158 ms | 36.9 tok/s |
| 979 | 2185 ms | 32.9 tok/s |

Prefill rate from the two rows: 935 extra tokens in 2.03 extra seconds, about 460 tokens/s. Decode is the same in both rows because a decode step costs the same however long the prompt was. Chapter 2 put the M1 Pro's bandwidth ceiling at about 168 tokens/s for this model; the CPU backend reaches a fifth of it, because it runs out of arithmetic long before it runs out of bandwidth.

The first time I ran this the 979-token TTFT was 235 ms, not 2185. That was prefix caching: the warm-up run and the timed runs sent the same prompt, and vLLM reused the cached KV blocks. The script now prefixes each prompt with a timestamp so no two requests share a prefix. Appendix C has the details.

## How it compares: the same model on Ollama

Ollama wraps llama.cpp and runs the model on the Mac's GPU through Metal. It also speaks the OpenAI API, so the same script works against it. Pull the fp16 build so both engines run the same precision:

```
brew install ollama
ollama serve
ollama pull qwen3:0.6b-fp16
uv run python chapters/03-serving-with-vllm/benchmark.py --url http://localhost:11434 --model qwen3:0.6b-fp16
uv run python chapters/03-serving-with-vllm/benchmark.py --url http://localhost:11434 --model qwen3:0.6b-fp16 --prompt "$(cat chapters/02-llms-on-infrastructure/draft.md | head -c 3000)"
```

Ollama ignores vLLM's `ignore_eos` and `chat_template_kwargs`, so its tokens are thinking tokens streamed in a `reasoning` field; the script counts those too. A token costs the same whichever field it lands in.

| Engine | Prompt tokens | TTFT | Decode |
| --- | --- | --- | --- |
| vLLM, CPU in Docker, float16 | 44 | 158 ms | 36.9 tok/s |
| vLLM, CPU in Docker, float16 | 979 | 2185 ms | 32.9 tok/s |
| Ollama, Metal, fp16 | 42 | 160 ms | 113.7 tok/s |
| Ollama, Metal, fp16 | 977 | 455 ms | 106.7 tok/s |

Ollama decodes three times faster and prefills seven times faster (about 3,200 tokens/s), and its 114 tokens/s is two thirds of the 168 ceiling: on a GPU, decode gets close to bandwidth-bound, exactly as chapter 2 said. The runs also barely vary (113.3 to 114.2) while vLLM's spread from 23 to 40, because the GPU is not sharing cores with everything else on the laptop. The conclusion is not that vLLM is slow. On a laptop, llama.cpp reaches the hardware that matters; vLLM's CPU backend is a portability path for testing, which is what we are using it for. Chapter 6 runs vLLM on the hardware it was built for.

## Checkpoint

```
uv run python chapters/03-serving-with-vllm/benchmark.py --model Qwen/Qwen3-0.6B
```

The number is the median decode rate: 36.9 tokens/s here. The script is reused in every later chapter, against the cluster, the cloud GPU, and the coding agent. Stop the container with `docker stop vllm`; the weights stay cached for chapter 4.

## What to remember

- An engine is five jobs: load weights, prefill and decode, manage the KV cache, batch, serve an API.
- The KV cache line at startup is chapter 2 arithmetic; the concurrency number next to it is the cache divided by the max request length.
- Prefill is compute-bound and sets TTFT; decode is bandwidth-bound and sets tokens/s. Today's table showed both, and showed a CPU reaching a fifth of its bandwidth ceiling while a GPU reached two thirds.
- Prefix caching is on by default and changes what you measure. Benchmarks must defeat it; agents benefit from it.
- Thinking tokens are still tokens. Split them with a reasoning parser so clients only see the answer.

## Appendix

### A. /dev/shm too small

Without `--shm-size`, the engine dies at startup with `Insufficient space in /dev/shm: 160 MiB required, 64 MiB free. Increase /dev/shm (e.g. --shm-size or --ipc=host)`. The API server, engine core and worker exchange requests through shared memory. Chapter 4 gives the pod a memory-backed volume at `/dev/shm` for the same reason.

### B. BF16 on Apple Silicon

`sysctl hw.optional.arm.FEAT_BF16` prints 0 on M1 family chips. With vLLM's default dtype the log shows `Failed to create oneDNN linear, fallback to torch linear` and `mkldnn_matmul failed`, and the warm-up pass ran for over 12 minutes at 900% CPU before I killed it. With `--dtype float16` the same startup takes about a minute.

### C. Prefix caching fooled the benchmark

With the same 979-token prompt on every run, TTFT was 235 ms; with a unique prefix per run it was 2185 ms. The engine had cached the prompt's KV blocks after the warm-up run. The script prefixes each prompt with a nanosecond timestamp, which invalidates the whole prefix since matching starts at the first block. It costs about 18 prompt tokens, because a long number tokenizes into many pieces. Chapter 7 measures the same effect as a feature.

### D. Thinking tokens

Qwen3 thinks by default. Without a reasoning parser the `<think>` block sits inside `content` and eats the token budget. With `--reasoning-parser qwen3` it moves to a `reasoning` field in the message; Ollama streams it in the same field. The benchmark counts a chunk as a token if it carries either field.

### E. Stopping and restarting

`docker stop vllm` keeps the container; `docker start vllm` brings it back with the same flags, mounts and port in about a minute. `docker run` creates a new container and fails while the name is taken, so use `docker rm -f vllm` first when you want different flags, as the reasoning-parser restart did.
