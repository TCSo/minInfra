# minInfra

Deploy and run your own LLM serving infrastructure, from an empty laptop to a
GPU cluster with a coding agent talking to it. Every step is done by hand, explained,
and measured.

## Why this exists

Coding-agent subscriptions are cheap because they are subsidized. Running your own
models is not automatically cheaper, but it teaches you the whole stack: containers,
Kubernetes, GPU scheduling, inference engines, autoscaling, and performance tuning.
By the end you will know what every piece does, what it costs, and when self-hosting
actually wins (teams sharing a cluster, agent swarms, owned hardware, privacy).

The default model throughout is Qwen3.8-27B: open weights, strong at coding, fits
on one GPU. More model support and guides incoming.

## Chapters

| # | Chapter | You will have | You will measure | v1 |
|---|---------|---------------|------------------|----|
| 1 | Foundations: Docker, Kubernetes, kubectl, Ray | A local multi-node cluster and the vocabulary to talk about it | 3 Ready nodes, a curl response | yes |
| 2 | LLMs on infrastructure | A mental model of memory, KV cache, prefill/decode, and why placement is hard | Model size vs memory, worked out by hand | yes |
| 3 | Serving with vLLM (and how it compares) | A model answering OpenAI-style requests, first on CPU | tokens/sec, single stream | yes |
| 4 | Deploying a model on the cluster | Manifests, storage, health checks, a stable endpoint | Startup time to first token | yes |
| 5 | Autoscaling | Replicas that follow load, scale-to-zero, the tradeoffs | Seconds from load spike to new replica serving | yes |
| 6 | Moving to a cloud GPU cluster | The same stack on rented GPUs, running Qwen3.8-27B | tokens/sec at 1 and 16 concurrent, $ per million tokens | yes |
| 7 | Performance | Quantization, prefix caching, speculative decoding, routing | Before/after tokens/sec and $ per million tokens | later |
| 8 | Connect your coding agent | OpenCode / Cline / Claude Code pointed at your endpoint | A completed task and its bill | yes |
| 9 | Tuning the model on your own work | RL fine-tuning with Ray | Benchmark score before/after | later |

Chapters 1 to 5 use a small model on CPU so the mechanics cost nothing; chapter 6
swaps in the real model on rented GPUs with the same manifests.

Chapters marked as placeholders are not written yet.

## How to use this guide

- Each chapter lives in `chapters/NN-name/` with a README, the exact files used,
  and a checkpoint: one command whose output is the number that chapter is about.
- Do the steps yourself. Copying manifests without reading them defeats the point.
- Chapters 1 to 5 run on a laptop with Docker. GPU chapters say up front what
  hardware or cloud budget they need.
- Companion videos: (link placeholder).

## Prerequisites

- macOS or Linux, Docker, and a terminal you are comfortable in.
- No Kubernetes or ML background assumed.

## License

MIT, see [LICENSE](LICENSE).
