# Rune Azure storage defaults for AI jobs

This guidance turns the `voice-agent-flex` benchmark results into practical
defaults for Rune jobs on Azure. The default shape is:

```text
Blob is durable. /mnt is hot. Copy between them deliberately.
```

## Default mount layout

| Mount | Backing storage | Use for | Default behavior |
|---|---|---|---|
| `/data` | Azure Blob mounted with BlobFuse | Durable source of truth | Canonical datasets, durable checkpoints, logs, outputs, and cross-node resume. Do not use as the hot path for tiny-file reads or 20 GiB checkpoints when a faster local path is available. |
| `/mnt` | Node-local host disk | Hot job-local storage | Staged datasets, packed shards, checkpoint temp files, tokenizer/cache files, and intermediate transforms. Treat as ephemeral and node-scoped. |
| `/scratch` | Kubernetes `emptyDir` | Small temporary pod scratch | Use only when explicitly sized and tested. Do not silently use kubelet-root `emptyDir` for large checkpoints. |

## Read path default

Use Blob as the durable dataset store, but train from staged local data.

1. Keep canonical datasets under `/data/datasets/...`.
2. At job startup, stage the required dataset subset to
   `/mnt/datasets/<dataset-fingerprint>/...`.
3. Train from `/mnt`, not directly from BlobFuse, for hot read loops.
4. Reuse staged data by dataset fingerprint when the local copy is complete and
   still valid.
5. Fall back to direct `/data` reads only for small jobs, large sequential reads,
   or when `/mnt` is unavailable.

For many small files, Rune should prefer packing before training:

- WebDataset/tar shards for image/audio/sample bundles.
- JSONL for line-oriented text records.
- Parquet or Arrow for tabular/tokenized data.
- LMDB or larger binary shards when random access is needed.

The voice/autoresearch benchmark found cold BlobFuse tiny-file reads around
hundreds of files per second, while staged local reads were roughly five orders
of magnitude faster. Loader tuning is not enough to close that gap.

## Checkpoint write default

Use two-tier checkpointing: write locally first, then copy to Blob in the
background.

1. Write to `/mnt/checkpoints/<job-id>/checkpoint.tmp`.
2. Flush, fsync, and rename locally to `checkpoint.pt`.
3. Queue a background durable copy to
   `/data/checkpoints/<job-id>/checkpoint.pt`.
4. Write or update the durable manifest only after the Blob copy succeeds.
5. Keep the last N local checkpoints and prune only after durable upload
   confirmation.
6. Resume from local only if the job is on the same node and the local checkpoint
   is complete and newer. Otherwise resume from Blob.

## Current node-class policy

| Node class | Hot checkpoint path | Durable path | Current finding |
|---|---|---|---|
| H200 flex | `/mnt` | `/data` async copy | Best observed hot path: about 1.15 GB/s for 20 GiB checkpoints. |
| A100 VMSS | `/mnt` | `/data` async copy | Faster than BlobFuse: about 0.79 GB/s versus about 0.49 GB/s. |
| H100 flex | Probe first; fall back to `/data` | `/data` async copy | One H100 host `/mnt` run was slower than BlobFuse. Do not assume `/mnt` is always faster on this node class. |

Rune should default to `/mnt`, but it must verify the local path before using it.

## Startup guardrails

Before selecting `/mnt` as the hot path, Rune should check:

1. `/mnt` exists and is writable.
2. Free space covers the expected dataset staging size plus checkpoint retention:
   `checkpoint_size * (local_retention + 1)`.
3. A small fsync write probe meets the minimum throughput threshold for the job.
   A 1-2 GiB probe is enough for startup classification; use the benchmark suite
   for full 20 GiB validation.
4. The path is not kubelet-root `emptyDir` unless the job explicitly requested
   that behavior and the storage request/limit is sized.

If any check fails, Rune should fall back to `/data` and emit a visible
warning/event. It should not silently write large checkpoints to a slow or
capacity-limited path.

## Known platform gaps

- Azure Disk attached successfully on the A100 AKS VMSS pool, but the tested
  Standard/Premium 1 TiB classes were slower than BlobFuse for 20 GiB
  fsync-heavy checkpoint writes.
- Azure Disk did not mount on flex H100/H200 nodes during this benchmark. Those
  nodes report provider IDs like
  `azure-flex:///.../Microsoft.Compute/virtualMachines/flex-*`, while the A100
  pool reports VMSS provider IDs. The failing path belongs in the flex/Karpenter
  adapter, cloud-provider, or Azure Disk CSI integration layer.
- Blob NFS v3 failed to mount from the H100 flex path with access denied. Re-test
  after the network/source-path issue is fixed.

These gaps should not block the default Rune policy. Use local-hot plus
Blob-durable now, and treat faster shared or attached storage as future
optimization work.

## Benchmark roadmap for Rune

The next useful benchmark waves are:

| Area | Question to answer | Done when |
|---|---|---|
| `/mnt` inventory | Which node classes have fast, large, safe local storage? | Every node class has capacity, filesystem, 1-2 GiB probe, and 20 GiB checkpoint results. |
| Read staging and packing | Which packed format should Rune prefer for tiny-file datasets? | Direct BlobFuse, copy-as-is staging, tar/WebDataset, JSONL, Parquet/Arrow, and LMDB-style options are compared on the voice/autoresearch shape. |
| Async durable upload | Which copy path should Rune use from `/mnt` to Blob? | Python copy, shell copy, Blob SDK/block upload, and azcopy are compared for foreground latency and background throughput. |
| Resume/failure semantics | Can Rune recover from interruption during write/upload/manifest update? | Same-node local resume and cross-node Blob resume are proven with kill tests. |
| Multi-job contention | How many jobs can stage/upload before Blob becomes the bottleneck? | Concurrent dataset staging and checkpoint upload thresholds are measured. |
| Inference/model load | Should inference jobs stage weights/adapters/tokenizers? | Cold and warm model-load timings are measured from BlobFuse and `/mnt`. |
| Alternative storage | Is there a better managed/shared hot path? | Larger/faster disks, Premium SSD v2, Ultra Disk, Blob NFS, Azure Files Premium, and Azure NetApp Files are compared where available. |

## Bottom line

Rune jobs on Azure should use:

- `/data` for durable datasets, durable checkpoints, logs, and outputs.
- `/mnt` for hot dataset reads, temporary transforms, caches, and local
  checkpoint writes.
- Background copy from `/mnt` to `/data` for durability.
- Startup probes and visible fallbacks so a slow or missing `/mnt` does not
  silently hurt training.
