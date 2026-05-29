# voice-agent-flex benchmark notes

This page records the real workload-shaped benchmark runs from the
`voice-agent-flex` AKS cluster. These are not generic storage claims; they are
evidence for one observed voice/autoresearch access pattern.

## Why this was measured

Heavy transcription and voice-agent training work appeared slow when reading
from the Blob-backed `/data` mount. The question was whether Blob itself was
slow for bulk reads, or whether the workload was hitting a more specific
small-file metadata/read pattern.

The investigation found that the hot path was not the STT serving pod directly:

- `fish-speech`, `gura-bot`, `gura-llm`, and `hutao-bot` mount
  `blob-training` at `/data`.
- `stt-serving` does not mount the Blob PVC.
- The autoresearch training scripts reference paths such as
  `/data/training/autoresearch`, `/data/autoresearch/results/...`, and
  `/data/replay-logs`.

## Data shape

The slow path is dominated by many tiny JSON/result artifacts under
`/data/autoresearch`, especially `/data/autoresearch/results/gura`.

| Path | Files | Bytes | Shape |
|---|---:|---:|---|
| `/data/autoresearch` | 11,440 | 13,219,591,738 | 10,298 files are under 4 KiB; mostly JSON plus model/tokenizer artifacts |
| `/data/autoresearch/results/gura` | 3,224 | 19,794,106 | 2,427 files are under 4 KiB; 778 files are 4-64 KiB |
| `/data/training/autoresearch` | 97 | 268,636,529 | Mostly JSONL training files; not the main tiny-file hotspot |
| `/data/replay-logs` | 6 | 24,088,736 | SQLite database files |

The benchmark samples below use the `gura` results directory because it
captures the painful tiny-file shape without reading the whole container.

## Reproduction-oriented profile

Artifact on the cluster:

```text
/data/storage-benchmarks/voice-path-profile-202604291825.{json,md}
```

The profile read up to 3,000 small files from each candidate path and compared
BlobFuse first-pass reads, BlobFuse warm reads, and the same staged sample on
node-local scratch.

| Path | BlobFuse first pass | BlobFuse warm pass | Local staged sample |
|---|---:|---:|---:|
| `/data/autoresearch` | 134 files/s, 0.326 MB/s | 8,280 files/s, 20.1 MB/s | 99k files/s, 241 MB/s |
| `/data/autoresearch/results/gura` | 135 files/s, 0.341 MB/s | 8,316 files/s, 21.0 MB/s | 96k files/s, 244 MB/s |

Interpretation: the observed slowness is cold small-file access through
BlobFuse. Warm-cache numbers are much better, which means repeated reads can
hide the issue during local testing or later epochs.

## Storage type comparison

Artifact on the cluster:

```text
/data/storage-benchmarks/voice-storage-compare-202604291833.{json,md}
```

This comparison sampled 3,000 files from
`/data/autoresearch/results/gura`, totaling 8,001,400 bytes with each file under
64 KiB. The exact sample was then staged to each target before measuring list
and read behavior.

| Target | FS | Stage s | List files/s | First read files/s | Warm read files/s | First read MB/s | Warm read MB/s |
|---|---|---:|---:|---:|---:|---:|---:|
| BlobFuse source | fuse |  | 7,423 | 140 | 8,144 | 0.861 | 50.001 |
| Blob NFS v3 | nfs | 130.789 | 891 | 155 | 183 | 0.412 | 0.487 |
| Standard Azure Disk | ext4 | 30.486 | 231,674 | 100,594 | 104,789 | 268.298 | 279.487 |
| Premium Azure Disk | ext4 | 0.622 | 230,758 | 101,902 | 105,302 | 271.787 | 280.853 |
| local `emptyDir` | ext4 | 0.615 | 241,639 | 99,449 | 101,909 | 265.242 | 271.804 |

Ranking for this access pattern:

```text
Azure Disk ~= local scratch >>> warm BlobFuse >>> cold BlobFuse ~= Blob NFS v3
```

