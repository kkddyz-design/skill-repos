---
name: model-fetch-kkddyz
description: Download and verify explicitly specified public AI model repositories or files from ModelScope, HF-Mirror, or Hugging Face. Use when the user has already confirmed an exact repository, optional file list, and destination type, and wants reliable local model download, resume, source fallback, or integrity verification on Windows.
---

# Model Fetch (kkddyz)

Use this skill only after the user has confirmed the exact model repository and, for partial downloads, exact filenames. Do not recommend or select models.

Run `scripts/model_fetch.py` with one of these operations:

```powershell
python scripts/model_fetch.py fetch --repo <publisher/repository> --target folder --resume
python scripts/model_fetch.py fetch --repo <publisher/repository> --files <file1,file2> --target lmstudio --resume
python scripts/model_fetch.py verify --repo <publisher/repository> --target folder
```

Use `--output <root>` only when the user gives a custom root. Defaults are `E:\AIModels` for `folder` and `E:\lmstudio-models\models` for `lmstudio`.

The script tries ModelScope, HF-Mirror, then Hugging Face. Report the successful source and final path. It creates an isolated runtime under the current user's local application data; never install dependencies into the system Python.

Do not delete incomplete or invalid target directories automatically. Stop and report the conflict. Use `--clean-failed` only after the user explicitly authorizes removal of that exact target.
