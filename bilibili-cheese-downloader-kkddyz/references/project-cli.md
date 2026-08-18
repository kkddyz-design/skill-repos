# Project CLI reference

The Skill invokes the initialized runtime directory selected by the user. It does not use the Skill package as a hidden download directory.

## Initialize

```powershell
& "$skillRoot\scripts\setup-runtime.ps1" `
  -SkillRoot "$skillRoot" `
  -InstallRoot "$confirmedInstallRoot"
```

Resolve it afterward:

```powershell
$project = & "$skillRoot\scripts\resolve-project.ps1" `
  -WorkspaceRoot "$workspaceRoot" `
  -ProjectRoot "$confirmedInstallRoot"
```

`BILIBILI_CHEESE_DOWNLOADER_ROOT` takes precedence over the workspace default when set. It must point to a directory containing source files, `.venv`, and `.tools\ffmpeg\bin\ffmpeg.exe`.

## Read-only default output resolution

This command obtains the course title and prints the path without creating directories or writing a manifest:

```powershell
& "$project\.venv\Scripts\python.exe" "$project\yt_dlp_course_downloader.py" `
  --url "https://www.bilibili.com/cheese/play/ss929131509" `
  --workspace-root "$workspaceRoot" `
  --print-default-output `
  --confirm-personal-use
```

## Download a selected course

```powershell
& "$project\.venv\Scripts\python.exe" "$project\yt_dlp_course_downloader.py" `
  --url "https://www.bilibili.com/cheese/play/ss929131509" `
  --episodes "1,3-5" `
  --output "$confirmedOutput" `
  --browser firefox `
  --confirm-personal-use `
  --confirm-output-path
```

## Download a complete course

```powershell
& "$project\.venv\Scripts\python.exe" "$project\yt_dlp_course_downloader.py" `
  --url "https://www.bilibili.com/cheese/play/ss929131509" `
  --all-episodes `
  --output "$confirmedOutput" `
  --browser firefox `
  --confirm-personal-use `
  --confirm-output-path
```

## Download one episode

```powershell
& "$project\.venv\Scripts\python.exe" "$project\yt_dlp_course_downloader.py" `
  --url "https://www.bilibili.com/cheese/play/ep2519879" `
  --output "$confirmedOutput" `
  --browser firefox `
  --confirm-personal-use `
  --confirm-output-path
```

## Verify existing files

Use the same URL, selection, and confirmed output directory:

```powershell
& "$project\.venv\Scripts\python.exe" "$project\yt_dlp_course_downloader.py" `
  --url "https://www.bilibili.com/cheese/play/ss929131509" `
  --all-episodes `
  --output "$confirmedOutput" `
  --browser firefox `
  --verify `
  --confirm-personal-use `
  --confirm-output-path
```

All commands use Firefox's in-memory cookie reader through yt-dlp. Cookie values and signed media URLs are not arguments, files, or manifest fields.
