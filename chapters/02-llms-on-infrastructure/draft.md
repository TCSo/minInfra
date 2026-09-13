Chapter 1 ended with "a model server is just a Deployment with a big image and a big memory request". This chapter answers how big, and why, using only numbers you can look up in a model's config file. No cluster is needed. By the end you will be able to take any open model and say what it weighs at each precision, how much memory each open request costs, how fast one GPU can possibly serve it, and which GPU to rent.

Since not everyone has a capable GPU cluster at home, chapters 3 to 5 use a small model on your laptop before moving to the cloud in chapter 6. We start with that small model here, because it is small enough to count by hand.

## 1. A model is parameters times bytes

Let's start by pulling down Qwen3-0.6B's config file from huggingface and run a small exercise:

```
% curl -s https://huggingface.co/Qwen/Qwen3-0.6B/raw/main/config.json | python3 -m json.tool
{
    "architectures": [
        "Qwen3ForCausalLM"
    ],
    "attention_bias": false,
    "attention_dropout": 0.0,
    "bos_token_id": 151643,
    "eos_token_id": 151645,
    "head_dim": 128,
    "hidden_act": "silu",
    "hidden_size": 1024,
    "initializer_range": 0.02,
    "intermediate_size": 3072,
    "max_position_embeddings": 40960,
    "max_window_layers": 28,
    "model_type": "qwen3",
    "num_attention_heads": 16,
    "num_hidden_layers": 28,
    "num_key_value_heads": 8,
    "rms_norm_eps": 1e-06,
    "rope_scaling": null,
    "rope_theta": 1000000,
    "sliding_window": null,
    "tie_word_embeddings": true,
    "torch_dtype": "bfloat16",
    "transformers_version": "4.51.0",
    "use_cache": true,
    "use_sliding_window": false,
    "vocab_size": 151936
}
```

Let's first step aside for a second to understand why this particular model is a fine target for local deployment, deploying LLM is constrained by memory and compute, and while compute can be slow, you need memory to place the model onto the machine and start executing it before you thrash the disk. So as a first step we need to calculate how big exactly is the model on the machine and it different based on the precision:

| Format       | Bytes per parameter |
| ------------ | ------------------- |
| FP32         | 4                   |
| BF16         | 2                   |
| FP 8         | 1                   |
| INT4 / NVFP4 | 0.5                 |

## 2. Counting Qwen3-0.6B by hand

As an exercise, let's calculate the number of parameters Qwen3 0.6B has and it should match the 0.6B figure:
per attention block:

$$
\begin{aligned}
D_k &= D_{hidden} \cdot (N_{KVhead} \cdot D_{head}) = 1024 \cdot (8 \cdot 128) = 1048576 \\
D_v &= D_{hidden} \cdot (N_{KVhead} \cdot D_{head}) = 1024 \cdot (8 \cdot 128) = 1048576\\
D_q &= D_{hidden} \cdot (N_{head} \cdot D_{head}) = 1024 \cdot (16 \cdot 128) = 2097152\\
D_{output} &= (N_{head} \cdot D_{head}) \cdot D_{hidden} = (16 \cdot 128) \cdot 1024  = 2097152 \\
Total &= 6291456\text{ per attention block}
\end{aligned}
$$

per linear block:

$$
\begin{aligned}
D_{gate} &= D_{hidden} \cdot D_{intermediate} = 1024 \cdot 3072 = 3145728\\
D_{up} &= D_{hidden} \cdot D_{intermediate} = 1024 \cdot 3072 = 3145728\\
D_{down} &= D_{intermediate} \cdot D_{hidden} = 3072 \cdot 1024 = 3145728\\
Total &= 9437184\text{ per feed-forward block}
\end{aligned}
$$

per embedding block, weight tied:

$$
\begin{aligned}
D_{embed} &= D_{vocab} \cdot D_{hidden} = 151936 \cdot 1024 = 155582464 \text{ per embedding layer}
\end{aligned}
$$

So in total we have:

$$
\begin{aligned}
N_{param} &= N_{layers} * (D_{attn} + D_{ffn}) + D_{embedding} \\
&= 28 * (6291456 + 9437184) + 155582464 \\
&= 595,984,384 \text{ total}
\end{aligned}
$$