Blob NFS v3 helped the earlier directory-listing-heavy synthetic run, but it did
not help this tiny-file read/open pattern. For this voice/autoresearch sample,
the practical fix is to stage or pack the tiny files before heavy training or
transcription loops.

## Lustre CSI training-data read plan

Lustre was the remaining backend not captured in the earlier `voice-agent-flex`
storage runs. With the Lustre CSI driver now mounted, use the read-only dataset
benchmark instead of the generated read/write harness so the run measures the
actual training-data layout and does not write benchmark payloads into Lustre.

Example manifest:

```bash
kubectl apply -f examples/kubernetes/lustre-training-data-read-job.yaml
```

Before applying, edit the manifest if needed:

- `claimName: lustre-training-data` should match the Lustre CSI PVC.
- `dataset-root: /lustre/training-data` should contain `1tib`, `5tib`,
  `10tib`, and `50tib` directories, or the job paths should be adjusted to the
  real dataset directories.
- The default jobs target `gpu: h100` nodes with `concurrency=16` and
  `block-size-mib=16`. Increase concurrency only if the pod has enough CPU and
  the Lustre server/client network can absorb the extra read pressure.
- On the live `lustre-h200-training` PVC, the mount succeeded on
  `flex-h200-eastus2euap-c8r87` but failed on `flex-h200-nxft5` with a Lustre
  MGS input/output error. If you use that PVC, pin to a verified node or update
  node placement after confirming the Lustre endpoint is routable from the node.

The four jobs write Markdown and JSON reports to:

```text
/data/storage-benchmarks/lustre-training-data-read-{1tib,5tib,10tib,50tib}-*.{json,md}
```

The benchmark reports both enumeration time and read time. Use read-time GB/s as
the storage throughput signal; use wall time when planning end-to-end training
startup or epoch scan cost. Multi-iteration runs are intentionally avoided here
because later passes may be served from page cache instead of Lustre.

### AMLFS strategy validation gaps

The target storage strategy is Azure Managed Lustre Filesystem (AMLFS) for an
active 50-150 TB training dataset, with Blob sync through HSM tiering, under an
elastic GPU reallocation model. The dataset-read benchmark validates the read
path only after a pod has mounted AMLFS. It is not sufficient by itself for that
strategy.

Before claiming the strategy holds, validate:

- **Dataset scale:** the mounted AMLFS path actually contains the intended
  50-150 TB active dataset. A live `/lustre` probe on 2026-05-27 selected only
  433,475,177,051 bytes, so a 50 TiB cap completed after reading all available
  data rather than proving a 50 TiB dataset scan.
- **Larger open-data staging:** use
  `examples/kubernetes/dolma-amlfs-stage-and-benchmark-job.yaml` to stage a
  capped slice of Allen AI's Dolma corpus onto AMLFS before benchmarking. Dolma
  v1.7 is documented as 4.5 TB gzip under ODC-BY, with upstream source
  license/terms caveats. The example defaults to only 100 URL-list entries for
  safety; increase `url-limit` and `parallel-downloads` deliberately when moving
  from a smoke slice to a multi-TB validation. This example was executed on
  2026-05-28 (see the Dolma result below): the default 100-URL slice staged
  174,479,448,489 bytes of `books`/`c4` gzip shards onto AMLFS and benchmarked
  cleanly.
- **Elastic placement:** every node class that can receive the training workload
  can mount the same AMLFS endpoint. The `amlfs-elastic-validation-job.yaml`
  example pins one validation job per candidate flex H200 node so mount failures
  are visible.
- **HSM/Blob tiering:** the pod image must include Lustre client tools. The
  `python:3.12-slim` image used by the simple examples does not include `lfs`,
  so it can validate mount/read behavior but not `lfs hsm_state` or archive
  state. Use `azure-storage-benchmark amlfs-validate` with a Lustre-capable image
  to capture HSM state signals.

Live AMLFS validation on 2026-05-27:

- `lustre-read-{1,5,10,50}tib-live` all completed on
  `flex-h200-eastus2euap-c8r87`, but each cap read the same available 433 GB
  dataset rather than distinct 1/5/10/50 TiB datasets.
