# bilibili-cheese-downloader-kkddyz

一个带运行时自举能力的 Codex Skill，用于在用户确认访问权限、安装目录和下载目录后，获取 B 站课堂视频素材。

Skill 包提供下载器源码模板、依赖锁文件和初始化脚本，但不打包 `.venv`、yt-dlp、FFmpeg、Cookie、视频或课程清单。首次实际调用时，源码、项目虚拟环境和 FFmpeg 会安装到用户确认的同一个目录。

## 默认目录

从工作区 `C:\Users\<用户名>\Documents\ChatGPT\小说拆解` 调用时，默认安装目录是：

```text
<当前工作目录>\bilibili-cheese-downloader-kkddyz
```

默认下载目录由 B 站课程标题生成：

```text
<当前工作目录>\<清理后的课程名称>
```

两个路径必须分别向用户展示并确认。也可以通过 `--output` 和安装目录参数使用自定义路径。

## 首次初始化

Skill 会在确认安装目录后执行：

```powershell
& "$skillRoot\scripts\setup-runtime.ps1" `
  -SkillRoot "$skillRoot" `
  -InstallRoot "$confirmedInstallRoot"
```

初始化脚本：

- 优先使用 `F:\python\python.exe`，其次是 `py -3.10` 和 `python`。
- 在安装目录创建 `.venv`，只向该虚拟环境安装 `requirements.lock`。
- 下载 `runtime-lock.json` 指定的固定版本 FFmpeg，先校验 SHA-256 再安装。
- 生成 `.runtime-manifest.json`。
- 已有有效环境时复用；发现源码、虚拟环境或 FFmpeg 冲突时停止。
- 不删除已有视频、清单、`.part` 文件或临时文件。

## 使用示例

课程下载：

```powershell
& "$project\.venv\Scripts\python.exe" "$project\yt_dlp_course_downloader.py" `
  --url "https://www.bilibili.com/cheese/play/ss929131509" `
  --all-episodes `
  --output "$confirmedOutput" `
  --browser firefox `
  --confirm-personal-use `
  --confirm-output-path
```

单集下载：

```powershell
& "$project\.venv\Scripts\python.exe" "$project\yt_dlp_course_downloader.py" `
  --url "https://www.bilibili.com/cheese/play/ep2519879" `
  --output "$confirmedOutput" `
  --browser firefox `
  --confirm-personal-use `
  --confirm-output-path
```

只解析默认下载路径，不写入任何文件：

```powershell
& "$project\.venv\Scripts\python.exe" "$project\yt_dlp_course_downloader.py" `
  --url "https://www.bilibili.com/cheese/play/ss929131509" `
  --workspace-root "$workspaceRoot" `
  --print-default-output `
  --confirm-personal-use
```

课程 URL 必须显式使用 `--episodes "1,3-5"` 或 `--all-episodes`；单集 URL 不使用这两个参数。课程清单会使用实际每集的 `ep...` 地址，视频和音频由 FFmpeg 合并为 MP4。

## 环境变量

可选环境变量 `BILIBILI_CHEESE_DOWNLOADER_ROOT` 用于指定已经初始化完成的统一安装目录：

```powershell
$env:BILIBILI_CHEESE_DOWNLOADER_ROOT = 'D:\Bilibili\bilibili-cheese-downloader-kkddyz'
```

它必须包含 `safe_course_downloader.py`、`yt_dlp_course_downloader.py`、`.venv` 和 `.tools\ffmpeg`，不能指向课程视频输出目录。修改持久环境变量后，需要重新打开终端或 Codex 任务。

## 隐私与边界

- 仅读取 Firefox 会话；不导出或保存 Cookie，不尝试解密 Chrome/Edge 会话。
- 保留可续传临时文件，不覆盖已有 MP4，不自动删除用户文件。
- 仅用于已授权且个人使用的本地素材获取。
- 不包含 ASR、强制对齐、课程分析、转录或 Markdown 文档生成。

## 目录结构

```text
bilibili-cheese-downloader-kkddyz/
├─ SKILL.md
├─ README.md
├─ safe_course_downloader.py
├─ yt_dlp_course_downloader.py
├─ requirements.txt
├─ requirements.lock
├─ runtime-lock.json
├─ scripts/
│  ├─ setup-runtime.ps1
│  └─ resolve-project.ps1
├─ references/
└─ tests/
```

运行环境（`.venv`、`.tools`、`.runtime-manifest.json`）应位于用户确认的安装目录，不应提交到 Skill 仓库或 ZIP 包。