Two checks. The model card at https://huggingface.co/Qwen/Qwen3-0.6B lists 0.6B total and 0.44B non-embedding; our 28 blocks alone are 440,401,920, and the total is 596M. Also notice where the parameters are: the feed-forward block is 9.4M of every 15.7M block, about 60%. Attention is the part everyone talks about, but the MLP is where the weight is. We ignored the norm vectors (one 1024-wide vector per sub-block); they add up to 65,536 parameters for the whole model, a rounding error.

A note on the gotcha in the attention block: `num_attention_heads * head_dim` is 2048, not `hidden_size`. The projections are not square. Q and the output projection follow the full head count because the 16 query heads are concatenated and then squeezed back to 1024. Only K and V shrink with `num_key_value_heads`, because those are the heads being shared. That is grouped-query attention, and it comes back in section 5 as a smaller KV cache.

Now the size. 596M parameters times 2 bytes is 1.19 GB in BF16. The rule gives one number per precision; here it is next to real files for this exact model, from the unsloth GGUF repo:

| Precision | Rule of thumb | Real file |
| --------- | ------------- | --------- |
| BF16      | 1.19 GB       | 1.20 GB   |
| Q8 / FP8  | 0.60 GB       | 0.64 GB   |
| Q4 / INT4 | 0.30 GB       | 0.40 GB   |
| Q2        | 0.15 GB       | 0.30 GB   |

The rule is exact at BF16 and tight at 8 bits, then gets loose. Quantized formats store a scale factor for every small block of weights, and they leave sensitive tensors such as the embedding and the norms in higher precision. On a 0.6B model the embedding alone is 26% of the parameters, so keeping it in BF16 adds 0.3 GB by itself. The rule is a floor: never a smaller file than that, and at 4 bits and below expect a third more. We will see the same gap on the big model, where it matters for GPU choice.

One convention for the rest of the guide: sizes are in GB, meaning 10^9 bytes, which is what Hugging Face shows. `nvidia-smi` and Kubernetes report MiB and GiB (2^20, 2^30), about 7% smaller units. When a number looks 7% off, that is why.

## 3. Applying it to Qwen3.8-27B

The model we will actually serve in chapter 6 is Qwen3.8-27B. Its config is at https://huggingface.co/Qwen/Qwen3.8-27B/raw/main/config.json, and the first thing you will notice is that the fields we used above are nested under `text_config`, next to a `vision_config`. The architecture is `Qwen3_5ForConditionalGeneration`: "ForConditionalGeneration" is the transformers naming for models that generate text conditioned on other inputs (here images and video), where a text-only model is "ForCausalLM". And "Qwen3_5" names the code that loads the weights, not the release: Qwen3.5 introduced this architecture and 3.6 and 3.8 are new weights on the same design, which is why a serving engine that runs one runs the next on release day.

You could count this model the way we counted the 0.6B. You would need to learn what a Gated DeltaNet layer is and what the vision tower does, and none of that is needed to host the model. The count is on the model card: 27B. The method is the same; the input just comes from a lookup instead of arithmetic.

| Precision | Rule of thumb | Real checkpoint                                          |
| --------- | ------------- | -------------------------------------------------------- |
| BF16      | 54 GB         | 55.6 GB (`Qwen/Qwen3.8-27B`)                             |
| FP8       | 27 GB         | 30.9 GB (`Qwen/Qwen3.8-27B-FP8`)                         |
| NVFP4     | 13.5 GB       | 23.4 GB (`unsloth/Qwen3.8-27B-NVFP4`)                    |

Same shape as the 0.6B table: exact at BF16, loose at 4 bits. The NVFP4 checkpoint is nearly twice the floor because the linear-attention layers, embeddings and output head are kept in higher precision. When you size a GPU, size it for the file you will download, not for the rule.

Two things in the BF16 file are not loaded by a text-only server: a vision tower and a multi-token-prediction draft head (used for speculative decoding in chapter 7), together about 0.9B parameters. So the deployable model is a little under the 55.6 GB file. The habit to build here is asking "what does the engine actually load", which matters again in chapter 4 when the download takes longer than the load.

One more thing to carry forward: this model's 64 layers are not all alike. Only 16 of them are ordinary attention layers; the other 48 are a linear-attention variant that keeps a small fixed-size state instead of a cache that grows with context. That does not change the weight math above. It changes the KV cache math in section 5, in your favor.

## 4. Prefill and decode

