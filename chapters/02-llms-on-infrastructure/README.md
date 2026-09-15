# Chapter 2: LLMs on infrastructure

Chapter 1 ended with "a model server is just a Deployment with a big image and a big memory request". This chapter answers how big, and why, using only a model's config file. No cluster needed.

**You will have:** a way to take any open model and say what it weighs at each precision, what each open request costs in memory, how fast one GPU can possibly serve it, and which GPU to rent.

**You will measure:** model size against memory, worked out by hand for Qwen3-0.6B and checked by script for Qwen3.8-27B.

## Files

| File | What it is |
| --- | --- |
| `model_memory.py` | Sizes any Hugging Face model from its config and safetensors headers |

Chapters 3 to 5 use the small Qwen3-0.6B on your laptop. We start with it here because it is small enough to count by hand.

## 1. A model is parameters times bytes

Pull the config:

```
curl -s https://huggingface.co/Qwen/Qwen3-0.6B/raw/main/config.json | python3 -m json.tool
```

The fields that matter: `hidden_size` 1024, `intermediate_size` 3072, `num_hidden_layers` 28, `num_attention_heads` 16, `num_key_value_heads` 8, `head_dim` 128, `vocab_size` 151936, `max_position_embeddings` 40960, `tie_word_embeddings` true.

Serving is constrained by memory before compute: the weights must fit on the device before anything runs. Weight size is parameter count times bytes per parameter.

| Format | Bytes per parameter |
| --- | --- |
| FP32 | 4 |
| BF16 | 2 |
| FP8 | 1 |
| INT4 / NVFP4 | 0.5 |

## 2. Counting Qwen3-0.6B by hand

Per attention block:

$$
\begin{aligned}
D_k &= D_{hidden} \cdot (N_{KVhead} \cdot D_{head}) = 1024 \cdot (8 \cdot 128) = 1048576 \\
D_v &= D_{hidden} \cdot (N_{KVhead} \cdot D_{head}) = 1024 \cdot (8 \cdot 128) = 1048576 \\
D_q &= D_{hidden} \cdot (N_{head} \cdot D_{head}) = 1024 \cdot (16 \cdot 128) = 2097152 \\
D_{output} &= (N_{head} \cdot D_{head}) \cdot D_{hidden} = (16 \cdot 128) \cdot 1024 = 2097152 \\
Total &= 6291456
\end{aligned}
$$

Per feed-forward block:

$$
\begin{aligned}
D_{gate} &= D_{hidden} \cdot D_{intermediate} = 1024 \cdot 3072 = 3145728 \\
D_{up} &= D_{hidden} \cdot D_{intermediate} = 1024 \cdot 3072 = 3145728 \\
D_{down} &= D_{intermediate} \cdot D_{hidden} = 3072 \cdot 1024 = 3145728 \\
Total &= 9437184
\end{aligned}
$$

Embedding, tied with the output head:

$$
D_{embed} = D_{vocab} \cdot D_{hidden} = 151936 \cdot 1024 = 155582464
$$

Total:

$$
\begin{aligned}
N_{param} &= N_{layers} \cdot (D_{attn} + D_{ffn}) + D_{embed} \\
&= 28 \cdot (6291456 + 9437184) + 155582464 \\
&= 595984384
\end{aligned}
$$

Checks and things to notice:

- The model card lists 0.6B total and 0.44B non-embedding. The 28 blocks alone are 440M; the total is 596M.
- The feed-forward block is 9.4M of every 15.7M block, about 60 percent. The MLP is where the weight is.
- `num_attention_heads * head_dim` is 2048, not `hidden_size`. Q and the output projection follow the full head count; only K and V shrink with `num_key_value_heads`. That is grouped-query attention, and it returns in section 5 as a smaller KV cache.
- Norm vectors add 65,536 parameters in total, a rounding error.

Now the size: 596M parameters times 2 bytes is 1.19 GB in BF16. Next to real files for this model from the unsloth GGUF repo:

