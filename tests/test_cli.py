import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def test_cli_smoke_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_json = Path(tmp) / "result.json"
            output_md = Path(tmp) / "result.md"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "azure_storage_benchmarks",
                    "run",
                    "--path",
                    f"tmp={tmp}",
                    "--small-files",
                    "2",
                    "--small-file-size-kib",
                    "1",
                    "--large-file-size-mib",
                    "1",
                    "--checkpoint-files",
                    "1",
                    "--checkpoint-size-mib",
                    "1",
                    "--async-checkpoint",
                    "--iterations",
                    "1",
                    "--run-id",
                    "cli",
                    "--output-json",
                    str(output_json),
                    "--output-md",
                    str(output_md),
                ],
                check=False,
                text=True,
                capture_output=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            report = json.loads(output_json.read_text(encoding="utf-8"))
            markdown = output_md.read_text(encoding="utf-8")

        self.assertEqual(report["run_id"], "cli")
        self.assertIn("Storage Benchmark Results", markdown)

    def test_cli_dataset_read_smoke_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dataset.bin").write_bytes(b"x" * 1024)
            output_json = root / "dataset-result.json"
            output_md = root / "dataset-result.md"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "azure_storage_benchmarks",
                    "dataset-read",
                    "--path",
                    f"tmp={tmp}",
                    "--max-bytes",
                    "512",
                    "--concurrency",
                    "2",
                    "--run-id",
                    "cli-dataset",
                    "--output-json",
                    str(output_json),
                    "--output-md",
                    str(output_md),
                ],
                check=False,
                text=True,
                capture_output=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            report = json.loads(output_json.read_text(encoding="utf-8"))
            markdown = output_md.read_text(encoding="utf-8")

        self.assertEqual(report["run_id"], "cli-dataset")
        self.assertEqual(report["results"][0]["metrics"]["bytes_read"], 512)
        self.assertIn("Dataset Read Benchmark Results", markdown)


if __name__ == "__main__":
    unittest.main()