Serving a request has two phases that stress the hardware in opposite ways.

**Prefill** reads the whole prompt in one pass. Every prompt token goes through every layer at once, which is one large matrix multiply per layer. The GPU is limited by how fast it can do arithmetic. Prefill ends when the first output token appears, so "time to first token" is mostly prefill time.

**Decode** produces the output one token per step. Each step takes the sequence so far, runs it through every layer, and emits one token. To do that it has to read every weight in the model from memory once. For one token. The arithmetic per step is tiny compared to prefill; the memory traffic is the same 54 GB every step. Decode is limited by memory bandwidth, not by compute.

That gives a ceiling you can compute before renting anything:

$$
\text{decode tokens/s (one stream)} \le \frac{\text{memory bandwidth}}{\text{bytes of weights read per token}}
$$

Here are the GPUs chapter 6 chooses between, with vendor bandwidth figures:

| GPU       | Memory | Bandwidth |
| --------- | ------ | --------- |
| RTX 5090  | 32 GB  | 1.79 TB/s |
| L40S      | 48 GB  | 0.86 TB/s |
| A100 80GB | 80 GB  | 2.04 TB/s |
| H100 SXM  | 80 GB  | 3.35 TB/s |

Ceiling for Qwen3.8-27B, using the real checkpoint sizes from section 3 (a dash means the weights do not fit; section 7 explains the fit):

| GPU       | BF16 (55.6 GB) | FP8 (30.9 GB) | NVFP4 (23.4 GB) |
| --------- | -------------- | ------------- | --------------- |
| RTX 5090  | -              | -             | 77 tok/s        |
| L40S      | -              | 28 tok/s      | 37 tok/s        |
| A100 80GB | 37 tok/s       | 66 tok/s      | 87 tok/s        |
| H100 SXM  | 60 tok/s       | 108 tok/s     | 143 tok/s       |

Read the table twice. Down a column, faster memory means faster decode, and nothing else about the GPU matters for a single stream. Across a row, halving the bytes doubles the ceiling: quantization is a bandwidth trick as much as a memory trick. These are ceilings; a real engine reads the KV cache too, launches kernels, and samples, so chapter 6 will measure something below these numbers. The gap between the ceiling and the measurement is what chapter 7 is about.

The same idea on your laptop: an M1 has 68 GB/s of memory bandwidth and the 0.6B model is 1.19 GB, so decode tops out around 57 tokens/s on this machine. Chapter 3 measures how close a CPU engine gets.

Prefill has its own back-of-envelope. Each token costs about 2 floating-point operations per parameter (a multiply and an add), so a prompt of N tokens costs 2 × 27B × N. An H100 does about 990 TFLOPS of dense BF16. For a 10,000-token prompt:

$$
\frac{2 \cdot 27 \times 10^9 \cdot 10^4}{990 \times 10^{12}} \approx 0.55\text{ s}
$$

That is the floor on time to first token for a long prompt on the fastest of the four GPUs. A coding agent sends long prompts (the repository context, the conversation so far, the tool outputs) and reads short answers, so its traffic is prefill-heavy and it feels this number on every turn. Chapter 8 will make that concrete.

So: prefill wants FLOPs, decode wants bandwidth, and the two phases of one request want different hardware. Every engine trick in chapter 7 is a way of not paying full price for one of them.

## 5. The KV cache

Attention lets each new token look at every previous token. Without a cache, decoding token 1000 would recompute the keys and values of the 999 tokens before it, every step. So the engine keeps them: for every layer with attention, for every token in the sequence, it stores one key vector and one value vector per KV head. That store is the KV cache, and it is the second big memory consumer after the weights. Unlike the weights it grows with every token and with every concurrent request.

Per token, per sequence:

$$
\text{KV bytes per token} = N_{layers} \cdot 2 \cdot N_{KVhead} \cdot D_{head} \cdot \text{bytes}
$$

The 2 is one key and one value. For Qwen3-0.6B in BF16:

$$
28 \cdot 2 \cdot 8 \cdot 128 \cdot 2 = 114{,}688 \text{ bytes} \approx 115\text{ KB per token}
$$

At its full context of 40,960 tokens, one sequence needs 4.7 GB of cache. The weights are 1.19 GB. A single long conversation costs four times the model. This is why grouped-query attention exists: with 16 KV heads instead of 8, the cache would be twice as large; the 0.6B model gave up some attention capacity to halve it.

