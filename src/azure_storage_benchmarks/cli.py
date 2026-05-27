from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .bench import (
    BenchmarkConfig,
    DatasetReadConfig,
    parse_named_path,
    render_dataset_read_markdown,
    render_markdown,
    run_benchmarks,
    run_dataset_read_benchmarks,
    write_dataset_read_markdown_report,
    write_json_report,
    write_markdown_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="azure-storage-benchmark",
        description="Benchmark storage mounts for AI workload access patterns.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run benchmarks against one or more paths")
    run_parser.add_argument(
        "--path",
        action="append",
        required=True,
        metavar="NAME=PATH",
        help="named mount/path to test, for example blob=/data",
    )
    run_parser.add_argument("--small-files", type=int, default=1000)
    run_parser.add_argument("--small-file-size-kib", type=int, default=4)
    run_parser.add_argument("--large-file-size-mib", type=int, default=256)
    run_parser.add_argument("--checkpoint-files", type=int, default=4)
    run_parser.add_argument("--checkpoint-size-mib", type=int, default=64)
    run_parser.add_argument(
        "--async-checkpoint",
        action="store_true",
        help="also run a checkpoint-like write in a background thread",
    )
    run_parser.add_argument(
        "--async-checkpoint-overlap-ms",
        type=int,
        default=0,
        help="milliseconds of simulated compute to overlap with async checkpoint writes",
    )
    run_parser.add_argument("--iterations", type=int, default=3)
    run_parser.add_argument("--run-id", default=None)
    run_parser.add_argument("--keep-data", action="store_true")
    run_parser.add_argument(
        "--output-json",
        type=Path,
        default=Path("storage-benchmark-results.json"),
        help="JSON report path",
    )
    run_parser.add_argument(
        "--output-md",
        type=Path,
        default=None,
        help="optional Markdown report path",
    )
    run_parser.set_defaults(func=run_command)

    dataset_parser = subparsers.add_parser(
        "dataset-read",
        help="read an existing dataset without modifying it",
    )
    dataset_parser.add_argument(
        "--path",
        action="append",
        required=True,
        metavar="NAME=PATH",
        help="named existing dataset path to read, for example lustre=/lustre/training-data",
    )
    dataset_parser.add_argument(
        "--max-bytes",
        type=int,
        default=None,
        help="maximum bytes to read per path; defaults to the full dataset",
    )
    dataset_parser.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="number of files to read in parallel",
    )
    dataset_parser.add_argument(
        "--block-size-mib",
        type=int,
        default=8,
        help="read chunk size per worker",
    )
    dataset_parser.add_argument("--run-id", default=None)
    dataset_parser.add_argument(
        "--output-json",
        type=Path,
        default=Path("dataset-read-benchmark-results.json"),
        help="JSON report path",
    )
    dataset_parser.add_argument(
        "--output-md",
        type=Path,
        default=None,
        help="optional Markdown report path",
    )
    dataset_parser.set_defaults(func=dataset_read_command)

    summarize_parser = subparsers.add_parser("summarize", help="render Markdown from a JSON report")
    summarize_parser.add_argument("input_json", type=Path)
    summarize_parser.add_argument("--output-md", type=Path, default=None)
    summarize_parser.set_defaults(func=summarize_command)

    return parser


def run_command(args: argparse.Namespace) -> int:
    try:
        paths = dict(parse_named_path(value) for value in args.path)
        config = BenchmarkConfig(
            small_files=args.small_files,
            small_file_size_kib=args.small_file_size_kib,
            large_file_size_mib=args.large_file_size_mib,
            checkpoint_files=args.checkpoint_files,
            checkpoint_size_mib=args.checkpoint_size_mib,
            async_checkpoint=args.async_checkpoint,
            async_checkpoint_overlap_ms=args.async_checkpoint_overlap_ms,
            iterations=args.iterations,
            run_id=args.run_id,
            keep_data=args.keep_data,
        )
        report = run_benchmarks(paths, config)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    write_json_report(report, args.output_json)
    if args.output_md:
        write_markdown_report(report, args.output_md)
    print(render_markdown(report))
    print(f"Wrote JSON report: {args.output_json}")
    if args.output_md:
        print(f"Wrote Markdown report: {args.output_md}")

    return 0 if all(result["ok"] for result in report["results"]) else 1


def dataset_read_command(args: argparse.Namespace) -> int:
    try:
        paths = dict(parse_named_path(value) for value in args.path)
        config = DatasetReadConfig(
            max_bytes=args.max_bytes,
            concurrency=args.concurrency,
            block_size_mib=args.block_size_mib,
            run_id=args.run_id,
        )
        report = run_dataset_read_benchmarks(paths, config)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    write_json_report(report, args.output_json)
    if args.output_md:
        write_dataset_read_markdown_report(report, args.output_md)
    print(render_dataset_read_markdown(report))
    print(f"Wrote JSON report: {args.output_json}")
    if args.output_md:
        print(f"Wrote Markdown report: {args.output_md}")

    return 0 if all(result["ok"] for result in report["results"]) else 1


def summarize_command(args: argparse.Namespace) -> int:
    report = json.loads(args.input_json.read_text(encoding="utf-8"))
    markdown = render_markdown(report)
    if args.output_md:
        args.output_md.parent.mkdir(parents=True, exist_ok=True)
        args.output_md.write_text(markdown, encoding="utf-8")
        print(f"Wrote Markdown report: {args.output_md}")
    else:
        print(markdown)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
