[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SkillRoot,

    [Parameter(Mandatory = $true)]
    [string]$InstallRoot
)

$ErrorActionPreference = 'Stop'

function Stop-Setup([string]$Message) {
    throw "运行环境初始化失败：$Message"
}

function Get-AbsolutePath([string]$PathText) {
    if (-not $PathText) {
        Stop-Setup '路径不能为空。'
    }
    return [System.IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($PathText))
}

function Get-FileSha256([string]$PathText) {
    return (Get-FileHash -LiteralPath $PathText -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Copy-TemplateSafely([string]$Source, [string]$Destination) {
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        Stop-Setup "Skill 模板缺失：$Source"
    }
    if (Test-Path -LiteralPath $Destination) {
        if (-not (Test-Path -LiteralPath $Destination -PathType Leaf)) {
            Stop-Setup "安装目录中的路径不是文件：$Destination"
        }
        if ((Get-FileSha256 $Source) -ne (Get-FileSha256 $Destination)) {
            Stop-Setup "安装目录中的源码或依赖文件与 Skill 模板冲突，不会覆盖：$Destination"
        }
        return
    }
    Copy-Item -LiteralPath $Source -Destination $Destination
}

function Get-PythonCommand {
    $fixedPython = 'F:\python\python.exe'
    if (Test-Path -LiteralPath $fixedPython -PathType Leaf) {
        return @($fixedPython)
    }

    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        try {
            & $launcher.Source -3.10 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 10) else 1)' 2>$null
            if ($LASTEXITCODE -eq 0) {
                return @($launcher.Source, '-3.10')
            }
        } catch {
            # Try the ordinary python command below.
        }
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        try {
            & $python.Source -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 10) else 1)' 2>$null
            if ($LASTEXITCODE -eq 0) {
                return @($python.Source)
            }
        } catch {
            # Report the uniform error below.
        }
    }
    Stop-Setup '未找到可用的 Python 3.10。已尝试 F:\python\python.exe、py -3.10 和 python。'
}

function Invoke-Checked([string]$Executable, [string[]]$Arguments, [string]$FailureMessage) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        Stop-Setup $FailureMessage
    }
}

$skill = Get-AbsolutePath $SkillRoot
$install = Get-AbsolutePath $InstallRoot
$runtimeLockPath = Join-Path $skill 'runtime-lock.json'
if (-not (Test-Path -LiteralPath $runtimeLockPath -PathType Leaf)) {
    Stop-Setup "runtime-lock.json 不存在：$runtimeLockPath"
}

if (Test-Path -LiteralPath $install -PathType Leaf) {
    Stop-Setup "安装路径已被文件占用：$install"
}
New-Item -ItemType Directory -Path $install -Force | Out-Null

$templates = @(
    'safe_course_downloader.py',
    'yt_dlp_course_downloader.py',
    'requirements.txt',
    'requirements.lock'
)
foreach ($name in $templates) {
    Copy-TemplateSafely (Join-Path $skill $name) (Join-Path $install $name)
}

$venv = Join-Path $install '.venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'
if (Test-Path -LiteralPath $venv -PathType Leaf) {
    Stop-Setup "虚拟环境路径已被文件占用：$venv"
}
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    # Capture command output as an array. A single executable path otherwise
    # becomes a scalar string, and indexing it returns only its first character.
    $pythonCommand = @(Get-PythonCommand)
    if ($pythonCommand.Count -eq 1) {
        Invoke-Checked $pythonCommand[0] @('-m', 'venv', $venv) '创建 Python 虚拟环境失败。'
    } else {
        $launcherArgs = @($pythonCommand[1..($pythonCommand.Count - 1)]) + @('-m', 'venv', $venv)
        Invoke-Checked $pythonCommand[0] $launcherArgs '创建 Python 虚拟环境失败。'
    }
}
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Stop-Setup "虚拟环境创建后未找到 Python：$venvPython"
}

$lockFile = Join-Path $install 'requirements.lock'
Invoke-Checked $venvPython @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', $lockFile) '安装锁定的 Python 依赖失败。'
Invoke-Checked $venvPython @('-m', 'pip', 'check') 'Python 依赖检查失败。'
$ytDlpVersion = (& $venvPython -c 'import yt_dlp; print(yt_dlp.version.__version__)' 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $ytDlpVersion) {
    Stop-Setup 'yt-dlp 安装后无法导入。'
}