Now Qwen3.8-27B. If all 64 layers were attention layers with 4 KV heads of dimension 256:

$$
64 \cdot 2 \cdot 4 \cdot 256 \cdot 2 = 262{,}144 \text{ bytes} = 262\text{ KB per token}
$$

But only 16 layers keep a KV cache (count the `"full_attention"` entries in `layer_types` in the config). The other 48 are linear-attention layers whose state is a fixed-size matrix per head that gets updated in place, so they cost the same at token 1 and at token 200,000:

$$
16 \cdot 2 \cdot 4 \cdot 256 \cdot 2 = 65{,}536 \text{ bytes} = 64\text{ KB per token}
$$

A quarter of the dense cost. At the model's native 262,144-token context, one sequence needs 17 GB of cache in BF16, or 8.6 GB with the cache in FP8 (`--kv-cache-dtype fp8` in vLLM, which the model's own serving recipe recommends). The linear-attention state adds a constant on the order of 80 MB per sequence, small enough to ignore in the budgeting. Treat both of these as hand estimates: in chapter 6, vLLM prints at startup how many KV tokens fit in the memory it has left, and comparing that line to this arithmetic is the check.

The budget rule that falls out of this:

$$
\text{memory for KV} = \text{GPU memory} \times \text{utilization} - \text{weights}
$$

vLLM's default utilization is 0.9, so on an H100 with the FP8 checkpoint: 80 × 0.9 − 30.9 = 41 GB for cache, about 630,000 tokens in BF16. Whether that is 20 conversations of 32,000 tokens or 630 of 1,000 is the scheduler's problem, and it is the topic of the next section.

## 6. Batching

Section 4 said a decode step reads all the weights to produce one token. What if there are two sequences in flight? The step reads the weights once and produces two tokens. The extra cost is reading two KV caches instead of one and a little more arithmetic, and until the arithmetic catches up with the memory traffic, the step takes almost the same time. Throughput roughly doubles. Then triples. This continues until one of two limits: the GPU runs out of KV cache memory for more sequences, or the batch is large enough that compute is the bottleneck and each step slows down.

So a GPU serving one user is mostly idle, waiting on memory, and the single-stream ceiling in section 4 is also the single-stream waste. Tokens per second per GPU goes up nearly linearly with concurrency, and cost per token goes down the same way. This is the mechanical reason the introduction says self-hosting wins with teams and agent swarms and not with one person: the hardware price is the same, the useful tokens are not.

Requests do not arrive in neat batches, and they finish at different times. Engines handle this with continuous batching: at every decode step the scheduler looks at what is waiting, adds new sequences that fit in the cache budget, and drops the ones that finished. Prefill for a new request is slotted in between decode steps of the others, which is why a long prompt from one user briefly slows everyone. Chapter 3 shows this happening in an engine's logs; chapter 6 measures tokens per second at 1 and at 16 concurrent streams, and the ratio between the two is the number that decides whether a shared GPU pays for itself.

## 7. Why GPUs, and which one

Everything above was about bytes moved and operations done, and a GPU is the machine that does both faster: memory bandwidth in the terabytes per second against about 0.07 for a laptop and 0.2 to 0.5 for a server CPU, and dense matrix throughput in the hundreds of TFLOPS against single digits. The programming model matters less for serving than those two numbers. When you read a GPU spec sheet for this purpose, look at memory capacity (does it fit), memory bandwidth (decode speed), and dense BF16 or FP8 TFLOPS (prefill speed), in that order.

The fit table, using real checkpoint sizes and vLLM's 0.9 utilization. "Left" is what remains for KV cache, and the token count uses the 64 KB per token from section 5:

| GPU       | Usable (×0.9) | BF16 55.6 GB     | FP8 30.9 GB        | NVFP4 23.4 GB      |
| --------- | ------------- | ---------------- | ------------------ | ------------------ |
| RTX 5090  | 28.8 GB       | no               | no                 | 5.4 GB left, 82k tokens |
| L40S      | 43.2 GB       | no               | 12.3 GB, 190k      | 19.8 GB, 300k      |
| A100 80GB | 72 GB         | 16.4 GB, 250k    | 41.1 GB, 630k      | 48.6 GB, 740k      |
| H100 SXM  | 72 GB         | 16.4 GB, 250k    | 41.1 GB, 630k      | 48.6 GB, 740k      |

