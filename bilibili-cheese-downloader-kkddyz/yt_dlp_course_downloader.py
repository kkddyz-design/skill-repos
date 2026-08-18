"""Download authorized Bilibili Cheese course media through Firefox and yt-dlp.

This backend deliberately uses yt-dlp's Firefox cookie reader in memory. It
does not export cookies, persist browser sessions, or attempt to decrypt the
cookie stores of other browsers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit

from safe_course_downloader import (
    DownloadError,
    find_ffmpeg_executable,
    parse_episode_selection,
    require_safe_output_root,
    sanitize_component,
    sha256_file,
)


SUPPORTED_BROWSER = "firefox"
MANIFEST_NAME = "course-manifest.json"
COURSE_PATH_RE = re.compile(r"^/cheese/play/ss(?P<id>[1-9]\d*)/?$", re.IGNORECASE)
EPISODE_PATH_RE = re.compile(r"^/cheese/play/ep(?P<id>[1-9]\d*)/?$", re.IGNORECASE)
ALLOWED_HOSTS = {"bilibili.com", "www.bilibili.com"}


@dataclass(frozen=True)
class Episode:
    index: int
    episode_id: int
    title: str
    duration: float | None

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/cheese/play/ep{self.episode_id}"


@dataclass(frozen=True)
class ManifestRecord:
    episode: int
    episode_id: int
    title: str
    filename: str
    bytes: int
    sha256: str
    duration: float | None


@dataclass(frozen=True)
class Target:
    kind: str
    identifier: int
    url: str


def parse_target_url(raw: str) -> Target:
    """Validate one Bilibili Cheese course or episode URL."""
    value = raw.strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise DownloadError("只接受 https://www.bilibili.com 的课堂课程或单集 URL。")
    if parsed.query and any(part.strip() for part in parsed.query.split("&")):
        # Tracking parameters are harmless, but the path remains the source of truth.
        pass
    if parsed.fragment:
        raise DownloadError("B 站课堂 URL 不应包含 fragment。")
    course_match = COURSE_PATH_RE.fullmatch(parsed.path)
    if course_match:
        return Target("course", int(course_match.group("id")), value)
    episode_match = EPISODE_PATH_RE.fullmatch(parsed.path)
    if episode_match:
        return Target("episode", int(episode_match.group("id")), value)
    raise DownloadError("URL 必须是 B 站课堂课程 ss... 或单集 ep... 地址。")


def build_ytdlp_base_command(browser: str = SUPPORTED_BROWSER) -> list[str]:
    if browser != SUPPORTED_BROWSER:
        raise DownloadError("当前后端只支持 Firefox 会话；不会尝试解密 Chrome 或 Edge Cookie。")
    return [sys.executable, "-m", "yt_dlp", "--cookies-from-browser", browser]


def _run_metadata(command: Sequence[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        raise DownloadError(f"无法启动 yt-dlp：{error}") from error
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout).strip()
        raise DownloadError(f"yt-dlp 获取播放信息失败（退出码 {completed.returncode}）：{details[-1200:]}")
    output = completed.stdout.strip()
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise DownloadError("yt-dlp 返回的播放信息不是有效 JSON。") from error
    if not isinstance(payload, dict):
        raise DownloadError("yt-dlp 返回的播放信息格式无效。")
    return payload


def _metadata_command(url: str, browser: str, *, playlist: bool) -> list[str]:
    command = build_ytdlp_base_command(browser)
    command.extend(["--dump-single-json", "--skip-download", "--quiet", "--no-warnings"])
    if playlist:
        command.append("--flat-playlist")
    command.append("--yes-playlist" if playlist else "--no-playlist")
    command.append(url)
    return command


def _episode_from_payload(payload: object, index: int) -> Episode:
    if not isinstance(payload, dict):
        raise DownloadError(f"第 {index} 集的播放信息格式无效。")
    raw_id = payload.get("id")
    try:
        episode_id = int(str(raw_id))
    except (TypeError, ValueError) as error:
        raise DownloadError(f"第 {index} 集缺少有效的 episode ID。") from error
    if episode_id < 1:
        raise DownloadError(f"第 {index} 集的 episode ID 无效。")
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        title = f"episode-{episode_id}"
    duration = payload.get("duration")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
        normalized_duration: float | None = float(duration)
    else:
        normalized_duration = None
    return Episode(index, episode_id, title.strip(), normalized_duration)


def enumerate_course(url: str, browser: str) -> tuple[str, list[Episode]]:
    payload = _run_metadata(_metadata_command(url, browser, playlist=True))
    course_title = payload.get("title")
    if not isinstance(course_title, str) or not course_title.strip():
        course_title = f"bilibili-course-{urlsplit(url).path.rsplit('/', 1)[-1]}"
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise DownloadError("课程清单为空，或 yt-dlp 没有返回可下载的集数。")
    episodes: list[Episode] = []
    for index, entry in enumerate(raw_entries, start=1):
        episodes.append(_episode_from_payload(entry, index))
    return course_title.strip(), episodes


def inspect_episode(url: str, browser: str) -> Episode:
    payload = _run_metadata(_metadata_command(url, browser, playlist=False))
    return _episode_from_payload(payload, 1)


def _manifest_identity(target: Target, course_title: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_type": target.kind,
        "source_id": target.identifier,
        "course_title": course_title,
        "note": "Firefox session and signed media URLs are not recorded.",
    }


def _manifest_record(payload: object, index: int) -> ManifestRecord:
    if not isinstance(payload, dict):
        raise DownloadError(f"清单第 {index} 条记录格式无效。")
    required = {"episode", "episode_id", "title", "filename", "bytes", "sha256", "duration"}
    missing = required - set(payload)
    if missing:
        raise DownloadError(f"清单第 {index} 条记录缺少字段：{', '.join(sorted(missing))}。")
    episode = payload["episode"]
    episode_id = payload["episode_id"]
    byte_count = payload["bytes"]
    if not isinstance(episode, int) or isinstance(episode, bool) or episode < 1:
        raise DownloadError(f"清单第 {index} 条记录的集数无效。")
    if not isinstance(episode_id, int) or isinstance(episode_id, bool) or episode_id < 1:
        raise DownloadError(f"清单第 {index} 条记录的 episode_id 无效。")
    if not isinstance(payload["title"], str) or not payload["title"].strip():
        raise DownloadError(f"清单第 {index} 条记录的标题无效。")
    filename = payload["filename"]
    if (
        not isinstance(filename, str)
        or Path(filename).name != filename
        or Path(filename).suffix.lower() != ".mp4"
    ):
        raise DownloadError(f"清单第 {index} 条记录的文件名不安全。")
    if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count <= 0:
        raise DownloadError(f"清单第 {index} 条记录的文件大小无效。")
    digest = payload["sha256"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise DownloadError(f"清单第 {index} 条记录的 SHA-256 无效。")
    duration = payload["duration"]
    if duration is not None and (
        not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration < 0
    ):
        raise DownloadError(f"清单第 {index} 条记录的时长无效。")
    return ManifestRecord(
        episode,
        episode_id,
        payload["title"].strip(),
        filename,
        byte_count,
        digest.lower(),
        float(duration) if duration is not None else None,
    )


def load_manifest(path: Path, target: Target, course_title: str) -> dict[int, ManifestRecord]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DownloadError(f"课程清单无法读取：{path}") from error
    if not isinstance(payload, dict):
        raise DownloadError("课程清单顶层格式无效。")
    identity = _manifest_identity(target, course_title)
    for key, expected in identity.items():
        if payload.get(key) != expected:
            raise DownloadError(f"课程清单字段 {key} 与当前任务不一致，已停止。")
    raw_records = payload.get("episodes")
    if not isinstance(raw_records, list):
        raise DownloadError("课程清单的 episodes 字段格式无效。")
    records: dict[int, ManifestRecord] = {}
    for index, raw_record in enumerate(raw_records, start=1):
        record = _manifest_record(raw_record, index)
        if record.episode in records:
            raise DownloadError(f"课程清单重复记录第 {record.episode} 集。")
        records[record.episode] = record
    return records


def verify_record(output_dir: Path, record: ManifestRecord, expected: Episode) -> Path:
    if record.episode_id != expected.episode_id or record.title != expected.title:
        raise DownloadError(f"第 {expected.index} 集的课程信息与清单不一致。")
    if record.filename != expected_filename(expected):
        raise DownloadError(f"第 {expected.index} 集的文件名与清单不一致。")
    target = output_dir / record.filename
    if not target.is_file() or target.stat().st_size <= 0:
        raise DownloadError(f"第 {expected.index} 集清单对应的 MP4 不存在或为空：{target}")
    actual_size = target.stat().st_size
    if actual_size != record.bytes:
        raise DownloadError(f"第 {expected.index} 集 MP4 大小与清单不一致，已停止。")
    if sha256_file(target) != record.sha256:
        raise DownloadError(f"第 {expected.index} 集 MP4 SHA-256 与清单不一致，已停止。")
    return target


def expected_filename(episode: Episode) -> str:
    title = sanitize_component(episode.title, fallback=f"episode-{episode.episode_id}", limit=150)
    return f"{episode.index:03d}_{title} [{episode.episode_id}].mp4"


def resolve_workspace_root(raw: str | None) -> Path:
    """Resolve the workspace used for the default course output directory."""
    workspace = Path(raw).expanduser().resolve() if raw else Path.cwd().resolve()
    if not workspace.is_dir():
        raise DownloadError(f"工作区目录不存在或不是目录：{workspace}")
    return workspace


def default_output_dir(workspace_root: Path, target: Target, course_title: str) -> Path:
    """Build a safe, deterministic output path without creating it."""
    fallback = f"{target.kind}-{target.identifier}"
    folder_name = sanitize_component(course_title, fallback=fallback, limit=150)
    return workspace_root / folder_name


def ensure_default_output_is_safe(output_dir: Path) -> None:
    """Reject a non-empty title collision that has no verifiable manifest."""
    if not output_dir.exists():
        return
    if not output_dir.is_dir():
        raise DownloadError(f"默认课程路径已被文件占用，已停止且不会覆盖：{output_dir}")
    manifest = output_dir / MANIFEST_NAME
    if not manifest.exists() and any(output_dir.iterdir()):
        raise DownloadError(
            f"默认课程目录已存在但没有课程清单，可能与另一课程同名；"
            f"请确认后使用 --output 指定新目录：{output_dir}"
        )


def _ffmpeg_args() -> list[str]:
    executable = Path(find_ffmpeg_executable())
    if executable.is_file():
        return ["--ffmpeg-location", str(executable.parent)]
    return []


def build_download_command(episode: Episode, output_dir: Path, browser: str) -> list[str]:
    filename = expected_filename(episode)
    stem = output_dir / filename[:-4]
    command = build_ytdlp_base_command(browser)
    command.extend(
        [
            "--no-playlist",
            "--continue",
            "--no-overwrites",
            "--retries",
            "3",
            "--fragment-retries",
            "3",
            "--concurrent-fragments",
            "1",
            "--merge-output-format",
            "mp4",
            *(_ffmpeg_args()),
            "--output",
            f"{stem}.%(ext)s",
            episode.url,
        ]
    )
    return command


def download_episode(episode: Episode, output_dir: Path, browser: str) -> Path:
    target = output_dir / expected_filename(episode)
    command = build_download_command(episode, output_dir, browser)
    print(f"下载第 {episode.index} 集：{episode.title}")
    try:
        completed = subprocess.run(command, check=False)
    except OSError as error:
        raise DownloadError(f"无法启动 yt-dlp：{error}") from error
    if completed.returncode != 0:
        raise DownloadError(
            f"第 {episode.index} 集下载失败（yt-dlp 退出码 {completed.returncode}）；"
            "已保留可续传文件，未覆盖已有 MP4。"
        )
    if not target.is_file() or target.stat().st_size <= 0:
        raise DownloadError(f"第 {episode.index} 集未生成有效 MP4：{target}")
    return target


def probe_duration(file_path: Path) -> float | None:
    executable = Path(find_ffmpeg_executable()).with_name("ffprobe.exe")
    command = str(executable) if executable.is_file() else "ffprobe"
    try:
        completed = subprocess.run(
            [command, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(file_path)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        raise DownloadError(f"无法启动 ffprobe 检查 MP4：{error}") from error
    if completed.returncode != 0:
        raise DownloadError(f"ffprobe 无法读取 MP4：{file_path}")
    try:
        return float(completed.stdout.strip())
    except ValueError as error:
        raise DownloadError(f"ffprobe 未返回有效时长：{file_path}") from error


def make_record(episode: Episode, output_dir: Path) -> ManifestRecord:
    target = output_dir / expected_filename(episode)
    return ManifestRecord(
        episode.index,
        episode.episode_id,
        episode.title,
        target.name,
        target.stat().st_size,
        sha256_file(target),
        probe_duration(target),
    )


def write_manifest(path: Path, target: Target, course_title: str, records: dict[int, ManifestRecord]) -> None:
    payload = _manifest_identity(target, course_title)
    payload["episodes"] = [asdict(records[index]) for index in sorted(records)]
    if path.exists() and not path.is_file():
        raise DownloadError(f"课程清单路径不是文件，已停止：{path}")
    temporary = path.with_suffix(path.suffix + ".tmp")
    if temporary.exists():
        raise DownloadError(f"发现未完成清单，请先人工检查：{temporary}")
    with temporary.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def select_episodes(args: argparse.Namespace, target: Target) -> tuple[str, list[Episode]]:
    if target.kind == "episode":
        if args.all_episodes or args.episodes:
            raise DownloadError("单集 URL 不能同时使用 --episodes 或 --all-episodes。")
        episode = inspect_episode(target.url, args.browser)
        return episode.title, [episode]

    if not args.all_episodes and not args.episodes and not args.print_default_output:
        raise DownloadError("课程 URL 必须明确使用 --episodes 或 --all-episodes。")
    course_title, episodes = enumerate_course(target.url, args.browser)
    if args.all_episodes or args.print_default_output:
        return course_title, episodes
    indexes = parse_episode_selection(args.episodes, len(episodes))
    return course_title, [episodes[index - 1] for index in indexes]


def run(args: argparse.Namespace) -> None:
    if not args.confirm_personal_use:
        raise DownloadError("必须确认你拥有课程访问权限且仅用于个人使用。")
    if args.print_default_output and args.output:
        raise DownloadError("--print-default-output 不能与 --output 同时使用。")
    if not args.print_default_output and args.output and not args.confirm_output_path:
        raise DownloadError("必须先确认实际下载路径，再传入 --confirm-output-path。")
    target = parse_target_url(args.url)
    course_title, episodes = select_episodes(args, target)

    workspace_root = resolve_workspace_root(args.workspace_root)
    if args.print_default_output:
        print(default_output_dir(workspace_root, target, course_title))
        return

    if not args.confirm_output_path:
        raise DownloadError("必须先确认实际下载路径，再传入 --confirm-output-path。")
    if args.output:
        output_dir = require_safe_output_root(args.output)
    else:
        output_dir = default_output_dir(workspace_root, target, course_title)
        ensure_default_output_is_safe(output_dir)
        output_dir = require_safe_output_root(str(output_dir))
    manifest_path = output_dir / MANIFEST_NAME
    records = load_manifest(manifest_path, target, course_title)

    if args.verify:
        for episode in episodes:
            record = records.get(episode.index)
            if record is None:
                raise DownloadError(f"第 {episode.index} 集没有清单记录，无法验证。")
            print(f"验证第 {episode.index} 集：{verify_record(output_dir, record, episode)}")
        print(f"验证完成：{len(episodes)} 集。清单：{manifest_path}")
        return

    for episode in episodes:
        existing = records.get(episode.index)
        if existing is not None:
            print(f"跳过已验证的第 {episode.index} 集：{verify_record(output_dir, existing, episode)}")
            continue
        target_file = output_dir / expected_filename(episode)
        if target_file.exists():
            raise DownloadError(
                f"第 {episode.index} 集已有 MP4 但清单中没有有效记录，已停止且不会覆盖：{target_file}"
            )
        download_episode(episode, output_dir, args.browser)
        record = make_record(episode, output_dir)
        records[episode.index] = record
        write_manifest(manifest_path, target, course_title, records)
        print(f"完成第 {episode.index} 集：{record.bytes} bytes，SHA-256 {record.sha256}")
    print(f"完成。清单：{manifest_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="仅供已获授权 B 站课堂素材的安全本地保存；只读取 Firefox 会话，不保存 Cookie。"
    )
    parser.add_argument("--url", required=True, help="B 站课堂课程 ss... 或单集 ep... URL")
    parser.add_argument("--output", help="已确认的 MP4 和清单输出目录；省略时使用工作区下的课程名称目录")
    parser.add_argument("--workspace-root", help="默认输出目录的工作区根目录；省略时使用当前工作目录")
    parser.add_argument(
        "--print-default-output",
        action="store_true",
        help="只解析并打印默认输出目录，不创建目录、不下载、不写清单",
    )
    parser.add_argument("--browser", choices=[SUPPORTED_BROWSER], default=SUPPORTED_BROWSER)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--episodes", help="课程集数，例如 1,3-5")
    selection.add_argument("--all-episodes", action="store_true", help="明确确认下载课程全部集数")
    parser.add_argument("--verify", action="store_true", help="只验证清单和已有 MP4，不下载")
    parser.add_argument(
        "--confirm-personal-use",
        action="store_true",
        help="确认拥有课程访问权限且仅供个人使用",
    )
    parser.add_argument(
        "--confirm-output-path",
        action="store_true",
        help="确认已核对并允许写入 --output 指定目录",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        run(args)
    except DownloadError as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
