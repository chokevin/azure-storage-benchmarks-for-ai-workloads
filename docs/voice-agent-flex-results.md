# Blob vs. Azure Managed Lustre for AI workloads

This page turns the `voice-agent-flex` benchmark results into general storage
guidance for AI workloads on AKS. The measurements are examples from one
cluster, not universal service limits.

## Short answer

- Use **Azure Blob Storage** as the durable, economical system of record for
  datasets, models, checkpoints, logs, and artifacts.
- Use **Azure Managed Lustre (AMLFS)** as a high-throughput shared working tier
  when many GPUs or nodes must stream an active dataset concurrently.
- Use **local NVMe or `emptyDir`** for temporary hot data and checkpoint staging.
- Do not leave a workload as millions of tiny files if throughput matters.
  Pack, shard, or stage the files regardless of the shared backend.

A common design is:

```text
Blob source of truth
        |
        | stage or hydrate the active dataset
        v
Azure Managed Lustre
        |
        | stream large training shards
        v
GPU nodes and local scratch
        |
        | asynchronously publish checkpoints and results
        v
Blob durable storage
```

## Blob and Lustre side by side

| Dimension | Azure Blob with BlobFuse/CSI | Azure Managed Lustre |
|---|---|---|
| Primary role | Durable object storage and system of record | Active high-performance shared filesystem |
| Best data shape | Large immutable objects, archives, model files, logs, and packed dataset shards | Large files read concurrently by distributed training or preprocessing jobs |
| Access model | Object storage presented through a filesystem adapter | Native parallel POSIX filesystem |
| Durability strategy | Keep the authoritative copy here | Treat as a working tier; archive or synchronize important data to Blob |
| Small-file behavior | Cold opens and metadata-heavy walks can be slow; cache state matters | Better filesystem semantics, but tiny-file metadata can still limit performance |
| Large sequential reads | Suitable for direct reads when concurrency and startup latency are acceptable | Designed for high aggregate throughput across clients and storage targets |
| Multi-node scaling | Each client reaches object storage through its mount and cache configuration | Clients share a striped parallel filesystem intended to scale with nodes and OSTs |
| Elastic compute | Easy to remount from new nodes if CSI, identity, and networking are configured correctly | Every eligible node must have network reachability and a working Lustre client |
| Cost profile | Usually the better long-term capacity tier | Higher-cost performance tier that should be sized for the active working set |
| Operational focus | Identity, network access, BlobFuse cache, mount options, and object-friendly layouts | Filesystem sizing, OST count, client consistency, mount reachability, and HSM/tiering |
| Choose it when | Durability, capacity, portability, and simple data exchange matter most | GPU utilization depends on feeding a large shared dataset at high aggregate bandwidth |

## Use-case guide

| Use case | Recommended storage | Why |
|---|---|---|
| Raw audio, images, video, and documents | Blob | Durable and economical for large immutable source objects |
| Training data retained for months | Blob | Long-term capacity matters more than POSIX metadata performance |
| Multi-node training over large shards | AMLFS, backed by Blob | Lustre serves the active working set with shared parallel reads |
| Millions of tiny JSON, token, or result files | Pack into larger shards, then use Blob or AMLFS | File-open and metadata overhead dominate either backend |
| Repeated epochs over the same active dataset | AMLFS or a deliberate cache tier | Avoid repeated cold object-store reads and improve aggregate throughput |
| Model distribution to a small number of pods | Blob, optionally staged locally | Simple durable source with local caching where needed |
| Frequent model loading across a large GPU fleet | AMLFS or a distributed cache over Blob | Shared high-throughput reads can reduce synchronized startup delays |
| Hot training checkpoints | Local NVMe first, then async copy to Blob | Keeps the training loop off the durable-storage critical path |
| Shared checkpoints needed immediately by many nodes | Benchmark AMLFS writes; publish durable copies to Blob | Lustre provides shared visibility, while Blob remains the durable destination |
| Logs, metrics, evaluation outputs, and final artifacts | Blob | These are durable outputs, not usually latency-critical shared working data |
| Voice-agent replay databases or mutable state | Managed disk or a database, with Blob backups | Object mounts are not a substitute for a transactional filesystem or database |

## What the benchmark showed

### Tiny files favor staging or packing

The measured voice/autoresearch path contained many small JSON artifacts:

| Path | Files | Total bytes | Shape |
|---|---:|---:|---|
| `/data/autoresearch` | 11,440 | 13.2 GB | 10,298 files under 4 KiB |
| `/data/autoresearch/results/gura` | 3,224 | 19.8 MB | 2,427 files under 4 KiB |

For a 3,000-file sample on BlobFuse:

| Read path | Files/s | MB/s |
|---|---:|---:|
| BlobFuse cold read | 140 | 0.861 |
| BlobFuse warm read | 8,144 | 50.001 |
| Local staged copy | about 100,000 | about 265 |

The large cold-to-warm difference means cache state can hide production startup
costs during repeated tests. The durable fix is to reduce object count or stage
the working set, not to depend on a warm cache.

### Lustre favored large, uniform training shards

A staged Dolma sample containing 100 gzip shards and about 174 GB of data read
from AMLFS at **3.810 GB/s** from one client. Enumeration took only 0.007 seconds
because the dataset used uniformly large files.