| Precision | Rule of thumb | Real file |
| --- | --- | --- |
| BF16 | 1.19 GB | 1.20 GB |
| Q8 / FP8 | 0.60 GB | 0.64 GB |
| Q4 / INT4 | 0.30 GB | 0.40 GB |
| Q2 | 0.15 GB | 0.30 GB |

The rule is exact at BF16, tight at 8 bits, and a floor below that. Quantized formats store scale factors per block and keep sensitive tensors (embedding, norms) in higher precision. Size for the file you will download.

Units for the rest of the guide: GB means 10^9 bytes, as Hugging Face shows. `nvidia-smi` and Kubernetes report GiB (2^30), about 7 percent smaller units. When a number looks 7 percent off, that is why.

## 3. Applying it to Qwen3.8-27B

The chapter 6 model. Its config is at https://huggingface.co/Qwen/Qwen3.8-27B/raw/main/config.json. Two differences from the 0.6B:

- The text fields are nested under `text_config`, next to a `vision_config`. The architecture is `Qwen3_5ForConditionalGeneration`: it generates text conditioned on other inputs (images, video), where a text-only model is `ForCausalLM`. Qwen3.5 introduced the architecture; 3.6 and 3.8 are new weights on the same design, which is why an engine that runs one runs the next on release day.
- Only 16 of its 64 layers are ordinary attention. The other 48 are a linear-attention variant with a fixed-size state. That does not change the weight math. It changes the KV cache math in section 5, in your favor.

Counting this model by hand means learning Gated DeltaNet and the vision tower, none of which is needed to host it. Take the count from the model card: 27B.

| Precision | Rule of thumb | Real checkpoint |
| --- | --- | --- |
| BF16 | 54 GB | 55.6 GB (`Qwen/Qwen3.8-27B`) |
| FP8 | 27 GB | 30.9 GB (`Qwen/Qwen3.8-27B-FP8`) |
| NVFP4 | 13.5 GB | 23.4 GB (`unsloth/Qwen3.8-27B-NVFP4`) |

Same shape as before: exact at BF16, loose at 4 bits, where the linear-attention layers, embeddings and output head stay in higher precision. The BF16 file also carries a vision tower and a multi-token-prediction draft head (used for speculative decoding in chapter 7), about 0.9B parameters a text-only server does not load. Always ask what the engine actually loads.

## 4. Prefill and decode

A request has two phases that stress the hardware in opposite ways.

- **Prefill** runs the whole prompt in one pass: one large matrix multiply per layer. Compute-bound. It ends when the first output token appears, so time to first token (TTFT) is mostly prefill.
- **Decode** produces one token per step, and every step reads every weight in the model once. Bandwidth-bound.

The single-stream decode ceiling:

$$
\text{decode tokens/s} \le \frac{\text{memory bandwidth}}{\text{bytes of weights read per token}}
$$

The GPUs chapter 6 chooses between, with vendor bandwidth figures:

| GPU | Memory | Bandwidth |
| --- | --- | --- |
| RTX 5090 | 32 GB | 1.79 TB/s |
| L40S | 48 GB | 0.86 TB/s |
| A100 80GB | 80 GB | 2.04 TB/s |
| H100 SXM | 80 GB | 3.35 TB/s |

Ceilings for Qwen3.8-27B using the real checkpoint sizes. A dash means the weights do not fit; section 7 has the fit.

| GPU | BF16 (55.6 GB) | FP8 (30.9 GB) | NVFP4 (23.4 GB) |
| --- | --- | --- | --- |
| RTX 5090 | - | - | 77 tok/s |
| L40S | - | 28 tok/s | 37 tok/s |
| A100 80GB | 37 tok/s | 66 tok/s | 87 tok/s |
| H100 SXM | 60 tok/s | 108 tok/s | 143 tok/s |

Down a column, faster memory means faster decode. Across a row, halving the bytes doubles the ceiling: quantization is a bandwidth trick as much as a memory trick. Real engines land below these numbers; the gap is chapter 7.

On the laptop: an M1 Pro has about 200 GB/s and the 0.6B model is 1.19 GB, so decode tops out near 168 tokens/s whatever engine runs it. Chapter 3 measures how close a CPU engine and the Mac's GPU get.

