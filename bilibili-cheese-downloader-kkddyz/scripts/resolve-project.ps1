[CmdletBinding()]
param(
    [string]$WorkspaceRoot,
    [string]$ProjectRoot
)

$ErrorActionPreference = 'Stop'
$skillRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$candidates = [System.Collections.Generic.List[string]]::new()

if ($ProjectRoot) {
    $candidates.Add($ProjectRoot)
}

if ($env:BILIBILI_CHEESE_DOWNLOADER_ROOT) {
    $candidates.Add($env:BILIBILI_CHEESE_DOWNLOADER_ROOT)
}

if ($WorkspaceRoot) {
    $candidates.Add((Join-Path $WorkspaceRoot 'bilibili-cheese-downloader-kkddyz'))
}

# The Skill package may itself be the initialized runtime directory when a user
# explicitly chooses that location. Do not search or clone unrelated projects.
$candidates.Add($skillRoot)

$seen = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
foreach ($candidate in $candidates) {
    if (-not $candidate -or -not (Test-Path -LiteralPath $candidate -PathType Container)) {
        continue
    }
    $resolved = (Resolve-Path -LiteralPath $candidate).Path
    if (-not $seen.Add($resolved)) {
        continue
    }
    $entrypoint = Join-Path $resolved 'yt_dlp_course_downloader.py'
    $safeDownloader = Join-Path $resolved 'safe_course_downloader.py'
    $python = Join-Path $resolved '.venv\Scripts\python.exe'
    $ffmpeg = Join-Path $resolved '.tools\ffmpeg\bin\ffmpeg.exe'
    if (
        (Test-Path -LiteralPath $entrypoint -PathType Leaf) -and
        (Test-Path -LiteralPath $safeDownloader -PathType Leaf) -and
        (Test-Path -LiteralPath $python -PathType Leaf) -and
        (Test-Path -LiteralPath $ffmpeg -PathType Leaf)
    ) {
        Write-Output $resolved
        exit 0
    }
}

Write-Error '找不到已初始化的 bilibili-cheese-downloader-kkddyz 运行目录。请先确认安装目录并运行 scripts\setup-runtime.ps1，或设置 BILIBILI_CHEESE_DOWNLOADER_ROOT。'
exit 2
