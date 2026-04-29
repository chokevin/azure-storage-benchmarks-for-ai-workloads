import importlib.util
import tempfile
import unittest
from pathlib import Path


EXAMPLE_PATH = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "pytorch"
    / "gpt2_async_checkpoint.py"
)


def load_example_module():
    spec = importlib.util.spec_from_file_location("gpt2_async_checkpoint", EXAMPLE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeTorch:
    @staticmethod
    def save(payload, handle):
        handle.write(payload["data"])


class Gpt2ExampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.example = load_example_module()

    def test_write_checkpoint_times_full_persistent_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "checkpoint.pt"

            result = self.example._write_checkpoint(
                torch=FakeTorch,
                payload={"data": b"x" * 4096},
                path=output,
            )

            self.assertEqual(output.stat().st_size, 4096)
            self.assertEqual(result["bytes"], 4096)
            self.assertGreater(result["seconds"], 0)
            self.assertFalse(output.with_suffix(".pt.tmp").exists())

    def test_async_writer_uses_run_id_in_checkpoint_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = self.example.AsyncCheckpointWriter(
                torch=FakeTorch,
                checkpoint_dir=Path(tmp),
                run_id="unit-run",
            )

            writer.submit(step=10, payload_factory=lambda: {"data": b"checkpoint"})
            summary = writer.close()

            checkpoint = Path(tmp) / "unit-run-step-000010.pt"
            self.assertTrue(checkpoint.exists())
            self.assertEqual(summary["total_bytes"], len(b"checkpoint"))
            self.assertEqual(summary["checkpoints"][0]["path"], str(checkpoint))

    def test_markdown_distinguishes_loop_and_step_throughput(self):
        report = {
            "run_id": "unit",
            "started_at": "2026-01-01T00:00:00Z",
            "host": {"hostname": "test-host"},
            "device": {"type": "cuda", "name": "Fake GPU"},
            "dataset": {"name": "dataset", "split": "train"},
            "model": {"parameters": 10, "parameter_bytes": 40},
            "config": {"checkpoint_padding_mib": 2048},
            "training": {
                "train_seconds": 4.0,
                "step_seconds_total": 1.0,
                "median_step_seconds": 0.1,
                "loop_tokens_per_second": 100.0,
                "step_tokens_per_second": 400.0,
                "final_loss": 1.23,
            },
            "checkpoint": {
                "async": {
                    "total_bytes": 2048,
                    "writer_gb_s": 1.0,
                    "total_blocked_seconds": 2.0,
                    "blocked_gb_s": 0.5,
                    "checkpoints": [
                        {
                            "step": 10,
                            "bytes": 2048,
                            "writer_seconds": 1.0,
                            "writer_gb_s": 1.0,
                            "wait_observed_seconds": 0.5,
                            "snapshot_seconds": 0.1,
                            "submit_seconds": 0.01,
                        }
                    ],
                },
                "sync_final": {"seconds": 1.0, "gb_s": 1.0},
            },
        }

        markdown = self.example.render_markdown(report)

        self.assertIn("Loop tokens/s, including checkpoint waits", markdown)
        self.assertIn("Pure step tokens/s, excluding checkpoint waits", markdown)
        self.assertIn("Checkpoint padding MiB", markdown)


if __name__ == "__main__":
    unittest.main()