- `amlfs-validate-h200-c8r87` completed and sampled 1,000 files totaling
  413,283,828,424 bytes. The mount source was `10.247.2.5@tcp:/lustrefs`.
- `amlfs-validate-h200-nxft5` and `amlfs-validate-h200-vhkcm` failed to mount the
  same PVC with `mount.lustre ... Input/output error; Is the MGS running?`.
- `amlfs-validate-h200-glzff` also completed and sampled the same 1,000 files
  totaling 413,283,828,424 bytes from `10.247.2.5@tcp:/lustrefs`.
- EUAP-only read reruns on the verified nodes both completed over the available
  433,475,177,051-byte dataset:

  | Node | Run ID | Enumerate s | Read s | Wall s | Read GB/s |
  |---|---|---:|---:|---:|---:|
  | `flex-h200-eastus2euap-c8r87` | `lustre-euap-c8r87-read-20260528022917` | 67.418 | 120.601 | 188.020 | 3.594 |
  | `flex-h200-eastus2euap-glzff` | `lustre-euap-glzff-read-20260528022934` | 870.815 | 270.483 | 1141.297 | 1.603 |

  The glzff run mounted successfully but spent much longer enumerating the same
  tree and read at less than half the c8r87 throughput, so the EUAP-only set is
  mount-compatible but not performance-uniform.
- A native large-file sanity check on the same AMLFS mount wrote and read a
  temporary 64 GiB sequential file per EUAP H200 node:

  | Node | Run ID | Write GB/s | Read GB/s |
  |---|---|---:|---:|
  | `flex-h200-eastus2euap-c8r87` | `lustre-native-sanity-c8r87-20260528072806` | 1.474 | 6.334 |
  | `flex-h200-eastus2euap-glzff` | `lustre-native-sanity-glzff-20260528072940` | 0.902 | 6.485 |

  This supports the interpretation that the lower 1.6-3.6 GB/s dataset-read
  numbers are driven by dataset shape, enumeration, and client behavior rather
  than a hard AMLFS sequential-read ceiling. Large-file single-client reads were
  around 6.4 GB/s from both EUAP nodes, while writes were lower and varied by
  node.
- A same-node BlobFuse2 comparison job on `flex-h200-eastus2euap-c8r87` mounted
  `blob-training` at `/data`, but that mount exposed no files on the EUAP H200
  node. The run completed with zero selected bytes:
  `blobfuse-training-read-compare-20260528030421`. This means the current
  cluster state cannot produce a same-dataset BlobFuse2-vs-AMLFS training-data
  comparison from the EUAP H200 nodes; the Blob container contents must first be
  made visible there or the same dataset must be staged under BlobFuse2.
  Follow-up probes showed the same `blob-training` PV exposes data on AKS CPU and
  A100 nodes, while flex H200 mounts are empty. Direct `az storage blob list`
  from `flex-h200-eastus2euap-c8r87` using the same `azure-blob-secret`
  credentials listed the container contents, so credentials and storage-account
  network reachability are valid. A fresh static Blob CSI PV/PVC with a unique
  volume handle and explicit BlobFuse2 protocol/cache options still mounted empty
  on flex. Treat this as a Blob CSI / BlobFuse2 flex-node mount behavior issue,
  not as a storage-account access issue. Blob CSI logs on the flex node also
  reported `error parsing volume id: "voiceagenttraining_training-data_flex",
  should at least contain two #`, which points at the static PV
  `volumeHandle`/driver parsing path as one issue. A corrected static PV using
  `volumeHandle: voiceagenttraining#training-data#flexfixed` was accepted by
  Blob CSI and mounted successfully on both EUAP H200 nodes, but still listed
  zero files; the driver logged a successful mount with
  `--container-name=training-data`. This narrows the remaining blocker to
  BlobFuse2/Blob CSI behavior on flex after a successful mount, or to using a
  supported dynamically provisioned Blob CSI configuration. No Blob CSI
  StorageClass was present in the cluster during this test.
