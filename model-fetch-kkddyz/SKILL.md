---
name: model-fetch-kkddyz
description: Download and verify explicitly specified public AI model repositories or files from ModelScope, HF-Mirror, or Hugging Face. Use when the user has already confirmed an exact repository, optional file list, and destination type, and wants reliable local model download, resume, source fallback, or integrity verification on Windows.
---

# Model Fetch (kkddyz)

Use this skill only after the user has confirmed the exact model repository and, for partial downloads, exact filenames. Do not recommend or select models.

Run `scripts/model_fetch.py` with one of these operations:

```powershell
python scripts/model_fetch.py fetch --repo <publisher/repository> --target lmstudio --resume
python scripts/model_fetch.py fetch --repo <publisher/repository> --files <exact-file.gguf> --target lmstudio --resume
python scripts/model_fetch.py fetch --repo <publisher/repository> --target lmstudio --resume --no-progress
python scripts/model_fetch.py verify --repo <publisher/repository> --target lmstudio
```

Use `--output <root>` only when the user gives a custom root. The default target is `lmstudio`, stored under `E:\lmstudio-models\models`; use `--target folder` explicitly for `E:\AIModels`.

For repositories whose names include `GGUF` or that contain several quantization variants, do not download the complete repository by default. Ask the user to confirm the exact filename (for example, one `Q4_K_M`, `Q5_K_M`, `Q6_K`, or `Q8_0` file), then pass it through `--files`. Omit `--files` only when the user explicitly authorizes downloading the entire repository. `BF16` files are not quantized GGUF variants and must be treated as separate files.

The script tries ModelScope, HF-Mirror, then Hugging Face. During `fetch`, it renders one aggregate progress bar to stderr in an interactive terminal with percentage, transferred size, speed, and ETA. Redirected or CI output uses throttled progress lines without carriage-return control characters. Vendor-native progress bars are disabled before SDK import so they do not interleave with the aggregate bar. Use `--no-progress` when progress output is not wanted. Report the successful source and final path. It creates an isolated runtime under the current user's local application data; never install dependencies into the system Python.

Progress is produced by the downloader itself; do not substitute manual assistant-only progress estimates. A failed source clears its current progress line, preserves its source-specific staging data for resume, and then the next source is attempted.

## CDP browser fallback

Use the CDP fallback only after the scripted sources fail because the command-line runtime cannot transfer the confirmed Hugging Face file. Do not use it for an invalid repository or filename, and never use it to download an entire multi-quantization repository.

- Use the `agent-browser` skill with a dedicated named session; do not attach to an unrelated application's CDP port or reuse another task's browser tab.
- Open the Hugging Face `blob/main/<exact-file>` page for the confirmed repository, click its `Download` control, then inspect `chrome://downloads/` for the exact filename, byte progress, speed, ETA, completion state, and browser-reported local path.
- Report browser progress using the download manager's actual values. Keep the same chat milestones and low-progress warning rules; do not estimate values from elapsed time or file size.
- Wait for the browser state to be complete before copying anything. If the download fails, pauses indefinitely, or the filename differs, stop and report the condition; retain the browser partial file and scripted source staging data.
- After completion, copy (do not overwrite) the exact file into `E:\lmstudio-models\models\<publisher>\<repository>\`. If that target exists or is invalid, stop and request explicit cleanup authorization rather than replacing it.
- Run the normal `verify` operation with the confirmed `--files` value. Report the source as `huggingface-cdp`, the browser download path, final path, and verification result. Browser fallback does not provide ModelScope/HF-Mirror source fallback or the script's atomic staging promotion.

## User-facing progress reports

Terminal rendering and chat reporting are separate responsibilities. The downloader owns the progress data; the assistant must relay milestone events in chat while monitoring the tool output.

During a download, follow this sequence and use the latest renderer output as the only source for user-facing progress updates:

- At start, report the exact file or files, total size when known, destination root, and current source.
- After every download-tool poll returns new output, inspect the newly returned progress lines before deciding whether to send a chat update. Do not wait until completion to summarize progress.
- If a source attempt is still below 10% after 60 seconds, report one low-progress warning with the current transferred size, speed, and ETA. Reset this timer when trying another source.
- Report once when progress crosses 10%, 25%, 50%, 75%, 90%, and 100%.
- If one tool result contains several newly crossed milestones, report each missing milestone once in ascending order; do not collapse them into only the final percentage.
- When a source fails, report the failure, the next source, and that source-specific staging data is retained for resume.
- Keep a per-download set of reported milestones and a per-source low-progress timer so repeated polling cannot duplicate messages. Reset both appropriately when a new source is attempted.
- Do not merely expose the terminal progress output without a corresponding chat update, do not send an update for every polling chunk, and do not invent or manually recalculate percentage, speed, or ETA.

On success, report the verified source, final path, and integrity result. Preserve the PowerShell dynamic bar, non-TTY throttled logs, and `--no-progress` behavior.

Do not delete incomplete or invalid target directories automatically. Stop and report the conflict. Use `--clean-failed` only after the user explicitly authorizes removal of that exact target.
