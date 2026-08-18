"""A conservative downloader for courses the user is allowed to save locally.

The program deliberately does not persist Bilibili cookies, install proxies,
delete existing files, or execute shell command strings. It is intentionally
single-threaded for downloads to avoid putting unnecessary load on Bilibili
endpoints. The optional QR panel uses a separate worker only for the existing
interactive login flow.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Sequence
from urllib.parse import urlsplit


BILIBILI_MEDIA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 "
        "Safari/537.36 Edg/131.0.0.0"
    ),
    "Referer": "https://www.bilibili.com",
}
MAX_STREAM_ATTEMPTS = 3
STREAM_RETRY_DELAYS = (0.5, 1.0)
MANIFEST_NOTE = "Authentication cookies and signed download URLs are intentionally not recorded."
COURSE_ID_RE = re.compile(r"(?:ss)?(?P<id>[1-9]\d*)\Z", re.IGNORECASE)
COURSE_URL_PATH_RE = re.compile(r"/cheese/play/ss(?P<id>[1-9]\d*)/?\Z", re.IGNORECASE)
ALLOWED_COURSE_HOSTS = {"www.bilibili.com", "bilibili.com"}
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL", *{f"COM{i}" for i in range(1, 10)},
    *{f"LPT{i}" for i in range(1, 10)},
}


class DownloadError(RuntimeError):
    """Raised when an input or download cannot be safely completed."""


class StreamHTTPError(DownloadError):
    """A media CDN response that callers may handle without parsing text."""

    def __init__(self, status: int, destination: Path, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.destination = destination


@dataclass(frozen=True)
class DownloadRecord:
    episode: int
    title: str
    filename: str
    bytes: int
    sha256: str


QR_EVENT_STATUS = {
    "SCAN": ("请使用 B 站 App 扫码", "#00AEEC"),
    "CONF": ("已扫码，等待确认", "#F2B84B"),
    "TIMEOUT": ("二维码已过期，正在刷新", "#F2B84B"),
    "DONE": ("登录成功，正在继续下载", "#53D597"),
}


def qr_event_status(event: object) -> tuple[str, str]:
    """Map bilibili-api QR events to user-facing text and an accent color."""
    name = getattr(event, "name", str(event).rsplit(".", 1)[-1]).upper()
    return QR_EVENT_STATUS.get(name, ("正在检查登录状态", "#9FB0BF"))


def qr_event_name(event: object) -> str:
    """Return a stable enum name for real or mocked bilibili-api events."""
    return getattr(event, "name", str(event).rsplit(".", 1)[-1]).upper()


class QrLoginPanel:
    """Small Windows-only Tk panel that owns all QR UI operations."""

    QR_SIZE = 300

    def __init__(self) -> None:
        try:
            import tkinter as tk
            from PIL import Image, ImageTk
        except ImportError as error:
            raise DownloadError(
                "无法启动二维码面板；--qr-ui 需要当前 Python 环境提供 tkinter 和 Pillow。"
            ) from error

        self._tk = tk
        self._image = Image
        self._image_tk = ImageTk
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._cancelled = threading.Event()
        self._login_succeeded = False
        self._qr_image = None

        try:
            self._root = tk.Tk()
        except Exception as error:
            raise DownloadError("无法启动二维码面板；请确认当前 Windows 会话支持图形窗口。") from error

        self._root.title("课程素材入口 · B 站登录")
        self._root.geometry("430x575")
        self._root.resizable(False, False)
        self._root.configure(bg="#111820")
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_layout()

    def _build_layout(self) -> None:
        tk = self._tk
        root = self._root
        tk.Frame(root, bg="#00AEEC", height=7).pack(fill="x")

        shell = tk.Frame(root, bg="#111820")
        shell.pack(fill="both", expand=True, padx=28, pady=(22, 20))

        tk.Label(
            shell,
            text="课程素材入口",
            bg="#111820",
            fg="#00AEEC",
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w")
        tk.Label(
            shell,
            text="扫码登录 B 站",
            bg="#111820",
            fg="#F7FAFC",
            font=("Microsoft YaHei UI", 20, "bold"),
        ).pack(anchor="w", pady=(5, 2))
        tk.Label(
            shell,
            text="仅在本次进程使用，不保存登录凭据",
            bg="#111820",
            fg="#9FB0BF",
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w")

        qr_card = tk.Frame(shell, bg="#FBFAF6", width=330, height=330)
        qr_card.pack(pady=(20, 18))
        qr_card.pack_propagate(False)
        self._qr_label = tk.Label(
            qr_card,
            text="正在生成二维码…",
            bg="#FBFAF6",
            fg="#5B6670",
            font=("Microsoft YaHei UI", 10),
        )
        self._qr_label.pack(expand=True)

        status_row = tk.Frame(shell, bg="#111820")
        status_row.pack(fill="x")
        self._status_dot = tk.Label(
            status_row,
            text="●",
            bg="#111820",
            fg="#00AEEC",
            font=("Segoe UI", 11),
        )
        self._status_dot.pack(side="left", padx=(0, 8))
        self._status_label = tk.Label(
            status_row,
            text="正在连接 B 站…",
            bg="#111820",
            fg="#D7E0E8",
            font=("Microsoft YaHei UI", 10),
            anchor="w",
        )
        self._status_label.pack(side="left", fill="x", expand=True)

        tk.Label(
            shell,
            text="二维码过期会自动刷新 · 关闭窗口将停止登录",
            bg="#111820",
            fg="#71808D",
            font=("Microsoft YaHei UI", 8),
        ).pack(anchor="w", pady=(16, 0))

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def login_succeeded(self) -> bool:
        return self._login_succeeded

    def post_qr(self, content: bytes) -> None:
        self._events.put(("qr", content))

    def post_status(self, text: str, color: str = "#00AEEC") -> None:
        self._events.put(("status", (text, color)))

    def post_qr_event(self, event: object) -> None:
        text, color = qr_event_status(event)
        self._events.put(("status", (text, color)))
        if qr_event_name(event) == "DONE":
            self._events.put(("success", None))

    def post_error(self, message: str) -> None:
        self._events.put(("error", message))

    def run(self) -> None:
        self._drain_events()
        self._root.mainloop()

    def cancel(self) -> None:
        if not self._login_succeeded:
            self._cancelled.set()

    def _on_close(self) -> None:
        self.cancel()
        self._root.destroy()

    def _drain_events(self) -> None:
        while True:
            try:
                event, payload = self._events.get_nowait()
            except queue.Empty:
                break
            if event == "qr":
                self._show_qr(payload)
            elif event == "status":
                text, color = payload
                self._set_status(text, color)
            elif event == "success":
                self._login_succeeded = True
                self._set_status("登录成功，正在继续下载", "#53D597")
                self._root.after(1000, self._root.destroy)
            elif event == "error":
                self._set_status(str(payload), "#F06D77")
                self._qr_label.configure(image="", text="无法继续")
                self._root.after(1800, self._root.destroy)
        try:
            if self._root.winfo_exists():
                self._root.after(50, self._drain_events)
        except self._tk.TclError:
            pass

    def _show_qr(self, content: object) -> None:
        if not isinstance(content, bytes) or not content:
            self.post_error("二维码图片内容为空，无法显示。")
            return
        try:
            with self._image.open(BytesIO(content)) as source:
                image = source.convert("RGB")
                resampling = getattr(self._image, "Resampling", self._image).NEAREST
                image.thumbnail((self.QR_SIZE, self.QR_SIZE), resampling)
                self._qr_image = self._image_tk.PhotoImage(image)
        except Exception as error:
            self.post_error(f"二维码图片无法显示：{error}")
            return
        self._qr_label.configure(image=self._qr_image, text="")

    def _set_status(self, text: str, color: str) -> None:
        self._status_dot.configure(fg=color)
        self._status_label.configure(text=text)


async def wait_for_qr_login(qr: object, qr_ui: QrLoginPanel, *, poll_interval: float = 1.0) -> object:
    """Drive QR login through a panel while keeping credentials in memory."""
    await qr.generate_qrcode()
    qr_ui.post_qr(qr.get_qrcode_picture().content)
    qr_ui.post_status("请使用 B 站 App 扫码")
    while not qr.has_done():
        if qr_ui.cancelled:
            raise DownloadError("二维码登录已取消；未保存登录凭据，也未开始下载。")
        event = await qr.check_state()
        event_name = qr_event_name(event)
        if event_name == "TIMEOUT":
            qr_ui.post_qr_event(event)
            await qr.generate_qrcode()
            qr_ui.post_qr(qr.get_qrcode_picture().content)
            qr_ui.post_status("二维码已刷新，请重新扫码")
            continue
        qr_ui.post_qr_event(event)
        if event_name != "DONE" and poll_interval > 0:
            await asyncio.sleep(poll_interval)
    if qr_ui.cancelled:
        raise DownloadError("二维码登录已取消；未保存登录凭据，也未开始下载。")
    return qr.get_credential()


def sanitize_component(value: str, *, fallback: str = "untitled", limit: int = 120) -> str:
    """Return a portable single path component, never a path supplied by metadata."""
    cleaned = INVALID_FILENAME_CHARS.sub("_", value).strip().rstrip(". ")
    cleaned = cleaned[:limit].rstrip(". ")
    if not cleaned:
        cleaned = fallback
    if cleaned.upper() in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


def parse_course_id(raw: str) -> int:
    match = COURSE_ID_RE.fullmatch(raw.strip())
    if not match:
        raise DownloadError("课程 ID 必须是正整数，或以 ss 开头的正整数。")
    return int(match.group("id"))


def parse_course_url(raw: str) -> int:
    """Extract a Cheese course ID from a canonical HTTPS Bilibili course URL."""
    parsed = urlsplit(raw.strip())
    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() not in ALLOWED_COURSE_HOSTS:
        raise DownloadError("课程 URL 必须是 https://www.bilibili.com/cheese/play/ss数字 形式。")
    match = COURSE_URL_PATH_RE.fullmatch(parsed.path)
    if not match:
        raise DownloadError("课程 URL 中未找到有效的 ss 课程 ID。")
    return int(match.group("id"))


def parse_episode_selection(raw: str, available: int) -> list[int]:
    """Parse comma-separated episode numbers and inclusive ranges."""
    selected: set[int] = set()
    if not raw.strip():
        raise DownloadError("请通过 --episodes 明确指定要处理的集数。")
    for item in raw.split(","):
        part = item.strip()
        if not part:
            raise DownloadError("集数列表中不能包含空项。")
        if "-" in part:
            start_text, end_text, *rest = part.split("-")
            if rest or not start_text.isdigit() or not end_text.isdigit():
                raise DownloadError(f"无效的集数范围：{part}")
            start, end = int(start_text), int(end_text)
            if start > end:
                raise DownloadError(f"集数范围起点不能大于终点：{part}")
            selected.update(range(start, end + 1))
        elif part.isdigit():
            selected.add(int(part))
        else:
            raise DownloadError(f"无效的集数：{part}")
    invalid = sorted(number for number in selected if number < 1 or number > available)
    if invalid:
        raise DownloadError(f"集数超出范围 1-{available}：{', '.join(map(str, invalid))}")
    return sorted(selected)


def require_safe_output_root(raw: str) -> Path:
    """Require an explicit non-project output directory before any network action."""
    if not raw:
        raise DownloadError("必须使用 --output 指定课程文件的保存目录。")
    output = Path(raw).expanduser().resolve()
    application_directory = Path(__file__).resolve().parent
    if output == Path.cwd().resolve() or output == application_directory:
        raise DownloadError("输出目录不能是程序目录；请指定专用的课程保存目录。")
    try:
        output.relative_to(application_directory)
    except ValueError:
        pass
    else:
        raise DownloadError("输出目录不能位于程序目录内；请指定仓库外的专用课程保存目录。")
    output.mkdir(parents=True, exist_ok=True)
    if not output.is_dir():
        raise DownloadError("--output 必须指向目录。")
    return output


def find_ffmpeg_executable() -> str:
    """Prefer the verified project-local FFmpeg, then fall back to PATH."""
    project_ffmpeg = Path(__file__).resolve().parent / ".tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
    return str(project_ffmpeg) if project_ffmpeg.is_file() else "ffmpeg"


def build_ffmpeg_command(
    video_file: Path,
    audio_file: Path,
    temporary_output: Path,
    *,
    executable: str | Path | None = None,
) -> list[str]:
    """Build an argument list, never a shell command string."""
    return [
        str(executable or find_ffmpeg_executable()),
        "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video_file), "-i", str(audio_file),
        "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-shortest",
        str(temporary_output),
    ]


def sha256_file(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json_atomically(destination: Path, payload: object) -> None:
    if destination.exists():
        raise DownloadError(f"清单已存在，默认不覆盖：{destination}")
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    if temporary.exists():
        raise DownloadError(f"发现未完成清单，请先人工检查：{temporary}")
    with temporary.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)


def _record_from_manifest_payload(payload: object, index: int) -> DownloadRecord:
    if not isinstance(payload, dict):
        raise DownloadError(f"清单第 {index} 条集数记录格式无效。")

    required = {"episode", "title", "filename", "bytes", "sha256"}
    if not required.issubset(payload):
        missing = ", ".join(sorted(required - payload.keys()))
        raise DownloadError(f"清单第 {index} 条记录缺少字段：{missing}。")

    episode = payload["episode"]
    title = payload["title"]
    filename = payload["filename"]
    byte_count = payload["bytes"]
    digest = payload["sha256"]
    if not isinstance(episode, int) or isinstance(episode, bool) or episode < 1:
        raise DownloadError(f"清单第 {index} 条记录的集数无效。")
    if not isinstance(title, str) or not title.strip():
        raise DownloadError(f"清单第 {index} 条记录的标题无效。")
    if (
        not isinstance(filename, str)
        or not filename.strip()
        or Path(filename).name != filename
        or Path(filename).suffix.lower() != ".mp4"
    ):
        raise DownloadError(f"清单第 {index} 条记录的文件名不安全或不是 MP4。")
    if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count <= 0:
        raise DownloadError(f"清单第 {index} 条记录的文件大小无效。")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise DownloadError(f"清单第 {index} 条记录的 SHA-256 无效。")

    return DownloadRecord(episode, title, filename, byte_count, digest.lower())


def load_course_manifest(
    manifest_path: Path,
    *,
    course_id: int,
    course_title: str,
) -> dict[int, DownloadRecord]:
    """Load and validate an incremental course manifest without changing it."""
    if not manifest_path.exists():
        return {}
    if not manifest_path.is_file():
        raise DownloadError(f"课程清单路径不是文件，已停止：{manifest_path}")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DownloadError(f"课程清单无法读取或不是有效 JSON：{manifest_path}") from error
    if not isinstance(payload, dict):
        raise DownloadError(f"课程清单顶层格式无效：{manifest_path}")
    if payload.get("course_id") != course_id:
        raise DownloadError("课程清单的 course_id 与当前课程不一致，已停止且不会覆盖文件。")
    if payload.get("course_title") != course_title:
        raise DownloadError("课程清单的课程标题与当前课程不一致，已停止且不会覆盖文件。")
    episode_payloads = payload.get("episodes")
    if not isinstance(episode_payloads, list):
        raise DownloadError("课程清单的 episodes 字段格式无效，已停止且不会覆盖文件。")

    records: dict[int, DownloadRecord] = {}
    for index, item in enumerate(episode_payloads, start=1):
        record = _record_from_manifest_payload(item, index)
        if record.episode in records:
            raise DownloadError(f"课程清单重复记录第 {record.episode} 集，已停止。")
        records[record.episode] = record
    return records


def verify_existing_record(
    course_directory: Path,
    record: DownloadRecord,
    *,
    expected_filename: str | None = None,
    expected_title: str | None = None,
) -> Path:
    """Verify a recorded MP4 before an all-course run skips it."""
    if expected_filename is not None and record.filename != expected_filename:
        raise DownloadError(
            f"第 {record.episode} 集清单文件名与当前课程信息不一致："
            f"{record.filename} != {expected_filename}。"
        )
    if expected_title is not None and record.title != expected_title:
        raise DownloadError(f"第 {record.episode} 集清单标题与当前课程信息不一致，已停止。")

    media = course_directory / record.filename
    if not media.is_file():
        raise DownloadError(f"第 {record.episode} 集清单记录对应的 MP4 不存在：{media}")
    actual_size = media.stat().st_size
    if actual_size <= 0:
        raise DownloadError(f"第 {record.episode} 集 MP4 为空，已停止且不会覆盖：{media}")
    if actual_size != record.bytes:
        raise DownloadError(
            f"第 {record.episode} 集 MP4 大小与清单不一致，已停止且不会覆盖：{media}"
        )
    actual_hash = sha256_file(media)
    if actual_hash != record.sha256:
        raise DownloadError(
            f"第 {record.episode} 集 MP4 SHA-256 与清单不一致，已停止且不会覆盖：{media}"
        )
    return media


def _course_manifest_payload(
    course_id: int,
    course_title: str,
    records: Iterable[DownloadRecord],
) -> dict[str, object]:
    return {
        "course_id": course_id,
        "course_title": course_title,
        "episodes": [asdict(record) for record in sorted(records, key=lambda item: item.episode)],
        "note": MANIFEST_NOTE,
    }


def update_course_manifest_atomically(
    destination: Path,
    *,
    course_id: int,
    course_title: str,
    records: Iterable[DownloadRecord],
) -> None:
    """Atomically update only a validated course manifest after one episode."""
    if destination.exists() and not destination.is_file():
        raise DownloadError(f"课程清单路径不是文件，已停止：{destination}")
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    if temporary.exists():
        raise DownloadError(f"发现未完成清单，请先人工检查：{temporary}")
    payload = _course_manifest_payload(course_id, course_title, records)
    with temporary.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)


def select_streams(streams: Iterable[object]) -> tuple[object, object]:
    """Select streams by their public object type instead of list position."""
    video_stream = None
    audio_stream = None
    for stream in streams:
        kind = type(stream).__name__.lower()
        if "video" in kind and video_stream is None:
            video_stream = stream
        elif "audio" in kind and audio_stream is None:
            audio_stream = stream
    if video_stream is None or audio_stream is None:
        raise DownloadError("未能从课程接口识别独立音频和视频流；请更新依赖后重试。")
    return video_stream, audio_stream


async def download_stream(url: str, destination: Path, *, stream_label: str = "媒体流") -> int:
    """Download one stream to a resumable .part file without accepting size drift."""
    try:
        import aiohttp
    except ImportError as error:
        raise DownloadError("缺少 aiohttp；请先按 README 创建隔离环境并安装依赖。") from error

    offset = destination.stat().st_size if destination.exists() else 0
    headers = dict(BILIBILI_MEDIA_HEADERS)
    if offset:
        headers["Range"] = f"bytes={offset}-"
    timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as response:
            expected_status = 206 if offset else 200
            if response.status != expected_status:
                partial_state = (
                    f"已保留临时文件：{destination.name}"
                    if destination.exists()
                    else "尚未创建临时文件"
                )
                raise StreamHTTPError(
                    response.status,
                    destination,
                    f"{stream_label}请求返回 HTTP {response.status}（预期 {expected_status}）；{partial_state}。",
                )
            expected_size = offset + response.content_length if response.content_length is not None else None
            mode = "ab" if offset else "xb"
            try:
                with destination.open(mode) as target:
                    async for block in response.content.iter_chunked(1024 * 1024):
                        target.write(block)
                    target.flush()
                    os.fsync(target.fileno())
            except FileExistsError as error:
                raise DownloadError(f"临时文件已存在但无法安全续传：{destination}") from error
    actual_size = destination.stat().st_size
    if expected_size is not None and actual_size != expected_size:
        raise DownloadError(
            f"下载大小不一致（预期 {expected_size}，实际 {actual_size}）；"
            f"已保留 {destination.name} 供检查或续传。"
        )
    if actual_size == 0:
        raise DownloadError(f"下载结果为空：{destination.name}")
    return actual_size


async def get_episode_stream(episode: object, stream_kind: str) -> object:
    """Fetch one fresh course stream object without exposing its signed URL."""
    if stream_kind not in {"video", "audio"}:
        raise DownloadError(f"不支持的流类型：{stream_kind}")
    try:
        from bilibili_api import video
    except ImportError as error:
        raise DownloadError("bilibili-api-python 安装不完整。") from error

    detected = video.VideoDownloadURLDataDetecter(
        data=await episode.get_download_url()
    ).detect_best_streams()
    video_stream, audio_stream = select_streams(detected or [])
    return video_stream if stream_kind == "video" else audio_stream


async def download_stream_with_retries(
    stream_url_provider: Callable[[], Awaitable[str]],
    destination: Path,
    *,
    stream_label: str,
    max_attempts: int = MAX_STREAM_ATTEMPTS,
    retry_delays: Sequence[float] = STREAM_RETRY_DELAYS,
) -> int:
    """Download one stream and refresh its signed URL only after HTTP 403."""
    if max_attempts < 1:
        raise DownloadError("流下载至少需要允许 1 次请求。")

    for attempt in range(1, max_attempts + 1):
        url = await stream_url_provider()
        try:
            return await download_stream(url, destination, stream_label=stream_label)
        except StreamHTTPError as error:
            if error.status != 403 or attempt == max_attempts:
                if error.status == 403:
                    partial_state = (
                        f"已保留临时文件：{destination.name}"
                        if destination.exists()
                        else "尚未创建临时文件"
                    )
                    raise DownloadError(
                        f"{stream_label}连续 {max_attempts} 次收到 HTTP 403；{partial_state}。"
                    ) from error
                raise

            print(
                f"{stream_label}收到 HTTP 403，刷新临时链接后重试"
                f"（{attempt + 1}/{max_attempts}）"
            )
            delay_index = attempt - 1
            if delay_index < len(retry_delays) and retry_delays[delay_index] > 0:
                await asyncio.sleep(retry_delays[delay_index])

    raise DownloadError(f"{stream_label}下载未产生结果。")


def merge_streams(video_file: Path, audio_file: Path, final_output: Path) -> None:
    if final_output.exists():
        raise DownloadError(f"目标已存在，默认不覆盖：{final_output}")
    temporary_output = final_output.with_name(f".{final_output.stem}.merging.mp4")
    if temporary_output.exists():
        raise DownloadError(f"发现未完成合成文件，请先人工检查：{temporary_output}")
    try:
        subprocess.run(
            build_ffmpeg_command(video_file, audio_file, temporary_output),
            check=True,
            capture_output=True,
            text=True,
            shell=False,
        )
    except FileNotFoundError as error:
        raise DownloadError("未找到 FFmpeg；请检查项目 .tools\\ffmpeg 或系统 PATH。") from error
    except subprocess.CalledProcessError as error:
        details = error.stderr.strip() or "ffmpeg 未提供错误详情"
        raise DownloadError(f"音视频合成失败：{details}") from error
    if not temporary_output.exists() or temporary_output.stat().st_size == 0:
        raise DownloadError("ffmpeg 未生成有效输出；已保留输入临时文件。")
    os.replace(temporary_output, final_output)


async def login_and_load_course(course_id: int, qr_ui: QrLoginPanel | None = None):
    """Log in interactively; credentials only live in process memory."""
    try:
        from bilibili_api import cheese, login_v2
    except ImportError as error:
        raise DownloadError("缺少 bilibili-api-python；请先按 README 创建隔离环境并安装依赖。") from error

    qr = login_v2.QrCodeLogin(platform=login_v2.QrCodeLoginChannel.WEB)
    if qr_ui is None:
        await qr.generate_qrcode()
        print(qr.get_qrcode_terminal())
        print("请使用 B 站 App 扫码登录。凭据只在本次进程内使用，不会保存到磁盘。")
        while not qr.has_done():
            await qr.check_state()
            await asyncio.sleep(1)
        credential = qr.get_credential()
    else:
        credential = await wait_for_qr_login(qr, qr_ui)
    course = cheese.CheeseList(season_id=course_id, credential=credential)
    metadata = await course.get_meta()
    episodes = await course.get_list()
    if not metadata.get("title") or not episodes:
        raise DownloadError("无法读取课程信息或课程没有可用集数。")
    return course, metadata, episodes


async def download_course(args: argparse.Namespace, qr_ui: QrLoginPanel | None = None) -> None:
    course_id = parse_course_url(args.course_url) if args.course_url else parse_course_id(args.course_id)
    output_root = require_safe_output_root(args.output)
    _, metadata, episodes = await login_and_load_course(course_id, qr_ui=qr_ui)
    chosen = list(range(1, len(episodes) + 1)) if args.all_episodes else parse_episode_selection(args.episodes, len(episodes))
    course_name = sanitize_component(metadata["title"])
    course_directory = output_root / f"{course_id}_{course_name}"
    course_directory.mkdir(exist_ok=True)
    partial_directory = course_directory / ".partials"
    partial_directory.mkdir(exist_ok=True)
    manifest_path = course_directory / "course-manifest.json"
    all_mode = bool(args.all_episodes)
    records: dict[int, DownloadRecord] = {}
    if all_mode:
        records = load_course_manifest(
            manifest_path,
            course_id=course_id,
            course_title=metadata["title"],
        )
        out_of_range = sorted(number for number in records if number > len(episodes))
        if out_of_range:
            raise DownloadError(
                f"课程清单包含当前课程不存在的集数：{out_of_range}；已停止且不会覆盖文件。"
            )

    # Sequential by design: reduces remote rate-limit and local disk-integrity risk.
    for number in chosen:
        episode = episodes[number - 1]
        episode_meta = await episode.get_meta()
        original_title = episode_meta.get("title") or f"episode-{number}"
        title = sanitize_component(original_title)
        final_file = course_directory / f"{number:03d}_{title}.mp4"
        existing_record = records.get(number)
        if all_mode and existing_record is not None:
            verify_existing_record(
                course_directory,
                existing_record,
                expected_filename=final_file.name,
                expected_title=original_title,
            )
            print(f"[{number}/{len(episodes)}] 已验证，跳过：{original_title}")
            update_course_manifest_atomically(
                manifest_path,
                course_id=course_id,
                course_title=metadata["title"],
                records=records.values(),
            )
            continue
        if all_mode and final_file.exists():
            raise DownloadError(
                f"第 {number} 集已有 MP4 但清单没有对应记录，已停止且不会覆盖：{final_file}"
            )
        if not all_mode and final_file.exists():
            raise DownloadError(f"目标已存在，默认停止以防覆盖：{final_file}")
        video_part = partial_directory / f"{number:03d}_{title}.video.m4s.part"
        audio_part = partial_directory / f"{number:03d}_{title}.audio.m4s.part"
        print(f"[{number}/{len(episodes)}] 下载：{original_title}")

        async def video_url_provider() -> str:
            return (await get_episode_stream(episode, "video")).url

        async def audio_url_provider() -> str:
            return (await get_episode_stream(episode, "audio")).url

        await download_stream_with_retries(
            video_url_provider,
            video_part,
            stream_label="视频流",
        )
        await download_stream_with_retries(
            audio_url_provider,
            audio_part,
            stream_label="音频流",
        )
        merge_streams(video_part, audio_part, final_file)
        records[number] = DownloadRecord(
            number,
            original_title,
            final_file.name,
            final_file.stat().st_size,
            sha256_file(final_file),
        )
        if all_mode:
            update_course_manifest_atomically(
                manifest_path,
                course_id=course_id,
                course_title=metadata["title"],
                records=records.values(),
            )
        print(f"完成：{final_file.name}")

    if all_mode:
        expected_numbers = set(range(1, len(episodes) + 1))
        if set(records) != expected_numbers:
            missing = sorted(expected_numbers - set(records))
            raise DownloadError(f"全量任务未完成，缺少集数：{missing}。")
        for number in sorted(expected_numbers):
            verify_existing_record(course_directory, records[number])
        print(f"完成全课程：{len(records)}/{len(episodes)} 集")
    else:
        write_json_atomically(
            manifest_path,
            _course_manifest_payload(course_id, metadata["title"], records.values()),
        )
    print(f"完成。清单：{manifest_path}")
    print(".partials 中的文件用于人工检查或续传；程序不会自动删除它们。")


def run_download_with_qr_ui(args: argparse.Namespace) -> None:
    """Run the async downloader in a worker while Tk owns the main thread."""
    panel = QrLoginPanel()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            asyncio.run(download_course(args, qr_ui=panel))
        except BaseException as error:  # Re-raise in the CLI thread after the window closes.
            errors.append(error)
            panel.post_error(str(error))

    download_thread = threading.Thread(target=worker, name="course-download", daemon=False)
    download_thread.start()
    try:
        panel.run()
    finally:
        if not panel.login_succeeded:
            panel.cancel()
        download_thread.join()
    if errors:
        raise errors[0]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="仅供已获授权课程的安全本地保存工具；不保存登录凭据，不自动删除文件。")
    course_source = parser.add_mutually_exclusive_group(required=True)
    course_source.add_argument("--course-url", help="B站课程链接，例如 https://www.bilibili.com/cheese/play/ss360")
    course_source.add_argument("--course-id", help="兼容旧用法：课程 ID，例如 ss360 或 360")
    parser.add_argument("--output", required=True, help="显式指定的专用输出目录")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--episodes", help="明确指定集数，例如 1,3-5")
    group.add_argument("--all-episodes", action="store_true", help="明确确认下载全部集数")
    parser.add_argument("--qr-ui", action="store_true", help="使用轻量二维码窗口登录；不传则使用终端二维码")
    parser.add_argument("--confirm-personal-use", action="store_true", help="确认你拥有课程访问和本地保存权限，且仅供个人使用")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.confirm_personal_use:
        parser.error("必须传入 --confirm-personal-use；请勿将本工具用于无授权内容。")
    try:
        if args.qr_ui:
            run_download_with_qr_ui(args)
        else:
            asyncio.run(download_course(args))
    except DownloadError as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(2) from error
    except KeyboardInterrupt:
        print("已中断；现有 .part 文件已保留，未自动删除。", file=sys.stderr)
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