- A later live remount of the original `blob-training` PV on
  `flex-h200-eastus2euap-c8r87` did expose the Blob container contents at
  `/data`, matching the Blob CSI node plugin `globalmount`. After that, two
  BlobFuse2 read jobs were started on the same node:
  `blobfuse-euap-c8r87-read` with the same 50 TiB cap as the Lustre run and
  `blobfuse-euap-c8r87-read-64g` with a 64 GiB cap. A later 1 GiB cap job was
  also started to get a bounded result. The mounted BlobFuse2 path showed 280
  top-level entries, so the jobs were not empty-mount failures; they were still
  spending time in the recursive selection/read path. The 64 GiB cap job hit its
  7,200 second active deadline with no report, and the 1 GiB cap job hit its
  1,800 second active deadline with no report. The full 50 TiB-cap job also hit
  its 21,600 second active deadline with no report. This is much slower
  operationally than the AMLFS c8r87 run, which read the whole available 433 GB
  dataset in 120.601 seconds of read time.
- HSM command observability was partially validated on
  `flex-h200-eastus2euap-c8r87` with a bounded host-root debug probe against a
  live pod mount. The normal benchmark image still lacked `lfs`, and running
  `lfs` inside the CSI container against host paths reported "Not a Lustre
  filesystem" because it was outside the host mount namespace. From the node
  mount namespace, `/usr/bin/lfs` confirmed the mounted AMLFS source
  `10.247.2.5@tcp:/lustrefs`, `lfs df -h` filesystem summary `15.7T` total /
  `393.4G` used / `14.5T` available, and two OSTs with about 196-197.5 GiB used
  each. `lfs getstripe` reported progressive layout with 1 MiB stripe size:
  stripe count 1 for 0-1 GiB, 5 for 1-100 GiB, 10 for 100-500 GiB, and `-1`
  after 500 GiB. `lfs hsm_state` on one sample file returned `(0x00000000)`, and
  `lfs hsm_archive` returned `Operation not permitted`. This proves `lfs` can
  inspect the mounted filesystem from the node and that the sampled file has no
  HSM flags, but it does not prove Blob tiering is active; repeatable HSM
  validation still needs a benchmark pod image with Lustre client tools and
  operator-approved HSM permissions.
- An open-source Dolma staging run on 2026-05-28 executed
  `examples/kubernetes/dolma-amlfs-stage-and-benchmark-job.yaml` on
  `flex-h200-eastus2euap-c8r87`. The default 100-URL slice staged
  174,479,448,489 bytes of Dolma v1.7 `books`/`c4` gzip shards (100 files, all
  >= 1 GiB) under `/lustre/datasets/dolma/v1_7`, then ran `dataset-read`:

  | Node | Run ID | Files | Bytes read | Enumerate s | Read s | Wall s | Read GB/s | Read GiB/s |
  |---|---|---:|---:|---:|---:|---:|---:|---:|
  | `flex-h200-eastus2euap-c8r87` | `dolma-v1_7-amlfs-c8r87-20260528200226` | 100 | 174,479,436,201 | 0.007 | 45.797 | 45.804 | 3.810 | 3.548 |

  This is the cleanest dataset-read result on AMLFS so far: a uniformly
  large-file (>= 1 GiB) corpus enumerated in 0.007 s, removing the
  enumeration penalty seen on the mixed 433 GB tree, and sustained 3.810 GB/s
  from a single client. It confirms a reproducible open-data path
  (stage capped Dolma slice, then benchmark) and matches the large-file sanity
  interpretation that earlier 1.6-3.6 GB/s numbers were enumeration/shape/client
  limited rather than an AMLFS sequential-read ceiling. It is still a ~174 GB
  single-client read, not a 50-150 TB multi-client scan.
