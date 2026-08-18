---
name: bilibili-cheese-downloader-kkddyz
description: This skill should be used when an authorized user asks to obtain Bilibili Cheese classroom video materials from an ss... course URL or an ep... episode URL. It confirms separate installation and download paths, bootstraps a local runtime in the confirmed installation directory, reads the user's Firefox session through yt-dlp, downloads and merges MP4 media, resumes safely, and verifies the course manifest. It does not transcribe, analyze, or create Markdown course notes.
---

# Bilibili Cheese Downloader

## Purpose and boundary

Use this Skill only for authorized local acquisition of Bilibili Cheese classroom video materials:

- Accept Bilibili Cheese `ss...` course URLs and `ep...` episode URLs.
- Support one episode, an explicit episode selection, or an explicit full-course download.
- Read the user's Firefox session through yt-dlp without exporting cookies.
- Merge video and audio into MP4 with FFmpeg, resume interrupted downloads, and verify each completed file.
- Do not transcribe, call ASR or alignment models, analyze lessons, or generate Markdown.

Do not infer authorization to save content. Confirm course access and personal-use scope before any metadata or media request.

## Required first-use workflow

The Skill package contains source templates and setup logic, but never contains `.venv`, yt-dlp, FFmpeg binaries, cookies, course manifests, or videos. Initialize a local runtime only during an actual Skill invocation and only after the installation path and personal-use authorization are confirmed. Metadata lookup needs that runtime, but it never creates a course output directory or downloads media.

1. Read the current working directory. Propose the installation directory:

   ```text
   <current working directory>\bilibili-cheese-downloader-kkddyz
   ```

2. Ask the user to confirm or change the installation directory. Do not create it before confirmation.
3. Validate the supplied URL and identify whether it is a course (`ss...`) or episode (`ep...`).
4. Confirm that the user has access to the course and that the copy is for personal use.
5. Ask the user to log in to Bilibili in Firefox and close Firefox completely if its cookie database is locked.
6. Initialize the confirmed installation directory with the Skill's `scripts\setup-runtime.ps1`.
7. Locate the initialized directory with `scripts\resolve-project.ps1 -ProjectRoot "$confirmedInstallRoot"`.
8. Ask the downloader for the read-only default output path. For a course this is:

   ```text
   <current working directory>\<sanitized course title>
   ```

   Use `--workspace-root` to make the root explicit. Ask the user to confirm or change this separate download directory. Do not create it before confirmation.
9. Run the download invocation only after the output path is confirmed.

If the installation path or personal-use authorization is not confirmed, stop without creating a runtime. If the download path is not confirmed, stop after read-only metadata resolution without creating an output directory or downloading media.

## Runtime initialization

The confirmed installation directory contains the source, `.venv`, and `.tools` together:

```text
bilibili-cheese-downloader-kkddyz\
├─ safe_course_downloader.py
├─ yt_dlp_course_downloader.py
├─ requirements.txt
├─ requirements.lock
├─ .venv\
├─ .tools\ffmpeg\bin\
│  ├─ ffmpeg.exe
│  ├─ ffprobe.exe
│  └─ ffplay.exe
└─ .runtime-manifest.json
```

Run setup only with the user-confirmed installation path after personal-use authorization:

```powershell
& "$skillRoot\scripts\setup-runtime.ps1" `
  -SkillRoot "$skillRoot" `
  -InstallRoot "$confirmedInstallRoot"
```

The script prefers `F:\python\python.exe`, then `py -3.10`, then `python`; creates only the project-local virtual environment; installs `requirements.lock`; downloads the fixed FFmpeg archive from `runtime-lock.json`; verifies its SHA-256 before installation; and writes `.runtime-manifest.json`. It is repeatable and stops on source, runtime, or FFmpeg conflicts. It never deletes existing media, manifests, partial files, or source files.

If a sandbox blocks pip or FFmpeg network access, request explicit approval for the same setup command. Do not replace the lock file, bypass the FFmpeg hash check, or substitute an unverified runtime.

## Download invocation

After setup, call:

```powershell
& "$project\.venv\Scripts\python.exe" "$project\yt_dlp_course_downloader.py" `
  --url "$url" `
  --workspace-root "$workspaceRoot" `
  --browser firefox `
  --confirm-personal-use `
  --confirm-output-path `
  <selection>
```

Use exactly one selection for a course:

- `--episodes "1,3-5"` for selected episodes.
- `--all-episodes` for the complete course.

Do not pass either selection option for an `ep...` URL. The course URL is enumerated into its real episode URLs; the downloader does not reuse one URL for every episode.

For a user-confirmed default output directory, omit `--output` and retain `--workspace-root`. For a user-selected custom directory, replace `--workspace-root "$workspaceRoot"` with `--output "$confirmedOutput"`. Always retain `--confirm-output-path`.

To inspect the default output path without writing anything:

```powershell
& "$project\.venv\Scripts\python.exe" "$project\yt_dlp_course_downloader.py" `
  --url "$url" `
  --workspace-root "$workspaceRoot" `
  --print-default-output `
  --confirm-personal-use
```

This mode does not create directories, download media, write a manifest, or modify the runtime.

## Operational recovery notes

- Treat a terminal's garbled rendering of Chinese file names as a console-encoding issue, not an alternate output path. Confirm the actual Unicode title through `course-manifest.json` or a filesystem listing; never retype a garbled path.
- Keep the course manifest and all MP4s together. The downloader verifies filename, title, episode ID, byte count, and SHA-256 before resuming. Do not add a manifest entry by hand or overwrite an MP4 to bypass a mismatch.
- If a run stops after an MP4 is merged but before the manifest is updated, preserve the files and report the affected episode and error. Resume only after the manifest updater itself is repaired and the completed file is reconciled against current course metadata and a SHA-256 record.

## Environment variable

`BILIBILI_CHEESE_DOWNLOADER_ROOT` is optional. It must point to the already initialized unified installation directory containing the source, `.venv`, and `.tools`; it must not point to a course output directory:

```powershell
$env:BILIBILI_CHEESE_DOWNLOADER_ROOT = 'D:\Bilibili\bilibili-cheese-downloader-kkddyz'
```

After changing a persistent environment variable, open a new terminal or Codex task. Without it, use the current workspace's default installation directory.

## Session, safety, and resume rules

- Use yt-dlp's `--cookies-from-browser firefox` only. Never export, print, persist, or upload cookies or session values.
- Do not attempt Chrome or Edge DPAPI decryption as a workaround.
- Use `--continue` and `--no-overwrites`; keep resumable temporary files after failure.
- Merge with FFmpeg stream copy and atomically update `course-manifest.json` after each successful episode.
- Skip an MP4 only when its manifest record, filename, title, episode ID, size, and SHA-256 all match.
- Stop on an existing unverified MP4, an output title collision, or a manifest mismatch. Do not delete or overwrite files automatically.
- Stop on the first failed episode and report the exact episode, output directory, and backend error.

## Handoff

After a successful run, report the confirmed output directory, completed and skipped episode counts, manifest path, MP4 count, and verification status. For a later verification-only request, use `--verify` with the same output path and selection; do not redownload verified files.

This Skill is intentionally limited to video acquisition. Course transcription, model execution, lesson analysis, and Markdown generation are separate workflows.