$lock = Get-Content -LiteralPath $runtimeLockPath -Raw | ConvertFrom-Json
$ffmpegLock = $lock.ffmpeg
if (-not $ffmpegLock.url -or -not $ffmpegLock.sha256 -or $ffmpegLock.archive -ne 'zip') {
    Stop-Setup 'runtime-lock.json 中的 FFmpeg 锁定信息不完整。'
}

$ffmpegRoot = Join-Path $install '.tools\ffmpeg'
$ffmpegBin = Join-Path $ffmpegRoot 'bin'
$expectedExecutables = @('ffmpeg.exe', 'ffprobe.exe', 'ffplay.exe')
$present = @($expectedExecutables | Where-Object { Test-Path -LiteralPath (Join-Path $ffmpegBin $_) -PathType Leaf })
if ($present.Count -gt 0 -and $present.Count -lt $expectedExecutables.Count) {
    Stop-Setup "FFmpeg 目录不完整，不会覆盖已有文件：$ffmpegBin"
}

if ($present.Count -eq $expectedExecutables.Count) {
    $ffmpegVersionText = (& (Join-Path $ffmpegBin 'ffmpeg.exe') '-version' 2>&1 | Select-Object -First 1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $ffmpegVersionText -notmatch [regex]::Escape([string]$ffmpegLock.version)) {
        Stop-Setup "已有 FFmpeg 版本与锁定版本 $($ffmpegLock.version) 不一致：$ffmpegVersionText"
    }
} else {
    $tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("bilibili-ffmpeg-" + [guid]::NewGuid().ToString('N'))
    $archivePath = Join-Path $tempRoot 'ffmpeg.zip'
    $extractRoot = Join-Path $tempRoot 'extract'
    $stagingRoot = Join-Path $tempRoot 'ffmpeg'
    $stagingBin = Join-Path $stagingRoot 'bin'
    try {
        New-Item -ItemType Directory -Path $tempRoot, $extractRoot, $stagingBin -Force | Out-Null
        Invoke-WebRequest -UseBasicParsing -Uri ([string]$ffmpegLock.url) -OutFile $archivePath
        $actualArchiveHash = Get-FileSha256 $archivePath
        if ($actualArchiveHash -ne ([string]$ffmpegLock.sha256).ToLowerInvariant()) {
            Stop-Setup "FFmpeg 压缩包 SHA-256 不匹配；期望 $($ffmpegLock.sha256)，实际 $actualArchiveHash。"
        }
        Expand-Archive -LiteralPath $archivePath -DestinationPath $extractRoot -Force
        foreach ($executable in $expectedExecutables) {
            $found = Get-ChildItem -LiteralPath $extractRoot -Recurse -Filter $executable -File | Select-Object -First 1
            if (-not $found) {
                Stop-Setup "FFmpeg 压缩包缺少 $executable。"
            }
            Copy-Item -LiteralPath $found.FullName -Destination (Join-Path $stagingBin $executable)
        }
        if (Test-Path -LiteralPath $ffmpegRoot) {
            Stop-Setup "FFmpeg 目标目录在下载期间出现冲突，不会覆盖：$ffmpegRoot"
        }
        New-Item -ItemType Directory -Path (Join-Path $install '.tools') -Force | Out-Null
        Move-Item -LiteralPath $stagingRoot -Destination $ffmpegRoot
    } finally {
        if (Test-Path -LiteralPath $tempRoot) {
            Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

$runtimeManifest = [ordered]@{
    schema_version = 1
    python = ((& $venvPython '--version' 2>&1 | Out-String).Trim())
    requirements_lock_sha256 = Get-FileSha256 $lockFile
    yt_dlp_version = $ytDlpVersion
    ffmpeg_version = [string]$ffmpegLock.version
    ffmpeg_archive_sha256 = ([string]$ffmpegLock.sha256).ToLowerInvariant()
    ffmpeg_executable_sha256 = Get-FileSha256 (Join-Path $ffmpegBin 'ffmpeg.exe')
    initialized_at_utc = [DateTime]::UtcNow.ToString('o')
    source_version = 'bilibili-cheese-downloader-kkddyz'
}
$manifestPath = Join-Path $install '.runtime-manifest.json'
$temporaryManifest = "$manifestPath.tmp.$([guid]::NewGuid().ToString('N'))"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($temporaryManifest, ($runtimeManifest | ConvertTo-Json -Depth 4), $utf8NoBom)
Move-Item -LiteralPath $temporaryManifest -Destination $manifestPath -Force

[pscustomobject]@{
    install_root = $install
    python = $venvPython
    yt_dlp_version = $ytDlpVersion
    ffmpeg_version = [string]$ffmpegLock.version
    runtime_manifest = $manifestPath
} | ConvertTo-Json -Depth 3