- A multi-pod aggregate test on 2026-05-28 split the staged Dolma slice into two
  disjoint halves (hardlinked, 50 files each) and ran one `dataset-read` pod per
  EUAP H200 node at the same time, barrier-synced to start together:

  | Node | Run ID | Bytes read | Read s | Read GB/s |
  |---|---|---:|---:|---:|
  | `flex-h200-eastus2euap-c8r87` | `dolma-multi-c8r87-20260528211203` | 86,930,401,330 | 31.551 | 2.755 |
  | `flex-h200-eastus2euap-glzff` | `dolma-multi-glzff-20260528211203` | 87,549,034,871 | 57.387 | 1.526 |

  Peak aggregate while both pods read at once was about 4.28 GB/s (~34 Gbps).
  Two things stand out. Adding a second client raised the aggregate only a
  little (3.810 -> ~4.28 GB/s) and the fast node dropped from 3.810 to
  2.755 GB/s once it shared the disks, so this filesystem is near its
  provisioned ceiling: host-namespace `lfs df` showed 15.7 TiB over two OSTs.
  And `glzff` read at ~1.5 GB/s here and in the earlier rerun (1.603 GB/s), so
  it is a consistently slow node. Lustre's "tens of GB/s" numbers assume many
  OSTs and many uniform clients; reaching ~40 GB/s here would need a larger or
  higher-tier AMLFS with more OSTs, not a mount-option change on this two-OST
  instance. The hardlinked shard directories remain at
  `/lustre/datasets/dolma/v1_7_shards/{a,b}` for repeat runs (no extra space;
  they reference the same inodes as `/lustre/datasets/dolma/v1_7`).

## Async checkpoint-style write comparison

Artifact on the cluster:

```text
/data/storage-benchmarks/storage-bench-async-checkpoint-202604291920.{json,md}
```

This run used the benchmark's async checkpoint mode with:

- 4 checkpoint files.
- 256 MiB per checkpoint file.
- 1 GiB total checkpoint payload.
- 5 seconds of simulated training compute overlapped with the background writer.

`Async writer GB/s` is the actual background write throughput. `Async wait ms`
is how long the caller waited after the 5 second overlap window. `Async blocked
GB/s` uses only submit time plus post-overlap wait time, so it can look much
higher than physical storage throughput when the write is fully hidden behind
compute.

| Target | Sync checkpoint GB/s | Async writer GB/s | Async wait ms after 5s overlap | Async blocked GB/s | Interpretation |
|---|---:|---:|---:|---:|---|
| BlobFuse | 0.121 | 0.129 | 3,291 | 0.326 | 5 seconds of overlap hid part, but not all, of the write |
| Blob NFS v3 | 0.243 | 0.245 | 0.021 | 1,934.973 | Writer finished inside the overlap window |
| Standard Azure Disk | 0.144 | 0.143 | 2,487 | 0.432 | Disk writer exceeded the 5 second overlap window |
| Premium Azure Disk | 0.163 | 0.164 | 1,551 | 0.692 | Faster than Standard Disk but still not fully hidden |
| local `emptyDir` | 0.922 | 0.826 | 0.019 | 2,084.903 | Fully hidden inside the overlap window |

For this checkpoint size, async checkpointing changes the question from "how
fast can storage write 1 GiB?" to "does the write fit inside the available
compute overlap window?" Local scratch and Blob NFS v3 fit inside this 5 second
window in this run; BlobFuse and the tested Azure Disks still had visible
post-overlap wait time.

## GPT-style PyTorch async checkpoint sample by GPU SKU

Artifacts on the cluster:

```text
/data/storage-benchmarks/gpt2-async-large-a100-20260429200040.{json,md}
/data/storage-benchmarks/gpt2-async-large-h100-20260429200040.{json,md}
/data/storage-benchmarks/gpt2-async-large-h200-20260429200040.{json,md}
```

This run used `examples/pytorch/gpt2_async_checkpoint.py`, a tiny byte-level
GPT-style PyTorch language model trained on Hugging Face `roneneldan/TinyStories`
text. The sample used:

- 30 training steps.
- 16 samples per batch.
- 256 byte-token sequence length.
- 4 transformer layers, 4 attention heads, 256 embedding dimensions.
- 3 async checkpoints, one every 10 steps.
- 2,048 MiB padding tensor per checkpoint to make checkpoint writes visible.
- Checkpoints written to BlobFuse under `/data/storage-benchmarks`.

The model itself is only 3,356,160 parameters, or about 13 MiB of FP32 parameter
tensors. The large checkpoint size below is intentionally simulated with padding
so the run exercises checkpoint I/O behavior without requiring a large model.

