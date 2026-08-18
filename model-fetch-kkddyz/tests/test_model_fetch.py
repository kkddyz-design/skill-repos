from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "model_fetch.py"
SPEC = importlib.util.spec_from_file_location("model_fetch_under_test", SCRIPT)
assert SPEC and SPEC.loader
model_fetch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = model_fetch
SPEC.loader.exec_module(model_fetch)


class FakeStream(io.StringIO):
    def __init__(self, *, tty: bool):
        super().__init__()
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class ProgressRendererTests(unittest.TestCase):
    def test_tty_renders_single_dynamic_line_with_eta(self):
        stream = FakeStream(tty=True)
        clock = FakeClock()
        renderer = model_fetch.ProgressRenderer(stream=stream, clock=clock)
        renderer.start("weights.gguf", 100)
        clock.value = 1.0
        renderer.update("weights.gguf", 50)
        renderer.close(success=True)

        output = stream.getvalue()
        self.assertIn("50.0%", output)
        self.assertIn("ETA", output)
        self.assertTrue(output.endswith("\n"))
        self.assertNotIn("\nDownloading", output)

    def test_non_tty_uses_throttled_lines_without_carriage_returns(self):
        stream = FakeStream(tty=False)
        clock = FakeClock()
        renderer = model_fetch.ProgressRenderer(stream=stream, clock=clock)
        renderer.start("weights.gguf", 100)
        clock.value = 1.0
        renderer.update("weights.gguf", 1)
        self.assertEqual(stream.getvalue().count("Downloading"), 1)
        clock.value = 11.0
        renderer.update("weights.gguf", 49)
        renderer.close(success=True)

        output = stream.getvalue()
        self.assertGreaterEqual(output.count("Downloading"), 2)
        self.assertNotIn("\r", output)

    def test_no_progress_is_silent(self):
        stream = FakeStream(tty=True)
        renderer = model_fetch.ProgressRenderer(enabled=False, stream=stream)
        renderer.start("weights.gguf", 10)
        renderer.update("weights.gguf", 10)
        renderer.close(success=True)
        self.assertEqual(stream.getvalue(), "")

    def test_format_helpers(self):
        self.assertEqual(model_fetch._format_bytes(1024), "1.0 KiB")
        self.assertEqual(model_fetch._format_duration(65), "01:05")