What the table says:

- A single consumer GPU holds this model only at 4 bits, and with a thin cache. The model's own vLLM recipe agrees: one RTX 5090 with NVFP4, FP8 KV cache, and CUDA graphs disabled to save memory. It works, it is not comfortable.
- A 48 GB card holds FP8 with a workable cache. This is the cheapest configuration that serves the model as published by Qwen rather than a third-party quantization.
- An 80 GB card holds BF16, but BF16 buys accuracy you probably cannot measure and costs half the cache and half the decode speed. FP8 on an 80 GB card is the comfortable option: full-precision KV cache for 600,000+ tokens and a 100+ tok/s single-stream ceiling.

Chapter 6 rents from this table. Prices change monthly, so the chapter records the price on the day it is run and puts it next to the measured tokens per second, which is the only way to get an honest dollars-per-million-tokens number.

## 8. A GPU is a Kubernetes resource

In chapter 1 the scheduler placed pods by comparing a pod's `resources.requests` to what each node had left. A GPU is one more line in that comparison. A node with a GPU runs NVIDIA's device plugin (installed as a DaemonSet, so it lands on every node with the right label), which discovers the cards and reports them to the kubelet as an extended resource named `nvidia.com/gpu`. From then on the node advertises it next to cpu and memory:

```
% kubectl describe node <gpu-node> | grep -A 6 Allocatable
Allocatable:
  cpu:                16
  ephemeral-storage:  ...
  memory:             ...
  nvidia.com/gpu:     1
```

On your kind cluster the line is missing, which is the correct answer: no plugin, no GPUs. A pod asks for one the same way it asks for memory:

```yaml
resources:
  limits:
    nvidia.com/gpu: 1
```

Three rules that differ from cpu and memory. GPUs are requested in whole units, and a container that gets one gets all of it; sharing (time-slicing, MIG) exists but is opt-in. Requests and limits must be equal, so you write it under `limits` only. And there is no overcommit: a node with one GPU runs one GPU pod, and a second one stays Pending with a scheduler event saying `Insufficient nvidia.com/gpu`, the same way a pod stays Pending when no node has the memory it requests.

The container also needs the CUDA libraries that match the host driver, which is why model-server images are large and why "a Deployment with a big image and a big memory request" is the right description. Chapter 6 builds the GPU node, installs the plugin, and applies the chapter 4 manifests with this one block added.

## Checkpoint

The chapter's command is a small script that does sections 1, 3 and 5 for any model on Hugging Face, from its config and the sizes of its published files:

```
uv run python chapters/02-llms-on-infrastructure/model_memory.py Qwen/Qwen3.8-27B
```

It downloads only the config, the file listing, and a few KB of header from each weight file, so it runs in seconds. The lines that matter:

```
  text model loaded   26.896 B  (stored minus vision, draft head, scales, tied duplicate)
  BF16    53.8 GB
  this checkpoint's real files: 55.6 GB
  bytes per token         65,536  (64 KB)
```

The number is the BF16 weight size: about 54 GB. If you did the arithmetic by hand first, the script confirms it; if a new model comes out, the script is how you redo this chapter in ten seconds. Run it on the three checkpoints from section 3 together and it prints the fit table from section 7 with real file sizes:

```
uv run python chapters/02-llms-on-infrastructure/model_memory.py Qwen/Qwen3.8-27B Qwen/Qwen3.8-27B-FP8 unsloth/Qwen3.8-27B-NVFP4
```

## What to remember

- Weights = parameters × bytes per parameter. Exact at BF16 and FP8, a floor at 4 bits: size for the file you will download.
- The MLP is most of the model. Attention is most of the discussion.
- Prefill is compute-bound and sets time to first token. Decode is bandwidth-bound and sets tokens per second. A coding agent is prefill-heavy.
- Single-stream decode ceiling = bandwidth / weight bytes. Compute it before renting; measure against it after.
- KV cache = layers × 2 × KV heads × head dim × bytes, per token, per sequence. It grows with context and with concurrency, and it is what is left after the weights.
- Batching is nearly free until the cache is full. One user wastes a GPU; sixteen use it.
- A GPU is a schedulable resource like memory, requested in whole units under `limits`. The scheduler does the rest.
