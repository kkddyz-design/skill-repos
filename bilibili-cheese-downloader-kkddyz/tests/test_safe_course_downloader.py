import asyncio
import hashlib
import http.server
import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from safe_course_downloader import (  # noqa: E402
    BILIBILI_MEDIA_HEADERS,
    DownloadError,
    build_ffmpeg_command,
    build_parser,
    download_stream,
    download_stream_with_retries,
    DownloadRecord,
    load_course_manifest,
    parse_course_id,
    parse_course_url,
    parse_episode_selection,
    qr_event_status,
    wait_for_qr_login,
    merge_streams,
    require_safe_output_root,
    sanitize_component,
    sha256_file,
    update_course_manifest_atomically,
    verify_existing_record,
    write_json_atomically,
)
import yt_dlp_course_downloader as ytdlp_course  # noqa: E402


class ScriptedMediaHandler(http.server.BaseHTTPRequestHandler):
    responses = []
    requests = []

    def do_GET(self):  # noqa: N802
        type(self).requests.append(
            {
                "user-agent": self.headers.get("User-Agent"),
                "referer": self.headers.get("Referer"),
                "range": self.headers.get("Range"),
            }
        )
        status, body = type(self).responses.pop(0) if type(self).responses else (500, b"")
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        return


class SafeCourseDownloaderTests(unittest.TestCase):
    def _start_scripted_server(self, responses):
        ScriptedMediaHandler.responses = list(responses)
        ScriptedMediaHandler.requests = []
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ScriptedMediaHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, f"http://127.0.0.1:{server.server_port}/media"

    def test_course_id_accepts_only_positive_numeric_values(self):
        self.assertEqual(parse_course_id("ss360"), 360)
        self.assertEqual(parse_course_id("360"), 360)
        with self.assertRaises(DownloadError):
            parse_course_id("../../360")

    def test_course_url_extracts_the_cheese_id_and_rejects_other_urls(self):
        self.assertEqual(
            parse_course_url(
                "https://www.bilibili.com/cheese/play/ss929131509?csource=common_myclass_purchasedlecture_null"
            ),
            929131509,
        )
        with self.assertRaises(DownloadError):
            parse_course_url("https://www.bilibili.com/video/BV1M4D7BxEJz/")
        with self.assertRaises(DownloadError):
            parse_course_url("http://www.bilibili.com/cheese/play/ss929131509")
        with self.assertRaises(DownloadError):
            parse_course_url("https://example.com/cheese/play/ss929131509")

    def test_episode_selection_is_explicit_and_bounded(self):
        self.assertEqual(parse_episode_selection("1,3-5", 5), [1, 3, 4, 5])
        with self.assertRaises(DownloadError):
            parse_episode_selection("", 5)
        with self.assertRaises(DownloadError):
            parse_episode_selection("6", 5)

    def test_qr_ui_is_opt_in_and_statuses_are_user_facing(self):
        common = ["--course-id", "360", "--output", "E:\\CourseSources", "--episodes", "1"]
        terminal_args = build_parser().parse_args(common)
        self.assertFalse(terminal_args.qr_ui)

        ui_args = build_parser().parse_args(common + ["--qr-ui"])
        self.assertTrue(ui_args.qr_ui)
        self.assertEqual(qr_event_status("SCAN")[0], "请使用 B 站 App 扫码")
        self.assertEqual(qr_event_status("CONF")[0], "已扫码，等待确认")
        self.assertEqual(qr_event_status("TIMEOUT")[0], "二维码已过期，正在刷新")
        self.assertEqual(qr_event_status("DONE")[0], "登录成功，正在继续下载")

    def test_qr_login_panel_flow_handles_timeout_scan_confirm_and_done(self):
        class FakeQr:
            def __init__(self):
                self.events = [
                    SimpleNamespace(name="TIMEOUT"),
                    SimpleNamespace(name="SCAN"),
                    SimpleNamespace(name="CONF"),
                    SimpleNamespace(name="DONE"),
                ]
                self.generated = 0
                self.done = False

            async def generate_qrcode(self):
                self.generated += 1

            def get_qrcode_picture(self):
                return SimpleNamespace(content=f"qr-{self.generated}".encode())

            def has_done(self):
                return self.done

            async def check_state(self):
                event = self.events.pop(0)
                if event.name == "DONE":
                    self.done = True
                return event

            def get_credential(self):
                return "credential-in-memory"

        class FakeUi:
            cancelled = False

            def __init__(self):
                self.qr_payloads = []
                self.statuses = []

            def post_qr(self, content):
                self.qr_payloads.append(content)

            def post_status(self, text, color="#00AEEC"):
                self.statuses.append(text)

            def post_qr_event(self, event):
                self.statuses.append(qr_event_status(event)[0])

        ui = FakeUi()
        credential = asyncio.run(wait_for_qr_login(FakeQr(), ui, poll_interval=0))
        self.assertEqual(credential, "credential-in-memory")
        self.assertEqual(len(ui.qr_payloads), 2)
        self.assertIn("已扫码，等待确认", ui.statuses)
        self.assertIn("登录成功，正在继续下载", ui.statuses)

    def test_metadata_never_becomes_a_path(self):
        name = sanitize_component('../CON<>:"/\\|?*  ')
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)
        self.assertNotEqual(name.upper(), "CON")

    def test_ffmpeg_is_an_argument_list_not_a_shell_string(self):
        command = build_ffmpeg_command(
            Path("video&name.m4s"), Path("audio.m4s"), Path("out.mp4"), executable="ffmpeg"
        )
        self.assertIsInstance(command, list)
        self.assertEqual(command[0], "ffmpeg")
        self.assertIn("video&name.m4s", command)

    def test_output_root_cannot_be_the_program_directory(self):
        with self.assertRaises(DownloadError):
            require_safe_output_root(str(Path.cwd()))
        with self.assertRaises(DownloadError):
            require_safe_output_root(str(ROOT))

    def test_manifest_write_is_valid_json_and_file_hash_is_stable(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            media = directory / "sample.bin"
            media.write_bytes(b"course-data")
            self.assertEqual(sha256_file(media), hashlib.sha256(b"course-data").hexdigest())
            manifest = directory / "course-manifest.json"
            write_json_atomically(manifest, {"episode": 1})
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8")), {"episode": 1})
            with self.assertRaises(DownloadError):
                write_json_atomically(manifest, {"episode": 2})
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8")), {"episode": 1})

    def test_incremental_manifest_merges_records_and_verified_media_can_be_skipped(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            media_one = directory / "001_first.mp4"
            media_two = directory / "002_second.mp4"
            media_one.write_bytes(b"first-media")
            media_two.write_bytes(b"second-media")
            record_one = DownloadRecord(
                1,
                "第一集",
                media_one.name,
                media_one.stat().st_size,
                sha256_file(media_one),
            )
            record_two = DownloadRecord(
                2,
                "第二集",
                media_two.name,
                media_two.stat().st_size,
                sha256_file(media_two),
            )
            manifest = directory / "course-manifest.json"
            update_course_manifest_atomically(
                manifest,
                course_id=360,
                course_title="测试课程",
                records=[record_one],
            )
            update_course_manifest_atomically(
                manifest,
                course_id=360,
                course_title="测试课程",
                records=[record_one, record_two],
            )

            records = load_course_manifest(
                manifest,
                course_id=360,
                course_title="测试课程",
            )
            self.assertEqual(list(records), [1, 2])
            verified = verify_existing_record(
                directory,
                records[1],
                expected_filename=media_one.name,
                expected_title="第一集",
            )
            self.assertEqual(verified, media_one)

    def test_manifest_conflicts_stop_without_overwriting_existing_media(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            media = directory / "001_first.mp4"
            media.write_bytes(b"original-media")
            record = DownloadRecord(
                1,
                "第一集",
                media.name,
                media.stat().st_size + 1,
                "0" * 64,
            )
            with self.assertRaises(DownloadError) as context:
                verify_existing_record(directory, record)
            self.assertIn("大小与清单不一致", str(context.exception))
            self.assertEqual(media.read_bytes(), b"original-media")

    def test_existing_mp4_without_manifest_record_is_a_conflict(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            media = directory / "001_first.mp4"
            media.write_bytes(b"existing-media")
            manifest = directory / "course-manifest.json"
            update_course_manifest_atomically(
                manifest,
                course_id=360,
                course_title="测试课程",
                records=[],
            )
            records = load_course_manifest(
                manifest,
                course_id=360,
                course_title="测试课程",
            )
            self.assertNotIn(1, records)
            self.assertTrue(media.exists())

    def test_manifest_course_identity_and_duplicate_records_are_rejected(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            manifest = Path(raw_directory) / "course-manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "course_id": 361,
                        "course_title": "测试课程",
                        "episodes": [],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(DownloadError):
                load_course_manifest(manifest, course_id=360, course_title="测试课程")

            manifest.write_text(
                json.dumps(
                    {
                        "course_id": 360,
                        "course_title": "测试课程",
                        "episodes": [
                            {
                                "episode": 1,
                                "title": "第一集",
                                "filename": "001_first.mp4",
                                "bytes": 1,
                                "sha256": "0" * 64,
                            },
                            {
                                "episode": 1,
                                "title": "第一集",
                                "filename": "001_first.mp4",
                                "bytes": 1,
                                "sha256": "0" * 64,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(DownloadError):
                load_course_manifest(manifest, course_id=360, course_title="测试课程")

    def test_existing_final_media_is_never_deleted_or_overwritten(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            final = directory / "existing.mp4"
            final.write_bytes(b"original")
            with self.assertRaises(DownloadError):
                merge_streams(directory / "video.m4s", directory / "audio.m4s", final)
            self.assertEqual(final.read_bytes(), b"original")

    def test_download_is_exact_and_refuses_unsafe_resume(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            source = directory / "source.bin"
            source.write_bytes(b"course-data")
            handler = lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(
                *args, directory=str(directory), **kwargs
            )
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/source.bin"
                target = directory / "target.part"
                self.assertEqual(asyncio.run(download_stream(url, target)), len(b"course-data"))
                self.assertEqual(target.read_bytes(), b"course-data")

                partial = directory / "partial.part"
                partial.write_bytes(b"cour")
                with self.assertRaises(DownloadError):
                    asyncio.run(download_stream(url, partial))
                self.assertEqual(partial.read_bytes(), b"cour")
            finally:
                server.shutdown()
                server.server_close()

    def test_media_download_uses_bilibili_browser_headers(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            server, url = self._start_scripted_server([(200, b"course-data")])
            try:
                target = directory / "target.part"
                self.assertEqual(asyncio.run(download_stream(url, target)), len(b"course-data"))
                self.assertEqual(ScriptedMediaHandler.requests[0]["user-agent"], BILIBILI_MEDIA_HEADERS["User-Agent"])
                self.assertEqual(ScriptedMediaHandler.requests[0]["referer"], BILIBILI_MEDIA_HEADERS["Referer"])
            finally:
                server.shutdown()
                server.server_close()

    def test_forbidden_refreshes_signed_url_and_succeeds_on_second_attempt(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            server, url = self._start_scripted_server([(403, b"blocked"), (200, b"course-data")])
            provider_calls = 0

            async def provider():
                nonlocal provider_calls
                provider_calls += 1
                return url

            try:
                target = directory / "target.part"
                self.assertEqual(
                    asyncio.run(
                        download_stream_with_retries(
                            provider,
                            target,
                            stream_label="视频流",
                            retry_delays=(0, 0),
                        )
                    ),
                    len(b"course-data"),
                )
                self.assertEqual(provider_calls, 2)
                self.assertEqual(target.read_bytes(), b"course-data")
            finally:
                server.shutdown()
                server.server_close()

    def test_forbidden_stops_after_three_attempts_without_creating_partial(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            server, url = self._start_scripted_server([(403, b"blocked")] * 3)
            provider_calls = 0

            async def provider():
                nonlocal provider_calls
                provider_calls += 1
                return url

            try:
                target = directory / "target.part"
                with self.assertRaises(DownloadError) as context:
                    asyncio.run(
                        download_stream_with_retries(
                            provider,
                            target,
                            stream_label="视频流",
                            retry_delays=(0, 0),
                        )
                    )
                self.assertEqual(provider_calls, 3)
                self.assertIn("连续 3 次收到 HTTP 403", str(context.exception))
                self.assertIn("尚未创建临时文件", str(context.exception))
                self.assertFalse(target.exists())
            finally:
                server.shutdown()
                server.server_close()

    def test_forbidden_preserves_existing_partial_and_non_forbidden_does_not_retry(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            server, url = self._start_scripted_server([(403, b"blocked")] * 3)
            provider_calls = 0

            async def provider():
                nonlocal provider_calls
                provider_calls += 1
                return url

            try:
                target = directory / "partial.part"
                target.write_bytes(b"original")
                with self.assertRaises(DownloadError) as context:
                    asyncio.run(
                        download_stream_with_retries(
                            provider,
                            target,
                            stream_label="音频流",
                            retry_delays=(0, 0),
                        )
                    )
                self.assertEqual(provider_calls, 3)
                self.assertIn("已保留临时文件", str(context.exception))
                self.assertEqual(target.read_bytes(), b"original")
            finally:
                server.shutdown()
                server.server_close()

        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            server, url = self._start_scripted_server([(404, b"missing")])
            provider_calls = 0

            async def non_forbidden_provider():
                nonlocal provider_calls
                provider_calls += 1
                return url

            try:
                with self.assertRaises(DownloadError) as context:
                    asyncio.run(
                        download_stream_with_retries(
                            non_forbidden_provider,
                            directory / "missing.part",
                            stream_label="视频流",
                            retry_delays=(0, 0),
                        )
                    )
                self.assertEqual(provider_calls, 1)
                self.assertIn("HTTP 404", str(context.exception))
            finally:
                server.shutdown()
                server.server_close()

    def test_audio_retry_does_not_recall_successful_video_provider(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            server, url = self._start_scripted_server(
                [(200, b"video-data"), (403, b"blocked"), (200, b"audio-data")]
            )
            video_calls = 0
            audio_calls = 0

            async def video_provider():
                nonlocal video_calls
                video_calls += 1
                return url

            async def audio_provider():
                nonlocal audio_calls
                audio_calls += 1
                return url

            try:
                asyncio.run(
                    download_stream_with_retries(
                        video_provider,
                        directory / "video.part",
                        stream_label="视频流",
                        retry_delays=(0, 0),
                    )
                )
                asyncio.run(
                    download_stream_with_retries(
                        audio_provider,
                        directory / "audio.part",
                        stream_label="音频流",
                        retry_delays=(0, 0),
                    )
                )
                self.assertEqual(video_calls, 1)
                self.assertEqual(audio_calls, 2)
                self.assertEqual((directory / "video.part").read_bytes(), b"video-data")
                self.assertEqual((directory / "audio.part").read_bytes(), b"audio-data")
            finally:
                server.shutdown()
                server.server_close()

    def test_ytdlp_target_url_accepts_course_and_episode_urls(self):
        course = ytdlp_course.parse_target_url(
            "https://www.bilibili.com/cheese/play/ss929131509?from=test"
        )
        episode = ytdlp_course.parse_target_url(
            "https://www.bilibili.com/cheese/play/ep2519879"
        )
        self.assertEqual((course.kind, course.identifier), ("course", 929131509))
        self.assertEqual((episode.kind, episode.identifier), ("episode", 2519879))

        with self.assertRaises(DownloadError):
            ytdlp_course.parse_target_url("https://example.com/cheese/play/ep2519879")

    def test_ytdlp_backend_only_allows_firefox_and_uses_real_episode_url(self):
        episode = ytdlp_course.Episode(2, 2519879, "第二集", 143.0)
        command = ytdlp_course.build_download_command(episode, Path("E:/CourseSources"), "firefox")
        self.assertIn("--cookies-from-browser", command)
        self.assertIn("firefox", command)
        self.assertIn("--no-overwrites", command)
        self.assertIn("--merge-output-format", command)
        self.assertIn("mp4", command)
        self.assertTrue(command[-1].endswith("/ep2519879") or command[-1].endswith("\\ep2519879"))
        self.assertNotIn("--cookies", command)

        with self.assertRaises(DownloadError):
            ytdlp_course.build_ytdlp_base_command("chrome")

    def test_ytdlp_requires_explicit_output_confirmation(self):
        args = ytdlp_course.build_parser().parse_args(
            [
                "--url",
                "https://www.bilibili.com/cheese/play/ep2519879",
                "--output",
                "E:/CourseSources",
                "--confirm-personal-use",
            ]
        )
        with self.assertRaises(DownloadError) as context:
            ytdlp_course.run(args)
        self.assertIn("确认实际下载路径", str(context.exception))

    def test_ytdlp_print_default_output_is_read_only_and_sanitized(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            workspace = Path(raw_directory)
            target = ytdlp_course.Target("course", 929131509, "https://www.bilibili.com/cheese/play/ss929131509")
            episodes = [ytdlp_course.Episode(1, 2519879, "第一集", 143.0)]
            args = ytdlp_course.build_parser().parse_args(
                [
                    "--url",
                    target.url,
                    "--workspace-root",
                    str(workspace),
                    "--print-default-output",
                    "--confirm-personal-use",
                ]
            )
            output = io.StringIO()
            with patch.object(ytdlp_course, "enumerate_course", return_value=("课程:/第一集?", episodes)):
                with redirect_stdout(output):
                    ytdlp_course.run(args)
            self.assertEqual(output.getvalue().strip(), str(workspace / "课程__第一集_"))
            self.assertFalse((workspace / "课程__第一集_").exists())

    def test_ytdlp_default_output_rejects_nonempty_title_collision(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            workspace = Path(raw_directory)
            collision = workspace / "同名课程"
            collision.mkdir()
            (collision / "existing.txt").write_text("keep", encoding="utf-8")
            with self.assertRaises(DownloadError):
                ytdlp_course.ensure_default_output_is_safe(collision)

    def test_ytdlp_manifest_reuses_only_matching_media(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            episode = ytdlp_course.Episode(1, 2519879, "第一集", 143.0)
            filename = ytdlp_course.expected_filename(episode)
            media = directory / filename
            media.write_bytes(b"verified-media")
            record = ytdlp_course.ManifestRecord(
                episode=1,
                episode_id=2519879,
                title="第一集",
                filename=filename,
                bytes=media.stat().st_size,
                sha256=sha256_file(media),
                duration=143.0,
            )
            target = ytdlp_course.Target("episode", 2519879, episode.url)
            manifest = directory / "course-manifest.json"
            ytdlp_course.write_manifest(manifest, target, "第一集", {1: record})
            ytdlp_course.write_manifest(manifest, target, "第一集", {1: record})
            loaded = ytdlp_course.load_manifest(manifest, target, "第一集")
            self.assertEqual(ytdlp_course.verify_record(directory, loaded[1], episode), media)

            media.write_bytes(b"tampered-media")
            with self.assertRaises(DownloadError):
                ytdlp_course.verify_record(directory, loaded[1], episode)

    def test_ytdlp_metadata_command_uses_flat_playlist_for_courses(self):
        command = ytdlp_course._metadata_command(
            "https://www.bilibili.com/cheese/play/ss929131509",
            "firefox",
            playlist=True,
        )
        self.assertIn("--flat-playlist", command)
        self.assertIn("--yes-playlist", command)
        self.assertNotIn("--no-playlist", command)


if __name__ == "__main__":
    unittest.main()
