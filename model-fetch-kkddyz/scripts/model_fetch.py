#!/usr/bin/env python3
"""Fetch and verify explicitly specified public model files on Windows."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import importlib.util
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import venv
from pathlib import Path
from typing import Callable, Iterable, TextIO

DEFAULT_ROOTS = {"folder": Path(r"E:\AIModels"), "lmstudio": Path(r"E:\lmstudio-models\models")}
SOURCE_ORDER = ("modelscope", "hf-mirror", "huggingface")
WEIGHT_SUFFIXES = (".safetensors", ".bin", ".gguf", ".onnx", ".pt", ".pth")
LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"


class ModelFetchError(RuntimeError):
    pass


@dataclasses.dataclass
class _ProgressTask:
    name: str
    total: int | None
    current: int
    started_at: float
    finished: bool = False


def _format_bytes(value: float | int | None) -> str:
    if value is None:
        return "? B"
    amount = float(max(0, value))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} B"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"


def _format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0 or not math.isfinite(seconds):
        return "--:--"
    whole = int(seconds)
    hours, remainder = divmod(whole, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


class ProgressRenderer:
    """Render one aggregate progress line for concurrent model downloads."""

    refresh_interval = 0.2
    log_interval = 10.0
    percent_step = 5.0
    bar_width = 28

    def __init__(
        self,
        enabled: bool | None = None,
        *,
        stream: TextIO | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.stream = stream or sys.stderr
        self.enabled = True if enabled is None else enabled
        self.dynamic = self.enabled and bool(self.stream.isatty())
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._tasks: dict[str, _ProgressTask] = {}
        self._last_render_at = 0.0
        self._last_logged_at = 0.0
        self._last_logged_percent = -self.percent_step
        self._last_bytes = 0
        self._last_line_length = 0
        self._closed = False

    def start(self, name: str, total: int | None, initial: int = 0) -> str:
        key = str(name)
        if not self.enabled:
            return key
        with self._lock:
            current = max(0, int(initial))
            self._tasks[key] = _ProgressTask(key, total, current, self._clock())
            self._render(force=True)
        return key

    def update(self, key: str, amount: int) -> None:
        if not self.enabled:
            return
        with self._lock:
            task = self._tasks.get(key)
            if task is None or task.finished:
                return
            task.current = max(0, task.current + int(amount))
            if task.total is not None:
                task.current = min(task.current, task.total)
            self._render()

    def finish(self, key: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            task = self._tasks.get(key)
            if task is None:
                return
            if task.total is not None:
                task.current = task.total
            task.finished = True
            self._render(force=True)

    def close(self, *, success: bool) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._closed:
                return
            if success:
                self._render(force=True)
                if self.dynamic:
                    self.stream.write("\n")
            elif self.dynamic and self._last_line_length:
                self.stream.write("\r" + (" " * self._last_line_length) + "\r")
            self.stream.flush()
            self._closed = True

    def _snapshot(self) -> tuple[int, int | None, int]:
        current = sum(task.current for task in self._tasks.values())
        totals = [task.total for task in self._tasks.values() if task.total is not None]
        total = sum(totals) if len(totals) == len(self._tasks) and totals else None
        active = sum(1 for task in self._tasks.values() if not task.finished)
        return current, total, active

    def _render(self, *, force: bool = False) -> None:
        now = self._clock()
        current, total, active = self._snapshot()
        percent = (100.0 * current / total) if total else None
        if not force:
            if self.dynamic and now - self._last_render_at < self.refresh_interval:
                return
            if not self.dynamic:
                percent_changed = percent is not None and percent - self._last_logged_percent >= self.percent_step
                if now - self._last_logged_at < self.log_interval and not percent_changed:
                    return
        elapsed = max(0.001, now - min((task.started_at for task in self._tasks.values()), default=now))
        speed = current / elapsed if current else 0.0
        eta = ((total - current) / speed) if total is not None and speed > 0 else None
        if total:
            filled = min(self.bar_width, int(self.bar_width * current / total))
            bar = "[" + ("#" * filled) + ("-" * (self.bar_width - filled)) + "]"
            progress = f"{percent:5.1f}% {bar}"
            size = f"{_format_bytes(current)}/{_format_bytes(total)}"
        else:
            progress = "[----------------------------]   ?%"
            size = f"{_format_bytes(current)}/?"
        suffix = f" {active} active" if active > 1 else ""
        line = f"Downloading {progress} {size} {_format_bytes(speed)}/s ETA {_format_duration(eta)}{suffix}"
        if self.dynamic:
            padding = max(0, self._last_line_length - len(line))
            self.stream.write("\r" + line + (" " * padding))
            self._last_line_length = len(line)
        else:
            self.stream.write(line + "\n")
            self._last_logged_at = now
            if percent is not None:
                self._last_logged_percent = percent
        self.stream.flush()
        self._last_render_at = now
        self._last_bytes = current


def _initial_file_size(filename: str | os.PathLike[str]) -> int:
    try:
        path = Path(filename)
        return path.stat().st_size if path.is_file() else 0
    except (OSError, ValueError):
        return 0


def _build_modelscope_callback(base_callback: type, renderer: ProgressRenderer) -> type:
    class ModelScopeProgressCallback(base_callback):
        def __init__(self, filename: str, file_size: int):
            super().__init__(filename, file_size)
            self._key = renderer.start(filename, file_size, _initial_file_size(filename))

        def update(self, size: int) -> None:
            renderer.update(self._key, size)

        def end(self) -> None:
            renderer.finish(self._key)

    return ModelScopeProgressCallback


def _build_huggingface_tqdm(base_tqdm: type, renderer: ProgressRenderer) -> type:
    class HuggingFaceProgressTqdm(base_tqdm):
        def __init__(self, *args, **kwargs):
            self._progress_name = str(kwargs.get("desc") or "download")
            self._progress_total = kwargs.get("total")
            self._progress_initial = int(kwargs.get("initial") or 0)
            kwargs["disable"] = True
            super().__init__(*args, **kwargs)
            self._progress_key = renderer.start(self._progress_name, self._progress_total, self._progress_initial)
            self._progress_closed = False

        def update(self, n: int = 1):
            renderer.update(self._progress_key, n)
            self.n = max(0, self.n + n)

        def close(self):
            if not self._progress_closed:
                renderer.finish(self._progress_key)
                self._progress_closed = True
            return super().close()

    return HuggingFaceProgressTqdm


@contextlib.contextmanager
def _temporary_environment(values: dict[str, str | None]):
    previous = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def user_runtime() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise ModelFetchError("LOCALAPPDATA is unavailable; cannot create an isolated runtime.")
    return Path(local_app_data) / "model-fetch-kkddyz" / "runtime"


def runtime_python(runtime: Path) -> Path:
    return runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def ensure_runtime_and_reexec() -> None:
    if "--_runtime" in sys.argv:
        return
    runtime = user_runtime()
    python_exe = runtime_python(runtime)
    if not python_exe.exists():
        print(f"Preparing isolated download runtime: {runtime}")
        venv.EnvBuilder(with_pip=True).create(runtime)
        subprocess.run([str(python_exe), "-m", "pip", "install", "--disable-pip-version-check", "modelscope-hub", "huggingface_hub"], check=True)
    os.execv(str(python_exe), [str(python_exe), str(Path(__file__).resolve()), "--_runtime", *sys.argv[1:]])


def ensure_runtime_packages() -> None:
    missing = [name for name in ("modelscope_hub", "huggingface_hub") if importlib.util.find_spec(name) is None]
    if missing:
        subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "modelscope-hub", "huggingface_hub"], check=True)


def parse_repo(value: str) -> tuple[str, str]:
    parts = value.split("/")
    if len(parts) != 2 or not all(parts) or any(part in {".", ".."} for part in parts):
        raise argparse.ArgumentTypeError("--repo must be exactly publisher/repository.")
    if any("\\" in part or ":" in part for part in parts):
        raise argparse.ArgumentTypeError("--repo contains invalid path characters.")
    return parts[0], parts[1]


def parse_files(values: Iterable[str] | None) -> list[str]:
    files: list[str] = []
    for value in values or []:
        files.extend(item.strip() for item in value.split(",") if item.strip())
    for filename in files:
        candidate = Path(filename)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ModelFetchError(f"Unsafe file path: {filename}")
    return files


def target_path(repo: str, target: str, output: str | None) -> tuple[Path, Path]:
    publisher, repository = parse_repo(repo)
    root = (Path(output).expanduser() if output else DEFAULT_ROOTS[target]).resolve()
    destination = (root / publisher / repository).resolve()
    if root != destination and root not in destination.parents:
        raise ModelFetchError("Resolved destination is outside the selected root.")
    return root, destination


def weight_files(path: Path) -> list[Path]:
    return [file for file in path.rglob("*") if file.is_file() and file.suffix.lower() in WEIGHT_SUFFIXES]


def validate_payload(path: Path, required_files: list[str]) -> list[str]:
    problems: list[str] = []
    if not path.is_dir():
        return [f"Directory does not exist: {path}"]
    files = [file for file in path.rglob("*") if file.is_file()]
    if not files:
        problems.append("Directory contains no files.")
    for required in required_files:
        if not (path / required).is_file():
            problems.append(f"Missing requested file: {required}")
    for file in files:
        if file.name.lower().endswith((".incomplete", ".lock")):
            problems.append(f"Incomplete download artifact: {file.relative_to(path)}")
        if file.stat().st_size == 0:
            problems.append(f"Zero-byte file: {file.relative_to(path)}")
    weights = weight_files(path)
    if not required_files and not weights:
        problems.append("No recognized model weight file was found.")
    for file in weights:
        with file.open("rb") as handle:
            if LFS_POINTER in handle.read(512):
                problems.append(f"Git LFS pointer instead of weights: {file.relative_to(path)}")
    return problems


def _download_modelscope(
    runtime_dir: Path,
    repo: str,
    files: list[str],
    stage: Path,
    renderer: ProgressRenderer,
) -> None:
    cache_dir = runtime_dir.parent / "modelscope-cache"
    with _temporary_environment({"MODELSCOPE_CACHE": str(cache_dir), "TQDM_DISABLE": "1"}):
        from modelscope_hub import HubApi, ProgressCallback

        callback = _build_modelscope_callback(ProgressCallback, renderer) if renderer.enabled else None
        HubApi().download_repo(
            repo,
            repo_type="model",
            cache_dir=cache_dir,
            local_dir=stage,
            allow_patterns=files or None,
            progress_callbacks=[callback] if callback else None,
        )


def _download_huggingface(
    source: str,
    repo: str,
    files: list[str],
    stage: Path,
    renderer: ProgressRenderer,
) -> None:
    endpoint = "https://hf-mirror.com" if source == "hf-mirror" else None
    environment = {
        "HF_ENDPOINT": endpoint,
        "HF_HUB_DISABLE_XET": "1",
        "TQDM_DISABLE": "1",
    }
    with _temporary_environment(environment):
        from huggingface_hub import snapshot_download

        kwargs = {
            "repo_id": repo,
            "local_dir": stage,
            "allow_patterns": files or None,
            "endpoint": endpoint,
        }
        if renderer.enabled:
            from tqdm.auto import tqdm

            kwargs["tqdm_class"] = _build_huggingface_tqdm(tqdm, renderer)
        snapshot_download(**kwargs)


def run_download(
    source: str,
    runtime_dir: Path,
    repo: str,
    files: list[str],
    stage: Path,
    *,
    progress: bool | None = None,
) -> tuple[bool, str]:
    stage.mkdir(parents=True, exist_ok=True)
    renderer = ProgressRenderer(enabled=progress)
    print(f"Trying source: {source}")
    try:
        if source == "modelscope":
            _download_modelscope(runtime_dir, repo, files, stage, renderer)
        elif source in {"hf-mirror", "huggingface"}:
            _download_huggingface(source, repo, files, stage, renderer)
        else:
            raise ModelFetchError(f"Unknown download source: {source}")
    except Exception as error:
        renderer.close(success=False)
        return False, f"{source} download failed: {type(error).__name__}: {error}"
    except BaseException:
        renderer.close(success=False)
        raise
    problems = validate_payload(stage, files)
    success = not problems
    renderer.close(success=success)
    return success, "" if success else f"{source} completed but validation failed: {'; '.join(problems)}"


def remove_failed_target(destination: Path, root: Path) -> None:
    if root not in destination.parents:
        raise ModelFetchError("Refusing to clean a target outside the selected root.")
    shutil.rmtree(destination)


def fetch(args: argparse.Namespace) -> int:
    files = parse_files(args.files)
    root, destination = target_path(args.repo, args.target, args.output)
    existing = validate_payload(destination, files) if destination.exists() else ["Target does not exist."]
    if destination.exists() and not existing:
        print(f"Already verified: {destination}")
        return 0
    if destination.exists():
        if not args.clean_failed:
            raise ModelFetchError(f"Target exists but is invalid: {'; '.join(existing)}. Refusing to delete it; rerun with --clean-failed only after review.")
        print(f"Removing explicitly approved failed target: {destination}")
        remove_failed_target(destination, root)
    runtime = user_runtime()
    stage_base = root / ".model-fetch-kkddyz-staging" / args.repo.replace("/", "__")
    failures: list[str] = []
    for source in SOURCE_ORDER:
        stage = stage_base / source
        success, detail = run_download(
            source,
            runtime,
            args.repo,
            files,
            stage,
            progress=False if args.no_progress else None,
        )
        if success:
            destination.parent.mkdir(parents=True, exist_ok=True)
            stage.replace(destination)
            print(f"Downloaded source: {source}")
            print(f"Verified destination: {destination}")
            return 0
        failures.append(detail)
        print(f"Source failed: {detail}", file=sys.stderr)
    raise ModelFetchError("All sources failed. " + " | ".join(failures))


def verify(args: argparse.Namespace) -> int:
    files = parse_files(args.files)
    _, destination = target_path(args.repo, args.target, args.output)
    problems = validate_payload(destination, files)
    if problems:
        print(f"Verification failed: {destination}", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 2
    print(f"Verified: {destination}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download and verify explicitly specified public model files.")
    parser.add_argument("--_runtime", action="store_true", help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("fetch", "verify"):
        subparser = subparsers.add_parser(action)
        subparser.add_argument("--repo", required=True, help="Exact publisher/repository identifier.")
        subparser.add_argument("--files", nargs="+", help="Optional filenames; omit to download the complete repository.")
        subparser.add_argument("--target", choices=tuple(DEFAULT_ROOTS), default="lmstudio")
        subparser.add_argument("--output", help="Optional replacement root directory.")
        if action == "fetch":
            subparser.add_argument("--resume", action="store_true", help="Reuse source-specific staging data from an interrupted attempt.")
            subparser.add_argument("--clean-failed", action="store_true", help="Explicitly remove an invalid target before downloading.")
            subparser.add_argument("--no-progress", action="store_true", help="Disable the download progress renderer.")
    return parser


def main() -> int:
    ensure_runtime_and_reexec()
    ensure_runtime_packages()
    args = build_parser().parse_args()
    parse_repo(args.repo)
    try:
        return fetch(args) if args.action == "fetch" else verify(args)
    except ModelFetchError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