Prefill's back of envelope: about 2 FLOPs per parameter per token. An H100 does about 990 TFLOPS of dense BF16, so a 10,000-token prompt costs:

$$
\frac{2 \cdot 27 \times 10^9 \cdot 10^4}{990 \times 10^{12}} \approx 0.55\text{ s}
$$

That is the TTFT floor for a long prompt on the fastest of the four GPUs. A coding agent sends long prompts and reads short answers, so its traffic is prefill-heavy. Chapter 8 makes that concrete.

## 5. The KV cache

The engine stores one key and one value vector per KV head, per attention layer, per token, so decoding token 1000 does not recompute the 999 before it. That store is the KV cache. Unlike the weights, it grows with every token and every concurrent request.

$$
\text{KV bytes per token} = N_{layers} \cdot 2 \cdot N_{KVhead} \cdot D_{head} \cdot \text{bytes}
$$

Qwen3-0.6B in BF16:

$$
28 \cdot 2 \cdot 8 \cdot 128 \cdot 2 = 114688 \text{ bytes} \approx 115\text{ KB per token}
$$

At its full 40,960-token context, one sequence needs 4.7 GB of cache against 1.19 GB of weights. This is why grouped-query attention exists: 16 KV heads instead of 8 would double it.

Qwen3.8-27B, if all 64 layers were attention with 4 KV heads of dimension 256:

$$
64 \cdot 2 \cdot 4 \cdot 256 \cdot 2 = 262144 \text{ bytes} = 262\text{ KB per token}
$$

But only 16 layers keep a KV cache (count `"full_attention"` in `layer_types`). The other 48 hold a fixed-size state that costs the same at token 1 and token 200,000:

$$
16 \cdot 2 \cdot 4 \cdot 256 \cdot 2 = 65536 \text{ bytes} = 64\text{ KB per token}
$$

A quarter of the dense cost. At the native 262,144-token context, one sequence needs 17 GB in BF16, or 8.6 GB with `--kv-cache-dtype fp8` in vLLM, which the model's own serving recipe recommends. The linear-attention state adds about 80 MB per sequence, small enough to ignore. In chapter 6, vLLM prints at startup how many KV tokens fit; compare that line to this arithmetic.

The budget rule:

$$
\text{memory for KV} = \text{GPU memory} \times \text{utilization} - \text{weights}
$$

vLLM's default utilization is 0.9. H100 with the FP8 checkpoint: 80 × 0.9 − 30.9 = 41 GB, about 630,000 tokens in BF16. Whether that is 20 conversations of 32,000 tokens or 630 of 1,000 is the scheduler's problem.

## 6. Batching

A decode step reads all the weights to produce one token. With two sequences in flight it reads them once and produces two tokens, for almost the same time. Throughput grows nearly linearly with concurrency until the KV cache is full or the batch is large enough that compute becomes the bottleneck.

So a GPU serving one user is mostly idle. Tokens per second per GPU goes up with concurrency and cost per token goes down the same way. This is the mechanical reason self-hosting wins with teams and agent swarms and not with one person.

Requests arrive and finish at different times. Engines handle this with continuous batching: at every decode step the scheduler adds new sequences that fit the cache budget and drops finished ones. Prefill for a new request is slotted between decode steps, which is why one long prompt briefly slows everyone. Chapter 3 shows this in engine logs; chapter 6 measures tokens per second at 1 and 16 concurrent streams. That ratio decides whether a shared GPU pays for itself.

## 7. Why GPUs, and which one

A GPU moves bytes and does arithmetic faster: memory bandwidth in terabytes per second against 0.1 to 0.5 for CPUs, and dense matrix throughput in hundreds of TFLOPS against single digits. On a spec sheet, read memory capacity (does it fit), memory bandwidth (decode speed) and dense BF16 or FP8 TFLOPS (prefill speed), in that order.

Fit table with real checkpoint sizes, 0.9 utilization, and 64 KB per token:

| GPU | Usable (×0.9) | BF16 55.6 GB | FP8 30.9 GB | NVFP4 23.4 GB |
| --- | --- | --- | --- | --- |
| RTX 5090 | 28.8 GB | no | no | 5.4 GB left, 82k tokens |
| L40S | 43.2 GB | no | 12.3 GB, 190k | 19.8 GB, 300k |
| A100 80GB | 72 GB | 16.4 GB, 250k | 41.1 GB, 630k | 48.6 GB, 740k |
| H100 SXM | 72 GB | 16.4 GB, 250k | 41.1 GB, 630k | 48.6 GB, 740k |

- A single consumer GPU holds this model only at 4 bits, with a thin cache. Qwen's own vLLM recipe agrees: one RTX 5090, NVFP4, FP8 KV cache, CUDA graphs off. It works, it is not comfortable.
- A 48 GB card holds FP8 with a workable cache. The cheapest configuration that serves the model as Qwen published it.
- An 80 GB card holds BF16, but BF16 buys accuracy you probably cannot measure and costs half the cache and half the decode speed. FP8 on 80 GB is the comfortable option.

Chapter 6 rents from this table and records the price on the day, next to measured tokens per second, for an honest dollars per million tokens.

## 8. A GPU is a Kubernetes resource

In chapter 1 the scheduler placed pods by comparing `resources.requests` to what each node had left. A GPU is one more line in that comparison. NVIDIA's device plugin runs as a DaemonSet on GPU nodes and reports cards to the kubelet as an extended resource, `nvidia.com/gpu`:

```
kubectl describe node <gpu-node> | grep -A 6 Allocatable
Allocatable:
  cpu:                16
  ephemeral-storage:  ...
  memory:             ...
  nvidia.com/gpu:     1
```

On the kind cluster the line is missing, which is correct: no plugin, no GPUs. A pod asks for one like this:

```yaml
resources:
  limits:
    nvidia.com/gpu: 1
```

Three rules that differ from cpu and memory:

- Whole units only. A container that gets a GPU gets all of it. Sharing (time-slicing, MIG) is opt-in.
- Requests and limits must be equal, so write it under `limits` only.
- No overcommit. A second GPU pod on a one-GPU node stays Pending with `Insufficient nvidia.com/gpu`.

The container also needs CUDA libraries matching the host driver, which is why model-server images are large. Chapter 6 builds the GPU node, installs the plugin, and applies the chapter 4 manifests with this one block added.

## Checkpoint

The script does sections 1, 3 and 5 for any model on Hugging Face. It downloads only the config, the file listing, and a few KB of header per weight file, so it runs in seconds.

```
uv run python chapters/02-llms-on-infrastructure/model_memory.py Qwen/Qwen3.8-27B
```

The lines that matter:

```
  text model loaded   26.896 B  (stored minus vision, draft head, scales, tied duplicate)
  BF16    53.8 GB
  this checkpoint's real files: 55.6 GB
  bytes per token         65,536  (64 KB)
```

The number is the BF16 weight size: about 54 GB. Run it on all three checkpoints and it prints the section 7 fit table with real file sizes:

```
uv run python chapters/02-llms-on-infrastructure/model_memory.py Qwen/Qwen3.8-27B Qwen/Qwen3.8-27B-FP8 unsloth/Qwen3.8-27B-NVFP4
```

When a new model comes out, this script is how you redo the chapter in ten seconds.

## What to remember

- Weights = parameters × bytes per parameter. Exact at BF16 and FP8, a floor at 4 bits. Size for the file you will download.
- The MLP is most of the model. Attention is most of the discussion.
- Prefill is compute-bound and sets TTFT. Decode is bandwidth-bound and sets tokens per second. A coding agent is prefill-heavy.
- Single-stream decode ceiling = bandwidth / weight bytes. Compute it before renting; measure against it after.
- KV cache = layers × 2 × KV heads × head dim × bytes, per token, per sequence. It grows with context and concurrency, and it gets what is left after the weights.
- Batching is nearly free until the cache is full. One user wastes a GPU; sixteen use it.
- A GPU is a schedulable resource like memory, requested in whole units under `limits`.
