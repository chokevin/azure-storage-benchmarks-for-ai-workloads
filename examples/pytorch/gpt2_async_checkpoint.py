#!/usr/bin/env python3
"""Tiny GPT-style training loop with sync and async checkpoint timing.

This example intentionally keeps dependencies out of the benchmark package. Run
it in an environment with PyTorch and Hugging Face `datasets` installed.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import statistics
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any


GB = 1000 * 1000 * 1000
MIB = 1024 * 1024


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train a tiny GPT-style model and time async checkpoint writes."
    )
    parser.add_argument("--dataset", default="roneneldan/TinyStories")
    parser.add_argument("--dataset-config", default=None)
    parser.add_argument("--split", default="train")
    parser.add_argument("--text-field", default="text")
    parser.add_argument("--max-records", type=int, default=512)
    parser.add_argument("--streaming", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--seq-len", type=int, default=128)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--steps", type=int, default=40)
    parser.add_argument("--checkpoint-every", type=int, default=10)
    parser.add_argument("--checkpoint-padding-mib", type=int, default=1024)
    parser.add_argument("--checkpoint-dir", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--layers", type=int, default=4)
    parser.add_argument("--heads", type=int, default=4)
    parser.add_argument("--embedding-dim", type=int, default=256)
    parser.add_argument("--learning-rate", type=float, default=3e-4)
    parser.add_argument("--keep-checkpoints", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    torch = _import_torch()
    load_dataset = _import_load_dataset()

    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
    args.checkpoint_dir.mkdir(parents=True, exist_ok=True)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    if args.output_md:
        args.output_md.parent.mkdir(parents=True, exist_ok=True)

    device = _select_device(torch, args.device)
    dataset_seconds, token_ids = _time_call(
        lambda: _load_byte_tokens(
            load_dataset=load_dataset,
            torch=torch,
            dataset=args.dataset,
            dataset_config=args.dataset_config,
            split=args.split,
            text_field=args.text_field,
            max_records=args.max_records,
            streaming=args.streaming,
        )
    )
    if len(token_ids) <= args.seq_len + 1:
        raise ValueError(
            f"dataset produced only {len(token_ids)} byte tokens; need > seq_len + 1"
        )

    model = TinyGpt(
        torch=torch,
        vocab_size=256,
        seq_len=args.seq_len,
        embedding_dim=args.embedding_dim,
        heads=args.heads,
        layers=args.layers,
    ).to(device)
    model_summary = _model_summary(model)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate)
    padding = torch.ones(args.checkpoint_padding_mib * MIB, dtype=torch.uint8)
    writer = AsyncCheckpointWriter(
        torch=torch,
        checkpoint_dir=args.checkpoint_dir,
        run_id=run_id,
    )

    losses: list[float] = []
    step_seconds: list[float] = []
    train_start = time.perf_counter()
    for step in range(1, args.steps + 1):
        step_seconds.append(
            _train_step(
                torch=torch,
                model=model,
                optimizer=optimizer,
                token_ids=token_ids,
                batch_size=args.batch_size,
                seq_len=args.seq_len,
                device=device,
                losses=losses,
            )
        )
        if step % args.checkpoint_every == 0:
            writer.submit(
                step=step,
                payload_factory=lambda step=step: _checkpoint_payload(
                    model=model,
                    optimizer=optimizer,
                    padding=padding,
                    step=step,
                    run_id=run_id,
                    losses=losses,
                ),
            )

    train_seconds = time.perf_counter() - train_start
    async_summary = writer.close()
    sync_result = _write_checkpoint(
        torch=torch,
        payload=_checkpoint_payload(
            model=model,
            optimizer=optimizer,
            padding=padding,
            step=args.steps,
            run_id=run_id,
            losses=losses,
        ),
        path=args.checkpoint_dir / f"{run_id}-sync-final.pt",
    )
    if not args.keep_checkpoints:
        for path in args.checkpoint_dir.glob(f"{run_id}-*.pt"):
            path.unlink(missing_ok=True)

    total_tokens = args.steps * args.batch_size * args.seq_len
    total_step_seconds = sum(step_seconds)
    loop_tokens_per_second = total_tokens / train_seconds
    step_tokens_per_second = _per_second(total_tokens, total_step_seconds)

    report = {
        "run_id": run_id,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "device": _device_info(torch, device),
        "model": model_summary,
        "dataset": {
            "name": args.dataset,
            "config": args.dataset_config,
            "split": args.split,
            "streaming": args.streaming,
            "max_records": args.max_records,
            "load_seconds": dataset_seconds,
            "byte_tokens": int(len(token_ids)),
        },
        "config": {
            "seq_len": args.seq_len,
            "batch_size": args.batch_size,
            "steps": args.steps,
            "checkpoint_every": args.checkpoint_every,
            "checkpoint_padding_mib": args.checkpoint_padding_mib,
            "layers": args.layers,
            "heads": args.heads,
            "embedding_dim": args.embedding_dim,
        },
        "training": {
            "train_seconds": train_seconds,
            "step_seconds_total": total_step_seconds,
            "mean_step_seconds": statistics.mean(step_seconds),
            "median_step_seconds": statistics.median(step_seconds),
            "tokens": total_tokens,
            "tokens_per_second": loop_tokens_per_second,
            "loop_tokens_per_second": loop_tokens_per_second,
            "step_tokens_per_second": step_tokens_per_second,
            "final_loss": losses[-1] if losses else None,
        },
        "checkpoint": {
            "directory": str(args.checkpoint_dir),
            "mount": _mount_info_for(args.checkpoint_dir),
            "async": async_summary,
            "sync_final": {
                "seconds": sync_result["seconds"],
                "bytes": sync_result["bytes"],
                "gb_s": _gb_per_second(sync_result["bytes"], sync_result["seconds"]),
            },
        },
    }
    args.output_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    markdown = render_markdown(report)
    if args.output_md:
        args.output_md.write_text(markdown)
    print(markdown)
    print(f"Wrote JSON: {args.output_json}")
    if args.output_md:
        print(f"Wrote Markdown: {args.output_md}")
    return 0


class TinyGpt:
    def __new__(
        cls,
        torch: Any,
        vocab_size: int,
        seq_len: int,
        embedding_dim: int,
        heads: int,
        layers: int,
    ):
        nn = torch.nn

        class _TinyGpt(nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.seq_len = seq_len
                self.token_embedding = nn.Embedding(vocab_size, embedding_dim)
                self.position_embedding = nn.Embedding(seq_len, embedding_dim)
                encoder_layer = nn.TransformerEncoderLayer(
                    d_model=embedding_dim,
                    nhead=heads,
                    dim_feedforward=embedding_dim * 4,
                    dropout=0.0,
                    activation="gelu",
                    batch_first=True,
                    norm_first=True,
                )
                self.blocks = nn.TransformerEncoder(encoder_layer, num_layers=layers)
                self.norm = nn.LayerNorm(embedding_dim)
                self.head = nn.Linear(embedding_dim, vocab_size, bias=False)

            def forward(self, tokens: Any) -> Any:
                batch, length = tokens.shape
                positions = torch.arange(length, device=tokens.device).unsqueeze(0)
                x = self.token_embedding(tokens) + self.position_embedding(positions)
                mask = torch.triu(
                    torch.ones(length, length, device=tokens.device, dtype=torch.bool),
                    diagonal=1,
                )
                x = self.blocks(x, mask=mask)
                return self.head(self.norm(x))

        return _TinyGpt()


class AsyncCheckpointWriter:
    def __init__(self, torch: Any, checkpoint_dir: Path, run_id: str) -> None:
        self.torch = torch
        self.checkpoint_dir = checkpoint_dir
        self.run_id = run_id
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.pending: Future | None = None
        self.records: list[dict] = []

    def submit(self, step: int, payload_factory: Any) -> None:
        prior_wait_seconds = self._wait_for_pending(reason="before_next_submit")
        snapshot_seconds, payload = _time_call(payload_factory)
        path = self.checkpoint_dir / f"{self.run_id}-step-{step:06d}.pt"
        submit_start = time.perf_counter()
        self.pending = self.executor.submit(
            _write_checkpoint,
            torch=self.torch,
            payload=payload,
            path=path,
        )
        submit_seconds = time.perf_counter() - submit_start
        self.records.append(
            {
                "step": step,
                "path": str(path),
                "prior_wait_seconds": prior_wait_seconds,
                "snapshot_seconds": snapshot_seconds,
                "submit_seconds": submit_seconds,
                "caller_blocked_at_submit_seconds": (
                    prior_wait_seconds + snapshot_seconds + submit_seconds
                ),
            }
        )

    def close(self) -> dict:
        final_wait_seconds = self._wait_for_pending(reason="final_wait")
        self.executor.shutdown(wait=True)
        total_writer_seconds = sum(
            record.get("writer_seconds", 0.0) for record in self.records
        )
        total_bytes = sum(record.get("bytes", 0) for record in self.records)
        total_blocked_seconds = (
            sum(record["caller_blocked_at_submit_seconds"] for record in self.records)
            + final_wait_seconds
        )
        return {
            "checkpoints": self.records,
            "final_wait_seconds": final_wait_seconds,
            "total_writer_seconds": total_writer_seconds,
            "total_bytes": total_bytes,
            "total_blocked_seconds": total_blocked_seconds,
            "writer_gb_s": _gb_per_second(total_bytes, total_writer_seconds),
            "blocked_gb_s": _gb_per_second(total_bytes, total_blocked_seconds),
        }

    def _wait_for_pending(self, reason: str) -> float:
        if self.pending is None:
            return 0.0
        wait_seconds, result = _time_call(self.pending.result)
        if self.records:
            self.records[-1].update(
                {
                    "writer_seconds": result["seconds"],
                    "bytes": result["bytes"],
                    "wait_reason": reason,
                    "wait_observed_seconds": wait_seconds,
                    "writer_gb_s": _gb_per_second(result["bytes"], result["seconds"]),
                }
            )
        self.pending = None
        return wait_seconds


def _train_step(
    torch: Any,
    model: Any,
    optimizer: Any,
    token_ids: Any,
    batch_size: int,
    seq_len: int,
    device: Any,
    losses: list[float],
) -> float:
    start = time.perf_counter()
    high = int(token_ids.numel() - seq_len - 1)
    offsets = torch.randint(0, high, (batch_size,))
    x = torch.stack([token_ids[offset : offset + seq_len] for offset in offsets]).to(device)
    y = torch.stack(
        [token_ids[offset + 1 : offset + seq_len + 1] for offset in offsets]
    ).to(device)
    logits = model(x)
    loss = torch.nn.functional.cross_entropy(
        logits.reshape(-1, logits.size(-1)),
        y.reshape(-1),
    )
    optimizer.zero_grad(set_to_none=True)
    loss.backward()
    optimizer.step()
    if device.type == "cuda":
        torch.cuda.synchronize(device)
    losses.append(float(loss.detach().cpu()))
    return time.perf_counter() - start


def _checkpoint_payload(
    model: Any,
    optimizer: Any,
    padding: Any,
    step: int,
    run_id: str,
    losses: list[float],
) -> dict:
    return {
        "run_id": run_id,
        "step": step,
        "losses": list(losses),
        "model": _state_dict_to_cpu(model.state_dict()),
        "optimizer": _state_dict_to_cpu(optimizer.state_dict()),
        "padding": padding,
    }


def _state_dict_to_cpu(value: Any) -> Any:
    if hasattr(value, "detach"):
        return value.detach().cpu()
    if isinstance(value, dict):
        return {key: _state_dict_to_cpu(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_state_dict_to_cpu(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_state_dict_to_cpu(item) for item in value)
    return value


def _write_checkpoint(torch: Any, payload: dict, path: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    start = time.perf_counter()
    with tmp_path.open("wb") as handle:
        torch.save(payload, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    seconds = time.perf_counter() - start
    return {"seconds": seconds, "bytes": path.stat().st_size}


def _model_summary(model: Any) -> dict:
    parameters = 0
    parameter_bytes = 0
    for parameter in model.parameters():
        parameters += int(parameter.numel())
        parameter_bytes += int(parameter.numel() * parameter.element_size())
    return {
        "parameters": parameters,
        "parameter_bytes": parameter_bytes,
    }


def _load_byte_tokens(
    load_dataset: Any,
    torch: Any,
    dataset: str,
    dataset_config: str | None,
    split: str,
    text_field: str,
    max_records: int,
    streaming: bool,
) -> Any:
    kwargs = {"split": split, "streaming": streaming}
    if dataset_config:
        loaded = load_dataset(dataset, dataset_config, **kwargs)
    else:
        loaded = load_dataset(dataset, **kwargs)
    chunks: list[str] = []
    for index, row in enumerate(loaded):
        if index >= max_records:
            break
        text = row.get(text_field)
        if text is None:
            text = next((value for value in row.values() if isinstance(value, str)), "")
        if text:
            chunks.append(text)
    data = "\n\n".join(chunks).encode("utf-8")
    return torch.tensor(list(data), dtype=torch.long)


def _select_device(torch: Any, requested: str) -> Any:
    if requested == "cuda" and not torch.cuda.is_available():
        return torch.device("cpu")
    return torch.device(requested)


def _device_info(torch: Any, device: Any) -> dict:
    info = {"requested": str(device), "type": device.type}
    if device.type == "cuda" and torch.cuda.is_available():
        index = device.index if device.index is not None else torch.cuda.current_device()
        props = torch.cuda.get_device_properties(index)
        info.update(
            {
                "name": props.name,
                "total_memory_bytes": props.total_memory,
                "capability": list(props.major_minor)
                if hasattr(props, "major_minor")
                else [props.major, props.minor],
            }
        )
    return info


def _mount_info_for(path: Path) -> dict:
    mountinfo = Path("/proc/self/mountinfo")
    if not mountinfo.exists():
        return {}
    resolved_path = path.resolve()
    best_match = {}
    best_length = -1
    for line in mountinfo.read_text(encoding="utf-8").splitlines():
        if " - " not in line:
            continue
        left, right = line.split(" - ", 1)
        left_fields = left.split()
        right_fields = right.split()
        if len(left_fields) < 5 or len(right_fields) < 2:
            continue
        mount_point = Path(left_fields[4])
        try:
            is_match = resolved_path == mount_point or mount_point in resolved_path.parents
        except RuntimeError:
            is_match = False
        if is_match and len(str(mount_point)) > best_length:
            best_match = {
                "mount_point": str(mount_point),
                "fs_type": right_fields[0],
                "source": right_fields[1],
            }
            best_length = len(str(mount_point))
    return best_match


def render_markdown(report: dict) -> str:
    async_summary = report["checkpoint"]["async"]
    sync_final = report["checkpoint"]["sync_final"]
    training = report["training"]
    config = report["config"]
    model = report.get("model", {})
    loop_tokens_per_second = training.get(
        "loop_tokens_per_second",
        training.get("tokens_per_second", 0.0),
    )
    lines = [
        "# GPT-style async checkpoint result",
        "",
        f"- Run ID: `{report['run_id']}`",
        f"- Started: `{report['started_at']}`",
        f"- Host: `{report['host']['hostname']}`",
        f"- Device: `{report['device'].get('name', report['device']['type'])}`",
        f"- Dataset: `{report['dataset']['name']}` / `{report['dataset']['split']}`",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Training loop seconds | {training['train_seconds']:.3f} |",
        f"| Sum of pure step seconds | {training.get('step_seconds_total', 0.0):.3f} |",
        f"| Median step seconds | {training['median_step_seconds']:.6f} |",
        f"| Loop tokens/s, including checkpoint waits | {loop_tokens_per_second:.1f} |",
        f"| Pure step tokens/s, excluding checkpoint waits | {training.get('step_tokens_per_second', 0.0):.1f} |",
        f"| Final loss | {training['final_loss']:.6f} |",
        f"| Model parameters | {model.get('parameters', 0)} |",
        f"| Model parameter bytes | {model.get('parameter_bytes', 0)} |",
        f"| Checkpoint padding MiB | {config['checkpoint_padding_mib']} |",
        f"| Async checkpoint bytes | {async_summary['total_bytes']} |",
        f"| Async writer GB/s | {async_summary['writer_gb_s']:.6f} |",
        f"| Async caller-blocked seconds | {async_summary['total_blocked_seconds']:.6f} |",
        f"| Async caller-blocked GB/s | {async_summary['blocked_gb_s']:.6f} |",
        f"| Sync final checkpoint seconds | {sync_final['seconds']:.6f} |",
        f"| Sync final checkpoint GB/s | {sync_final['gb_s']:.6f} |",
        "",
        "## Async checkpoints",
        "",
        "| Step | Bytes | Writer s | Writer GB/s | Wait observed s | Snapshot s | Submit s |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for checkpoint in async_summary["checkpoints"]:
        lines.append(
            "| {step} | {bytes} | {writer:.6f} | {gb_s:.6f} | {wait:.6f} | {snapshot:.6f} | {submit:.6f} |".format(
                step=checkpoint["step"],
                bytes=checkpoint.get("bytes", 0),
                writer=checkpoint.get("writer_seconds", 0.0),
                gb_s=checkpoint.get("writer_gb_s", 0.0),
                wait=checkpoint.get("wait_observed_seconds", 0.0),
                snapshot=checkpoint["snapshot_seconds"],
                submit=checkpoint["submit_seconds"],
            )
        )
    lines.append("")
    return "\n".join(lines)


def _time_call(fn: Any) -> tuple[float, Any]:
    start = time.perf_counter()
    value = fn()
    return time.perf_counter() - start, value


def _gb_per_second(bytes_count: int, seconds: float) -> float:
    if seconds <= 0:
        return 0.0
    return (bytes_count / GB) / seconds


def _per_second(count: int, seconds: float) -> float:
    if seconds <= 0:
        return 0.0
    return count / seconds


def _import_torch() -> Any:
    try:
        import torch
    except ImportError as exc:
        raise SystemExit("Install PyTorch before running this example.") from exc
    return torch


def _import_load_dataset() -> Any:
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise SystemExit("Install Hugging Face datasets before running this example.") from exc
    return load_dataset


if __name__ == "__main__":
    raise SystemExit(main())
