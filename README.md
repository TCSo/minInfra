# minInfra

nanoGPT for serving: the smallest runnable path from an empty laptop to a GPU
cluster with a coding agent talking to it.

Deploy and run your own LLM serving infrastructure. Every step is done by hand,
explained, and measured, and every chapter ends in one number you can reproduce.

## Why this exists

Coding-agent subscriptions are cheap because they are subsidized. Running your own
models is not automatically cheaper, but it teaches you the whole stack: containers,
Kubernetes, GPU scheduling, inference engines, autoscaling, and performance tuning.
By the end you will know what every piece does, what it costs, and when self-hosting
actually wins (teams sharing a cluster, agent swarms, owned hardware, privacy).

The working model from chapter 5 on is Qwen3-8B-FP8: open weights, 9.4 GB, runs on
the cheapest GPU every cloud keeps in stock. Chapter 10 uses Qwen3-32B-FP8, which
does not fit one of those GPUs, to motivate splitting a model. The headline model,
Qwen3.8-27B, is the one the course reruns on a 48 GB or larger card whenever one can
be rented; the manifests do not change, only the fit arithmetic in chapter 2.

## Chapters

| #   | Chapter                                       | You will have                                                                                                                                             | You will measure                                                                                  | Status |
| --- | --------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- | ------ |
| 1   | Foundations: Docker, Kubernetes, kubectl      | A local multi-node cluster, a load-balanced Service, and the scheduler refusing a pod that does not fit                                                    | 3 Ready nodes, a curl response, pods that fit before the cluster is full                          | done   |
| 2   | LLMs on infrastructure                        | A mental model of memory, KV cache, prefill/decode, and why placement is hard                                                                             | Model size vs memory, worked out by hand                                                          | done   |
| 3   | Serving with vLLM (and how it compares)       | A model answering OpenAI-style requests, first on CPU                                                                                                     | tokens/sec, single stream                                                                         | done   |
| 4   | Deploying a model on the cluster              | Manifests, storage, health checks, a rolling update with requests in flight                                                                               | Startup time to first token                                                                       | done   |
| 5   | Moving to a cloud GPU cluster                 | The laptop's ceiling under concurrent load, a priced comparison of four providers, and the same manifests running Qwen3-8B-FP8 on a rented GPU, torn down at the end | 39 s vs 110 s to write a program at 1 vs 8 users on the laptop; on an A10G, 290 s to Ready and $6.35 → $0.25 per million tokens from 1 to 32 users within the latency target | done   |
| 6   | Autoscaling                                   | KEDA scaling vLLM replicas on the engine's own queue metric, Karpenter scaling the GPU node pool to zero, and what the endpoint costs while idle           | Seconds from load spike to new replica serving, idle $ per month                                  | next   |
| 7   | Performance                                   | Quantization, prefix caching, speculative decoding, and prefix-aware routing through the Kubernetes Gateway API Inference Extension                        | Before/after tokens/sec, TTFT with and without cache-aware routing                                | later  |
| 8   | Connect your coding agent                     | OpenCode / Cline / Claude Code pointed at your endpoint, recorded sessions as a replay benchmark                                                          | A completed task and its bill                                                                     | next   |
| 9   | Tuning the model on your own work             | RL fine-tuning with Ray: a trainer and vLLM rollout workers coordinated as one job                                                                       | Benchmark score before/after                                                                      | later  |
| 10  | Tensor and pipeline parallelism               | Qwen3-32B-FP8, which does not fit one 24 GB GPU, run as TP=2 and PP=2 across two single-GPU nodes with a LeaderWorkerSet; then TP over NVLink vs PCIe on one 4-GPU node | tokens/sec at 1 and 16 concurrent per layout, Ethernet vs PCIe vs NVLink delta                    | later  |
| 11  | Prefill/decode disaggregation                 | One prefill and one decode replica with KV transfer over NIXL, the Inference Extension routing in front                                                   | TTFT and per-token latency under mixed load vs one replica on the same GPUs                       | later  |

Chapters 1 to 4 use a small model on CPU so the mechanics cost nothing; chapter 5
moves the same manifests to a rented GPU, and every cloud chapter creates its
cluster at the start and deletes it at the end, so nothing bills between sessions.
The serving chapters use only Kubernetes primitives, vLLM, and the autoscaling and
routing projects built on them. Ray enters in chapter 9 for training, where it is
the standard tool.
Chapters 10 and 11 use two single-GPU nodes, so they need a G-instance quota of 16
vCPUs instead of chapter 5's 8.

"next" chapters are being written in order.

## What this guide does not cover

- RDMA. Chapters 10 and 11 run across nodes over ordinary Ethernet, which is what
  makes the interconnect cost visible; the InfiniBand and EFA variants are described,
  not validated.
- Serving many fine-tuned adapters per team (multi-LoRA).
- The platform layer around the endpoint: authentication, per-team rate limits and
  cost attribution, PII handling. Real, and out of scope for a curriculum.

## How to use this guide

- Each chapter lives in `chapters/NN-name/` with a README, the exact files used,
  and a checkpoint: one command whose output is the number that chapter is about.
- Do the steps yourself. Copying manifests without reading them defeats the point.
- Chapters 1 to 4 run on a laptop with Docker. GPU chapters say up front what
  hardware or cloud budget they need.
- Companion videos: **coming soon**.

## Prerequisites

- macOS or Linux, Docker, and a terminal you are comfortable in.
- No Kubernetes or ML background assumed.
- The laptop chapters were tested on a 2021 M1 Mac with 16 GB RAM.

## License

MIT, see [LICENSE](LICENSE).
