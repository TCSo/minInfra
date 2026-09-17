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

The default model throughout is Qwen3.8-27B: open weights, strong at coding, fits
on one GPU. More model support and guides incoming.

## Chapters

| #   | Chapter                                       | You will have                                                                                                                                             | You will measure                                                                                  | Status |
| --- | --------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- | ------ |
| 1   | Foundations: Docker, Kubernetes, kubectl, Ray | A local multi-node cluster and the vocabulary to talk about it                                                                                            | 3 Ready nodes, a curl response                                                                    | done   |
| 2   | LLMs on infrastructure                        | A mental model of memory, KV cache, prefill/decode, and why placement is hard                                                                             | Model size vs memory, worked out by hand                                                          | done   |
| 3   | Serving with vLLM (and how it compares)       | A model answering OpenAI-style requests, first on CPU                                                                                                     | tokens/sec, single stream                                                                         | done   |
| 4   | Deploying a model on the cluster              | Manifests, storage, health checks, a rolling update with requests in flight                                                                               | Startup time to first token                                                                       | done   |
| 5   | Moving to a cloud GPU cluster                 | The laptop's ceiling under concurrent load, a priced comparison of four providers, and the same manifests running Qwen3.8-27B on a rented GPU             | Seconds to write a program at 1 vs 8 concurrent; tokens/sec at 1 and 16, $ per million tokens     | next   |
| 6   | Autoscaling                                   | Ray Serve LLM on KubeRay: replicas that follow load, scale-to-zero for pods and GPU nodes, and what the endpoint costs while idle                          | Seconds from load spike to new replica serving, idle $ per month                                  | next   |
| 7   | Performance                                   | Quantization, prefix caching, speculative decoding, prefix-aware routing across replicas                                                                  | Before/after tokens/sec, TTFT with and without cache-aware routing                                | later  |
| 8   | Connect your coding agent                     | OpenCode / Cline / Claude Code pointed at your endpoint, recorded sessions as a replay benchmark                                                          | A completed task and its bill                                                                     | next   |
| 9   | Tuning the model on your own work             | RL fine-tuning with Ray                                                                                                                                   | Benchmark score before/after                                                                      | later  |
| 10  | GPU topology, tensor and pipeline parallelism | Two GPUs read with `nvidia-smi topo -m`; the model as TP=2, PP=2, and two replicas, placed with Ray bundling strategies                                   | tokens/sec at 1 and 16 concurrent per layout, NVLink vs PCIe delta                                | later  |
| 11  | Prefill/decode disaggregation                 | One prefill and one decode replica with KV transfer over NIXL, prefix-affinity routing in front                                                           | TTFT and per-token latency under mixed load vs one replica on the same GPUs                       | later  |

Chapters 1 to 4 use a small model on CPU so the mechanics cost nothing; chapter 5
swaps in the real model on rented GPUs with the same manifests, and everything from
chapter 6 on runs on that one cloud cluster, which scales to zero when you are not
using it. Chapters 10 and 11 need a node with two GPUs; their placement exercises
run on the laptop cluster first.

"next" chapters are being written in order.

## What this guide does not cover

- Cross-node tensor parallelism and cross-node KV transfer over RDMA. Chapters 10 and
  11 stay on one node; the multi-node variants are described, not validated.
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