A two-client run reached about **4.28 GB/s aggregate**. The modest gain over one
client indicated that this specific two-OST filesystem was near its provisioned
ceiling. Lustre throughput depends on filesystem size, OST count, client
uniformity, and workload concurrency; adding GPU nodes does not automatically
add storage bandwidth.

A separate 64 GiB sequential-file sanity test read at about **6.3-6.5 GB/s** on
each tested node. That higher result confirms that file layout, enumeration, and
client behavior can limit dataset scans before the filesystem reaches its raw
sequential ceiling.

### Blob remained useful for checkpoints, but not always on the hot path

BlobFuse sustained roughly **0.31-0.49 GB/s** for the tested 20 GiB checkpoint
writes. Depending on the node, one checkpoint took about 41-61 seconds in the
isolated hot-path test.

Node-local `/mnt` reached **1.15 GB/s** on the tested H200 node, but local
storage is ephemeral and not shared. The safer training pattern is:

1. Write the hot checkpoint to local storage.
2. Resume training as soon as the local write is safe.
3. Upload the checkpoint to Blob asynchronously.
4. Retain or garbage-collect local copies based on recovery requirements.

AMLFS checkpoint writes can be appropriate when checkpoints must be visible to
multiple nodes immediately, but they should be benchmarked independently from
dataset reads and copied to Blob when durable retention is required.

## Observed comparison

These results are useful directional evidence, but they are not a controlled
service-level comparison. The same large dataset was not successfully measured
through both BlobFuse and AMLFS under identical conditions.

| Pattern | Blob observation | AMLFS observation | Practical conclusion |
|---|---|---|---|
| Cold tiny-file reads | 140 files/s for the sampled result files | Not measured on the same sample | Pack or stage tiny files; do not select a backend from headline GB/s |
| Warm tiny-file reads | 8,144 files/s with BlobFuse cache | Not measured on the same sample | Warm-cache tests are not a safe startup estimate |
| Large training shards | Same-dataset test was inconclusive because BlobFuse jobs timed out or had mount issues | 3.810 GB/s for a 174 GB single-client Dolma read | AMLFS is the stronger candidate for an active distributed training tier |
| Multi-client dataset reads | Not captured comparably | About 4.28 GB/s across two clients | Size AMLFS for aggregate demand; the tested two-OST instance was the bottleneck |
| Large sequential file | Not captured comparably | About 6.4 GB/s read per tested client | Dataset layout can matter as much as backend capability |
| 20 GiB checkpoint writes | About 0.31-0.49 GB/s | Native sanity writes were about 0.90-1.47 GB/s, using a different test | Benchmark the exact checkpoint path; prefer local staging when training stalls |

## Selection rules

Choose a **Blob-first design** when:

- the workload mostly reads large immutable objects;
- compute jobs are intermittent or highly elastic;
- long-term capacity and durability dominate;
- a single job or small fleet can tolerate mount/cache startup behavior; or
- the application can use object APIs directly instead of requiring POSIX.

Add **Azure Managed Lustre** when:

- a large GPU fleet repeatedly scans the same active dataset;
- measured storage stalls reduce GPU utilization;
- the dataset is already packed into large shards;
- aggregate bandwidth must scale beyond one client's object-store path; and
- the performance benefit justifies a dedicated, provisioned working tier.

Use **both** for large training platforms: Blob for the complete durable corpus,
AMLFS for the current training window, and local storage for temporary hot data.

## Validation checklist

Before adopting either design, measure the real workload:

- **File shape:** file count, median size, and small-file percentage.
- **Cold start:** first mount, first list, and first read on a new node.
- **Steady state:** repeated reads with explicit cache-state reporting.
- **Concurrency:** one client, expected client count, and peak synchronized load.
- **End-to-end impact:** GPU idle time, data-loader wait, and checkpoint pause.
- **Elastic placement:** mount and read validation on every eligible node class.
- **Durability:** recovery after pod loss, node loss, and working-tier loss.
- **Cost:** storage capacity, provisioned performance, data movement, and idle time.

For AMLFS specifically, validate the intended dataset scale, OST capacity,
cross-node performance consistency, and any HSM/Blob tiering workflow. A
successful mount or a single-client read is not enough to prove that an elastic
50-150 TB training design is ready.

## Benchmark provenance and caveats

The source measurements were collected on the historical `voice-agent-flex`
cluster. Current clusters, storage tiers, node networking, CSI versions, and
cache settings can produce different results.

Relevant cluster artifacts included:

```text
/data/storage-benchmarks/voice-path-profile-202604291825.{json,md}
/data/storage-benchmarks/voice-storage-compare-202604291833.{json,md}
/data/storage-benchmarks/storage-bench-async-checkpoint-202604291920.{json,md}
/data/storage-benchmarks/gpt2-async-20g-*.{json,md}
/data/storage-benchmarks/hot-checkpoint-*.{json,md}
```

Important limitations:

- Blob and AMLFS were not tested with the same large dataset in a clean,
  controlled side-by-side run.
- BlobFuse behavior depends on mount configuration, cache state, identity,
  network placement, and CSI/driver versions.
- AMLFS results came from a small two-OST deployment and included a consistently
  slower client node.
- The observed AMLFS dataset was hundreds of GB, not the intended 50-150 TB
  production scale.
- Local disk results are useful upper bounds and staging guidance, not durable
  shared-storage comparisons.
