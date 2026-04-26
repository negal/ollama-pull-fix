# ollama-pull-fix

> Resumable, network-resilient Ollama model downloader for environments where `ollama pull` fails with EOF / TLS errors.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey.svg)]()

[中文文档 → README_zh.md](./README_zh.md)

---

## The Problem

On corporate networks, campus Wi-Fi, or behind certain ISPs that perform SSL inspection, `ollama pull` fails with:

```
Error: pull model manifest: Get "https://registry.ollama.ai/v2/...": EOF
```

This happens because Ollama's Go HTTP client uses Go's native `crypto/tls` stack, whose TLS ClientHello fingerprint is blocked or interrupted by middleboxes — while plain `curl` and `aria2c` (using LibreSSL/BoringSSL) get through fine. See the long-standing discussions in [ollama/ollama#12624](https://github.com/ollama/ollama/issues/12624), [#1036](https://github.com/ollama/ollama/issues/1036), [#6211](https://github.com/ollama/ollama/issues/6211), and [#8533](https://github.com/ollama/ollama/issues/8533).

This tool bypasses the broken Go TLS path entirely: it fetches the manifest with `curl`, downloads the large model blobs with `aria2c` (multi-threaded, resumable), and constructs the local manifest by hand so `ollama list` recognizes the model as if it had been pulled normally.

## Quick Start

```bash
# 1. Install aria2 (curl is preinstalled)
brew install aria2          # macOS
# sudo apt install aria2    # Debian/Ubuntu

# 2. Clone & run
git clone https://github.com/negal/ollama-pull-fix.git
cd ollama-pull-fix
python3 scripts/ollama_deploy.py qwen2.5vl:3b
```

When the download finishes, `ollama list` will show the model and `ollama run qwen2.5vl:3b` will work normally.

**If interrupted** (Ctrl-C, network drop, sleep), just rerun the same command — it picks up where it left off.

## Features

- **Pure Python stdlib** — no `pip install`, no virtualenv, just `python3`
- **Multi-threaded resumable downloads** via `aria2c` (4 connections per server by default)
- **Auto-retry on incomplete downloads** (up to 3 attempts) — short network blips no longer require manual reruns
- **Forced size verification** after every blob — partial files are deleted and retried instead of silently passing
- **Per-layer SHA256 verification** — config + every layer is checked, not just the main model blob
- **Graceful degradation** — if one blob ultimately fails, downloaded bytes are preserved and you can rerun to resume
- **Auto-cleans proxy env vars** (`HTTP_PROXY`, `HTTPS_PROXY`, etc.) — proxies often make this worse, not better
- **Manifest auto-construction** so `ollama list` / `ollama run` recognize the downloaded model
- **Idempotent** — already-downloaded blobs are skipped on rerun

## Requirements

- Python 3.8+
- `curl` (preinstalled on macOS/Linux)
- `aria2` — `brew install aria2` / `apt install aria2` / `dnf install aria2`
- Ollama installed (the `ollama` CLI must be on `PATH` for the final `ollama list` check)

## Usage

```bash
python3 scripts/ollama_deploy.py <model>:<tag>
```

Examples:

```bash
python3 scripts/ollama_deploy.py qwen2.5vl:3b
python3 scripts/ollama_deploy.py llama3.2:3b
python3 scripts/ollama_deploy.py deepseek-r1:7b
```

The script prints progress for each blob, then verifies the SHA256 of the main model layer:

```
Step 1/4: Fetch manifest
  ✅ ok, layers: 4
Step 2/4: Download config blob
  ✅ ok
Step 3/4: Download model files (4 files)
  📥 sha256:abc... (3.2 GB) (model)
      [#aaa 1.2GiB/3.2GiB(38%) CN:4 DL:25MiB ETA:1m20s]
  ...
Step 4/4: Build local manifest
  ✅ ok
🔍 SHA256 verification passed
✅ qwen2.5vl:3b deployed.
```

### Tested models

| Model | Size | Status |
|-------|------|--------|
| `qwen2.5vl:3b` | 3.2 GB | ✅ verified |

PRs welcome to extend this table.

## How It Works (Manual Steps)

If the script doesn't fit your environment, you can do this by hand:

### 1. Fetch the manifest

```bash
env -i HOME=$HOME PATH=$PATH \
  curl -s "https://registry.ollama.ai/v2/library/<model>/manifests/<tag>"
```

The manifest looks like:

```json
{
  "config": { "digest": "sha256:...", "size": 567 },
  "layers": [
    { "mediaType": "application/vnd.ollama.image.model",    "digest": "sha256:...", "size": 3200614720 },
    { "mediaType": "application/vnd.ollama.image.template", "digest": "sha256:...", "size": 1024 },
    { "mediaType": "application/vnd.ollama.image.system",   "digest": "sha256:...", "size": 256 },
    { "mediaType": "application/vnd.ollama.image.params",   "digest": "sha256:...", "size": 128 }
  ]
}
```

### 2. Download the large model blob with aria2c (resumable)

```bash
BLOBS=~/.ollama/models/blobs
BLOB_HASH="sha256:..."
BLOB_NAME="${BLOB_HASH/sha256:/sha256-}"

env -i HOME=$HOME PATH=$PATH \
  aria2c -c -x 4 -s 4 --max-connection-per-server=4 \
  --continue=true --file-allocation=none \
  --max-tries=0 --retry-wait=5 \
  -d "$BLOBS" -o "$BLOB_NAME" \
  "https://registry.ollama.ai/v2/library/<model>/blobs/${BLOB_HASH}"
```

### 3. Download the small blobs with curl

```bash
env -i HOME=$HOME PATH=$PATH \
  curl -sL "https://registry.ollama.ai/v2/library/<model>/blobs/${BLOB_HASH}" \
  --max-time 30 -o "${BLOBS}/${BLOB_NAME}"
```

### 4. Construct the local manifest

```bash
MANIFEST_DIR=~/.ollama/models/manifests/registry.ollama.ai/library/<model>
mkdir -p "$MANIFEST_DIR"
echo '{"compact json one-liner..."}' > "$MANIFEST_DIR/<tag>"
```

> ⚠️ The manifest **must** be compact JSON on a single line — pretty-printed JSON is not recognized by Ollama.

### 5. Verify

```bash
ollama list | grep <model>
ollama show <model>:<tag>
shasum -a 256 ~/.ollama/models/blobs/sha256-... | cut -d' ' -f1
```

## Why It Works

**Why does `ollama pull` fail?** Ollama's HTTP client is Go's `net/http` + `crypto/tls`. Its TLS ClientHello fingerprint differs from `curl`/`browser` fingerprints, and some middleboxes (Palo Alto, Zscaler, Forcepoint, GFW-style filters) drop the connection mid-handshake → EOF.

**Why doesn't a proxy help?** Proxies like Clash/Verge often can't tunnel HTTPS to `registry.ollama.ai` cleanly either, especially on the redirect path to Cloudflare R2 — you typically see HTTP 000 instead of 200.

**Why does direct `curl`/`aria2c` work?** They use LibreSSL/BoringSSL, which presents a different TLS fingerprint that middleboxes treat as ordinary browser-like traffic. The actual data path (Cloudflare CDN → R2 with AWS4-HMAC-SHA256 signed URLs) is robust once the TLS handshake succeeds.

**Why must we clear `HTTP_PROXY` etc.?** A proxy that mostly works will partially handshake then drop on the large blob, leaving you with a worse failure mode. The script uses `env -i HOME=$HOME PATH=$PATH` to spawn child processes with a clean environment — `unset` in your shell doesn't help because the script's subprocesses would still inherit the parent env.

## FAQ

**Q: The download was interrupted. How do I resume?**
Just rerun the same command. `aria2c -c` and `curl` skip already-completed bytes/files.

**Q: `ollama list` doesn't show the model after download.**
Check the manifest file:
```bash
cat ~/.ollama/models/manifests/registry.ollama.ai/library/<model>/<tag>
```
It must be **compact** JSON on one line. Pretty-printed JSON is silently ignored.

**Q: My disk fills up before download starts.**
The script passes `--file-allocation=none` so aria2 doesn't preallocate the full file size. If you removed that flag, restore it.

**Q: Does this work for non-`library/` namespace models (e.g. fine-tunes pushed to ollama.ai)?**
The script currently hardcodes the `library/` namespace. PRs welcome to extend it to user/org namespaces.

**Q: Windows support?**
Not tested. The Python is portable but `aria2c`/`curl` invocation and path handling may need tweaks. PRs welcome.

## Use as a Claude Code Skill

This repo is also a valid [Claude Code](https://claude.com/claude-code) skill. To install:

```bash
git clone https://github.com/negal/ollama-pull-fix.git ~/.claude/skills/ollama-pull-fix
```

Then in Claude Code: *"use ollama-pull-fix to download llama3.2:3b"*. Claude will discover `SKILL.md` and run the script for you.

## Contributing

PRs welcome. Particularly useful:
- Extending the **Tested models** table
- Linux / Windows compatibility fixes
- Support for non-`library/` namespaces
- Reports from other restricted-network environments

## License

[MIT](./LICENSE) © 2026 Xu Jiming
