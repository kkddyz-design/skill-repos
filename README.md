# kkddyz Skill Repositories

这是个人维护的 Codex Skill 单一源 Git 仓库。仓库中的 Skill 源码由 `C:\Users\kkddyz\.codex\skills\` 下的 Windows junction 作为运行入口；日常只需要修改本仓库，Codex 会直接读取同一份内容。

## 当前 Skill

### `bilibili-cheese-downloader-kkddyz`

用于在用户已获授权且确认个人使用范围后，获取 Bilibili Cheese 课程或单集视频素材。

- 读取 Firefox 会话，通过 yt-dlp 下载视频和音频。
- 使用 FFmpeg 合并 MP4，支持安全续传和课程清单校验。
- 首次使用时在用户确认的安装目录初始化隔离运行时。
- 不导出或保存 Cookie，不提交视频、课程清单、FFmpeg 或虚拟环境。
- 不负责转录、ASR、课程分析或 Markdown 笔记生成。

入口文件：[`bilibili-cheese-downloader-kkddyz/SKILL.md`](bilibili-cheese-downloader-kkddyz/SKILL.md)

### `model-fetch-kkddyz`

用于下载和校验用户已经明确指定的公开 AI 模型仓库或文件。

- 支持 ModelScope、HF-Mirror 和 Hugging Face，并按顺序回退来源。
- 支持完整仓库或明确指定文件，支持断点续传。
- 支持 `folder` 和 `lmstudio` 两种目标类型，并校验零字节文件、未完成文件和 Git LFS 指针。
- 在当前用户的本地应用数据目录创建隔离运行时，不污染系统 Python。
- 不替用户推荐模型；必须先确认准确的模型仓库和文件范围。

入口文件：[`model-fetch-kkddyz/SKILL.md`](model-fetch-kkddyz/SKILL.md)

## 目录约定

```text
skill-repos/
├─ .git/
├─ README.md
├─ bilibili-cheese-downloader-kkddyz/
│  ├─ SKILL.md
│  ├─ agents/openai.yaml
│  ├─ scripts/
│  ├─ references/
│  └─ tests/
└─ model-fetch-kkddyz/
   ├─ SKILL.md
   ├─ agents/openai.yaml
   └─ scripts/model_fetch.py
```

`SKILL.md` 描述 Skill 的触发条件和操作边界，`agents/openai.yaml` 提供界面元数据，`scripts/` 保存可执行脚本。运行时、缓存、模型、视频、Cookie 和临时文件不属于源码仓库。

## 更新流程

1. 直接修改对应的 Skill 子目录。
2. 运行该 Skill 可用的语法、结构和测试检查。
3. 确认 `git status` 只包含预期文件。
4. 仅暂存对应 Skill 或文档变更并提交：

   ```powershell
   git add README.md <skill-directory>
   git commit -m "<description>"
   git push
   ```

5. 由于 `C:\Users\kkddyz\.codex\skills\` 使用 junction，运行中的 Skill 会自动读取仓库中的最新源码，不需要复制同步。

不要使用强制推送，也不要把 `.venv`、`.tools`、模型权重、视频、Cookie、课程清单或下载临时文件加入提交。