class AdapterTests(unittest.TestCase):
    def test_modelscope_callback_reports_existing_bytes(self):
        stream = FakeStream(tty=True)
        renderer = model_fetch.ProgressRenderer(stream=stream)
        renderer.refresh_interval = 0

        class BaseCallback:
            def __init__(self, filename, file_size):
                self.filename = filename
                self.file_size = file_size

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "weights.gguf"
            path.write_bytes(b"1234")
            callback_type = model_fetch._build_modelscope_callback(BaseCallback, renderer)
            callback = callback_type(str(path), 10)
            callback.update(2)
            callback.end()

        output = stream.getvalue()
        self.assertIn("60.0%", output)

    def test_huggingface_adapter_forwards_progress_to_renderer(self):
        stream = FakeStream(tty=True)
        renderer = model_fetch.ProgressRenderer(stream=stream)
        renderer.refresh_interval = 0

        class BaseTqdm:
            def __init__(self, *args, **kwargs):
                self.n = int(kwargs.get("initial") or 0)

            def close(self):
                return None

        progress_type = model_fetch._build_huggingface_tqdm(BaseTqdm, renderer)
        progress = progress_type(total=10, initial=2, desc="weights.gguf")
        progress.update(3)
        progress.close()

        self.assertIn("50.0%", stream.getvalue())

    def test_modelscope_sdk_receives_expected_arguments(self):
        calls = []

        class BaseCallback:
            def __init__(self, filename, file_size):
                pass

        class FakeHubApi:
            def download_repo(self, *args, **kwargs):
                calls.append((args, kwargs))

        fake_module = types.ModuleType("modelscope_hub")
        fake_module.HubApi = FakeHubApi
        fake_module.ProgressCallback = BaseCallback
        with mock.patch.dict(sys.modules, {"modelscope_hub": fake_module}):
            renderer = model_fetch.ProgressRenderer(enabled=False, stream=FakeStream(tty=True))
            with tempfile.TemporaryDirectory() as temp_dir:
                model_fetch._download_modelscope(
                    Path(temp_dir) / "runtime",
                    "owner/repo",
                    ["weights.gguf"],
                    Path(temp_dir) / "stage",
                    renderer,
                )

        self.assertEqual(calls[0][0], ("owner/repo",))
        self.assertEqual(calls[0][1]["repo_type"], "model")
        self.assertEqual(calls[0][1]["allow_patterns"], ["weights.gguf"])
        self.assertIsNone(calls[0][1]["progress_callbacks"])

    def test_modelscope_import_happens_with_vendor_progress_disabled(self):
        observed = []

        class BaseCallback:
            def __init__(self, filename, file_size):
                pass

        class FakeHubApi:
            def __init__(self):
                observed.append(os.environ.get("TQDM_DISABLE"))

            def download_repo(self, *args, **kwargs):
                return None

        fake_module = types.ModuleType("modelscope_hub")
        fake_module.HubApi = FakeHubApi
        fake_module.ProgressCallback = BaseCallback
        with mock.patch.dict(sys.modules, {"modelscope_hub": fake_module}):
            renderer = model_fetch.ProgressRenderer(enabled=False, stream=FakeStream(tty=True))
            with tempfile.TemporaryDirectory() as temp_dir:
                model_fetch._download_modelscope(
                    Path(temp_dir) / "runtime",
                    "owner/repo",
                    [],
                    Path(temp_dir) / "stage",
                    renderer,
                )

        self.assertEqual(observed, ["1"])

    def test_huggingface_sdk_receives_endpoint_and_patterns(self):
        calls = []

        def fake_snapshot_download(**kwargs):
            calls.append(kwargs)

        fake_hf = types.ModuleType("huggingface_hub")
        fake_hf.snapshot_download = fake_snapshot_download
        with mock.patch.dict(sys.modules, {"huggingface_hub": fake_hf}):
            renderer = model_fetch.ProgressRenderer(enabled=False, stream=FakeStream(tty=True))
            with tempfile.TemporaryDirectory() as temp_dir:
                model_fetch._download_huggingface(
                    "hf-mirror",
                    "owner/repo",
                    ["weights.gguf"],
                    Path(temp_dir) / "stage",
                    renderer,
                )

        self.assertEqual(calls[0]["repo_id"], "owner/repo")
        self.assertEqual(calls[0]["allow_patterns"], ["weights.gguf"])
        self.assertEqual(calls[0]["endpoint"], "https://hf-mirror.com")
        self.assertNotIn("tqdm_class", calls[0])


class FetchFlowTests(unittest.TestCase):
    def test_source_failure_falls_back_and_preserves_staging_until_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            calls = []

            def fake_run_download(source, runtime, repo, files, stage, *, progress=None):
                calls.append((source, progress))
                stage.mkdir(parents=True, exist_ok=True)
                (stage / "weights.gguf").write_bytes(b"model")
                if source == "modelscope":
                    return False, "modelscope failed"
                return True, ""

            args = model_fetch.build_parser().parse_args(
                ["fetch", "--repo", "owner/repo", "--output", str(root), "--no-progress"]
            )
            with mock.patch.object(model_fetch, "user_runtime", return_value=root / "runtime"), mock.patch.object(
                model_fetch, "run_download", side_effect=fake_run_download
            ):
                result = model_fetch.fetch(args)

            self.assertEqual(result, 0)
            self.assertEqual(calls, [("modelscope", False), ("hf-mirror", False)])
            self.assertTrue((root / "owner" / "repo" / "weights.gguf").is_file())

    def test_parser_exposes_no_progress(self):
        args = model_fetch.build_parser().parse_args(
            ["fetch", "--repo", "owner/repo", "--no-progress"]
        )
        self.assertTrue(args.no_progress)
        self.assertEqual(args.target, "lmstudio")


if __name__ == "__main__":
    unittest.main()