| GPU SKU | Device | Checkpoint bytes each | Pure step tokens/s | Loop tokens/s incl. checkpoint waits | Median step s | Async writer GB/s | Async caller-blocked s | Sync final GB/s |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| A100 | NVIDIA A100-SXM4-80GB | 2,187,816,968 | 164,392 | 4,561 | 0.011913 | 0.184579 | 35.391502 | 0.148870 |
| H100 | NVIDIA H100 NVL | 2,187,816,968 | 345,391 | 8,200 | 0.005785 | 0.292561 | 22.341305 | 0.409877 |
| H200 | NVIDIA H200 | 2,187,816,968 | 300,198 | 6,807 | 0.005264 | 0.250062 | 26.179885 | 0.224126 |

`Pure step tokens/s` sums only the timed forward/backward/update steps. `Loop
tokens/s` includes the whole loop, including waits for the prior async checkpoint
before submitting the next checkpoint and the final drain at shutdown. That is
why loop throughput mostly follows BlobFuse checkpoint write speed in this run:
H100 had both the highest pure step throughput and the fastest BlobFuse
checkpoint writes, while H200 had a slightly faster median step than H100 but
slower total step time and slower checkpoint writes.

This larger run replaced an earlier 256 MiB-padding sanity check whose ~309 MB
checkpoints were too small to make a useful training/checkpoint overlap claim.
The earlier numbers also reported a single `tokens/s` value that included
checkpoint waits, which made GPU comparisons look contradictory when placed next
to median step time.

### 20 GiB checkpoint probe

Artifact on the cluster:

```text
/data/storage-benchmarks/gpt2-async-20g-a100-20260429204054.{json,md}
/data/storage-benchmarks/gpt2-async-20g-h100-20260429202505.{json,md}
/data/storage-benchmarks/gpt2-async-20g-h200-20260429204054.{json,md}
```

After the 2 GiB-padding GPU-SKU run, the same sample was run with 20,480 MiB of
checkpoint padding. The A100 and H200 jobs were run sequentially after the H100
probe to avoid deliberate cross-SKU contention against the same BlobFuse mount.

| GPU SKU | Device | Checkpoint bytes each | Async checkpoint total GB | Pure step tokens/s | Loop tokens/s incl. checkpoint waits | Async writer GB/s | Async caller-blocked s | Sync final GB/s |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| A100 | NVIDIA A100-SXM4-80GB | 21,515,171,192 | 64.546 | 173,382 | 982 | 0.329044 | 196.174834 | 0.369199 |
| H100 | NVIDIA H100 NVL | 21,515,171,192 | 64.546 | 345,706 | 1,380 | 0.483925 | 133.288581 | 0.492306 |
| H200 | NVIDIA H200 | 21,515,171,192 | 64.546 | 299,068 | 892 | 0.307572 | 209.887191 | 0.322191 |

The larger checkpoint writes behaved more like sustained sequential writes than
the smaller 2 GiB-padding run. H100 was the fastest BlobFuse writer in this
capture, at roughly 0.48-0.49 GB/s for both async and sync writes. A100 and H200
were slower on the same BlobFuse path, around 0.31-0.37 GB/s depending on async
or sync timing. In all cases the caller still spent minutes blocked across three
async checkpoints because the toy model's training steps are far too short to
hide ~21.5 GB checkpoint writes.

### Hot checkpoint path candidates

Artifacts on the cluster:

```text
/data/storage-benchmarks/hot-checkpoint-h100-hostmnt-202604292140.{json,md}
/data/storage-benchmarks/hot-checkpoint-a100-disk-202604292145.{json,md}
/data/storage-benchmarks/hot-checkpoint-h200-hostmnt-202604292155.{json,md}
```

These runs use the same 20,480 MiB checkpoint shape, but isolate storage target
behavior without the PyTorch model. Each sample writes a temp file, flushes,
fsyncs, renames it into place, fsyncs the directory, then removes the checkpoint
payload.

