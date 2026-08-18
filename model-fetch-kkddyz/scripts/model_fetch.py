#!/usr/bin/env python3
"""Fetch and verify explicitly specified public model files on Windows."""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path
from typing import Iterable

DEFAULT_ROOTS = {"folder": Path(r"E:\AIModels"), "lmstudio": Path(r"E:\lmstudio-models\models")}
SOURCE_ORDER = ("modelscope", "hf-mirror", "huggingface")
WEIGHT_SUFFIXES = (".safetensors", ".bin", ".gguf", ".onnx", ".pt", ".pth")
LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"


class ModelFetchError(RuntimeError):
    pass


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


def command_for(source: str, runtime_dir: Path, repo: str, files: list[str], stage: Path) -> tuple[list[str], dict[str, str]]:
    scripts_dir = runtime_dir / ("Scripts" if os.name == "nt" else "bin")
    environment = os.environ.copy()
    if source == "modelscope":
        environment["MODELSCOPE_CACHE"] = str(runtime_dir.parent / "modelscope-cache")
        command = [str(scripts_dir / ("ms-hub.exe" if os.name == "nt" else "ms-hub")), "download", repo, *files, "--local-dir", str(stage)]
    else:
        if source == "hf-mirror":
            environment["HF_ENDPOINT"] = "https://hf-mirror.com"
        else:
            environment.pop("HF_ENDPOINT", None)
        environment["HF_HUB_DISABLE_XET"] = "1"
        command = [str(scripts_dir / ("hf.exe" if os.name == "nt" else "hf")), "download", repo, *files, "--local-dir", str(stage)]
    return command, environment


def run_download(source: str, runtime_dir: Path, repo: str, files: list[str], stage: Path) -> tuple[bool, str]:
    stage.mkdir(parents=True, exist_ok=True)
    command, environment = command_for(source, runtime_dir, repo, files, stage)
    print(f"Trying source: {source}")
    completed = subprocess.run(command, env=environment)
    if completed.returncode:
        return False, f"{source} exited with code {completed.returncode}."
    problems = validate_payload(stage, files)
    return (not problems, "" if not problems else f"{source} completed but validation failed: {'; '.join(problems)}")


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
        success, detail = run_download(source, runtime, args.repo, files, stage)
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
        subparser.add_argument("--target", choices=tuple(DEFAULT_ROOTS), default="folder")
        subparser.add_argument("--output", help="Optional replacement root directory.")
        if action == "fetch":
            subparser.add_argument("--resume", action="store_true", help="Reuse source-specific staging data from an interrupted attempt.")
            subparser.add_argument("--clean-failed", action="store_true", help="Explicitly remove an invalid target before downloading.")
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
