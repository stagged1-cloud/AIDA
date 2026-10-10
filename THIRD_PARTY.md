# Third-party software and models

TARS itself is MIT-licensed (see `LICENSE`). It does **not** bundle any of the items below. The installer downloads
them to your PC from their public sources, and each stays under its own licence. Licences are listed from the
projects' public pages as best known at the time of writing; check the linked source before redistributing anything.

## Models (downloaded by `install.ps1`)

| What | Source | Licence |
|---|---|---|
| Kokoro v1.0 voice model (`kokoro-v1.0.onnx`, `voices-v1.0.bin`), ONNX build of Kokoro-82M | GitHub release `model-files-v1.0` of [thewh1teagle/kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) | Apache-2.0 (Kokoro-82M, hexgrad) |
| Whisper `small.en` speech recognition, CTranslate2 conversion | Hugging Face `Systran/faster-whisper-small.en`, fetched by `faster-whisper` | MIT (OpenAI Whisper) |

The installer verifies the Kokoro files by size and SHA-256 before using them.

## Python packages (installed with pip into `.venv`)

| Package | Licence |
|---|---|
| faster-whisper, ctranslate2 | MIT |
| kokoro-onnx | MIT |
| onnxruntime | MIT |
| sounddevice | MIT |
| keyboard | MIT |
| PyYAML | MIT |
| numpy, scipy | BSD-3-Clause |
| pywin32 | PSF-2.0 |
| phonemizer(-fork), espeakng-loader (pulled in by kokoro-onnx; wraps eSpeak NG) | GPL-3.0 family (eSpeak NG is GPL-3.0+). Installed as separate pip dependencies, not bundled or modified by TARS. If you redistribute a bundle that includes them, the GPL applies to that bundle. |
| nvidia-cublas-cu12, nvidia-cudnn-cu12 (optional, NVIDIA GPUs only) | NVIDIA proprietary licence (CUDA / cuDNN EULA); free to use, not redistributable by TARS |

## Programs

| Program | Notes |
|---|---|
| Claude Code (`claude`) | Anthropic. Installed with Anthropic's own installer; use is governed by Anthropic's terms and **your own** Claude account or plan. TARS never sees or stores your credentials. |
| Python, GitHub CLI (`gh`), Git, Microsoft Visual C++ Redistributable | Installed from their official sources via `winget`; their own licences apply. |

## Trademarks

"TARS" is the name of a robot in the film *Interstellar*. This is an unofficial, non-commercial hobby project with no
affiliation to the film or its rights holders, and it contains no film dialogue or assets. Rename it if you publish
a fork. "Claude" and "Claude Code" are trademarks of Anthropic, PBC; this project is not affiliated with or endorsed by Anthropic.