| Node class | Target | FS/source | Mean GB/s | Mean seconds per 20 GiB checkpoint | Notes |
|---|---|---|---:|---:|---|
| H200 flex | host `/mnt` | ext4 `/dev/sdb1` | 1.146984 | 18.730 | Fastest observed hot path; ephemeral/local to node |
| A100 VMSS | host `/mnt` | ext4 `/dev/sdb1` | 0.785619 | 27.360 | Faster than BlobFuse on the A100 pool; ephemeral/local to node |
| H100 flex | BlobFuse `/data` | fuse `blobfuse2` | 0.524992 | 41.063 | Faster than H100 host `/mnt` in this capture |
| A100 VMSS | BlobFuse `/data` | fuse `blobfuse2` | 0.489744 | 43.955 | Similar to the GPT-style 20 GiB H100 BlobFuse result |
| H200 flex | BlobFuse `/data` | fuse `blobfuse2` | 0.354608 | 60.572 | Much slower than H200 host `/mnt` |
| H100 flex | host `/mnt` | ext4 `/dev/sdb1` | 0.257685 | 83.338 | Slower than BlobFuse on this H100 host |
| A100 VMSS | Standard SSD 1 TiB disk | ext4 `/dev/sdc` | 0.239423 | 89.694 | Current `managed-csi` class is not a fast hot checkpoint target |
| A100 VMSS | Premium SSD 1 TiB disk | ext4 `/dev/sdd` | 0.199571 | 107.605 | Current `managed-csi-premium` class is not a fast hot checkpoint target |

The fastest practical target observed so far is node-local `/mnt` on H200. It is
not durable and not shared, so the safe pattern is checkpoint to local `/mnt`
first, then upload/copy to Blob out-of-band. On H100 flex, the same host `/mnt`
path was slower than BlobFuse in this capture, so it should not be assumed
universally fast across all flex GPU node classes.

Azure Disk could not be evaluated on the flex H100/H200 nodes with the current
cluster integration. The flex nodes report provider IDs such as
`azure-flex:///.../Microsoft.Compute/virtualMachines/flex-h100-*`, while the A100
AKS pool reports VMSS IDs such as
`azure:///.../virtualMachineScaleSets/aks-gpu-.../virtualMachines/0`. Azure Disk
CSI attached successfully on the A100 VMSS node, but attach failed on flex H100
with `could not get disk lun ... not a vmss instance`. The real Blob NFS v3 export
also failed to mount from the H100 flex path with `access denied by server`.

That means the current hot checkpoint choices are different by node class:

- H200 flex: use local host `/mnt` as the hot checkpoint target, then copy to
  Blob asynchronously for durability.
- H100 flex: no faster mounted hot path was found; BlobFuse beat host `/mnt`, and
  Azure Disk/Blob NFS need platform integration fixes before they can be used.
- A100 VMSS: local host `/mnt` beat BlobFuse, but the current 1 TiB Standard and
  Premium managed disk classes did not. A faster managed-disk option would need a
  larger/faster tier, Premium SSD v2, Ultra Disk, or striping rather than the
  current classes.

## Caveats

- The staged targets are measured after copying the same sampled files from
  BlobFuse. That makes target reads warmer than a completely cold dataset
  provisioned natively on those backends, but it preserves the exact file shape
  of the voice result directory.
- Azure Disk and local `emptyDir` are single-node options in these runs. They
  are strong staging/cache targets, not shared durable sources of truth.
- BlobFuse warm-cache results depend on cache state, pod placement, and client
  behavior. Treat first-pass numbers as the safer signal for new jobs or cold
  nodes.
- Async checkpoint blocked throughput is a caller-time metric, not physical
  storage throughput. Always compare it with `Async writer GB/s` and the chosen
  overlap window.
- The GPT-style GPU-SKU sample uses Hugging Face data and a deliberately tiny
  model with padding-inflated checkpoint files. It is useful for comparing
  checkpoint overlap mechanics across GPU SKUs, not for claiming absolute GPT-2
  training throughput.
- The `training-nfs` PVC in this cluster is also a Blob CSI / BlobFuse mount to
  the same backing container, so it should not be interpreted as a real NFS
  comparison.
