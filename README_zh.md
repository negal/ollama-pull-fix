# ollama-pull-fix

> 在 `ollama pull` 因网络/TLS 问题失败时，用 `curl + aria2c` 替代下载，**支持断点续传**。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey.svg)]()

[English → README.md](./README.md)

---

## 这是解决什么问题的？

在公司网、校园网，或某些做了 SSL 中间人检测的运营商环境下，`ollama pull` 经常会出现：

```
Error: pull model manifest: Get "https://registry.ollama.ai/v2/...": EOF
```

根因是 Ollama 用 Go 自带的 `crypto/tls` 发起 HTTPS，它的 TLS ClientHello 指纹被中间设备识别后阻断；而 `curl` 和 `aria2c` 用的是 LibreSSL/BoringSSL，TLS 指纹不同，能正常握手。Ollama 官方 issue 区已经有大量讨论：[#12624](https://github.com/ollama/ollama/issues/12624)、[#1036](https://github.com/ollama/ollama/issues/1036)、[#6211](https://github.com/ollama/ollama/issues/6211)、[#8533](https://github.com/ollama/ollama/issues/8533)。

本工具完全绕开 Go TLS 这条路：用 `curl` 取 manifest，用 `aria2c` 多线程断点续传下载大模型 blob，再手动构造本地 manifest，让 `ollama list` 把这个模型当作正常 pull 下来的。

## 快速开始

```bash
# 1. 安装 aria2（curl 系统自带）
brew install aria2          # macOS
# sudo apt install aria2    # Debian/Ubuntu

# 2. 克隆并运行
git clone https://github.com/<your-username>/ollama-pull-fix.git
cd ollama-pull-fix
python3 scripts/ollama_deploy.py qwen2.5vl:3b
```

下载完成后，`ollama list` 能看到模型，`ollama run qwen2.5vl:3b` 就能用。

**中途断了**（Ctrl-C / 网断 / 电脑睡眠）？直接重跑同一条命令——会自动接着上次的进度继续下。

## 特性

- **纯标准库 Python**，零依赖，不用 `pip install`，不用 venv
- **多线程断点续传**，aria2c 默认 4 路并发
- **自动清空代理变量**（`HTTP_PROXY`、`HTTPS_PROXY` 等）——这种场景下代理通常帮倒忙
- **SHA256 校验**主模型 blob 与 manifest 一致
- **自动构建 manifest**，让 `ollama list` / `ollama run` 识别下载好的模型
- **可重入**，已下载完成的 blob 重跑会跳过

## 环境要求

- Python 3.8+
- `curl`（macOS / Linux 系统自带）
- `aria2`：`brew install aria2` / `apt install aria2` / `dnf install aria2`
- 已安装 Ollama（`ollama` CLI 在 `PATH` 中，最后会调用 `ollama list` 验证）

## 用法

```bash
python3 scripts/ollama_deploy.py <模型名>:<标签>
```

示例：

```bash
python3 scripts/ollama_deploy.py qwen2.5vl:3b
python3 scripts/ollama_deploy.py llama3.2:3b
python3 scripts/ollama_deploy.py deepseek-r1:7b
```

脚本会按步骤打印进度，最后自动 SHA256 校验主模型层：

```
Step 1/4: 获取模型 manifest
  ✅ 获取成功, layers: 4
Step 2/4: 下载 config blob
  ✅ 完成
Step 3/4: 下载模型文件 (4 个文件)
  📥 sha256:abc... (3.2 GB) (model)
      [#aaa 1.2GiB/3.2GiB(38%) CN:4 DL:25MiB ETA:1m20s]
  ...
Step 4/4: 构建本地 manifest
  ✅ Manifest 已创建
🔍 sha256 校验通过!
✅ qwen2.5vl:3b 部署完成!
```

### 已测试模型

| 模型 | 大小 | 状态 |
|------|------|------|
| `qwen2.5vl:3b` | 3.2 GB | ✅ 已验证 |

欢迎 PR 补充。

## 手工部署步骤（脚本不适用时）

### 1. 取 manifest

```bash
env -i HOME=$HOME PATH=$PATH \
  curl -s "https://registry.ollama.ai/v2/library/<model>/manifests/<tag>"
```

manifest 长这样：

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

### 2. 大文件用 aria2c 下载（断点续传）

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

### 3. 小文件用 curl 下载

```bash
env -i HOME=$HOME PATH=$PATH \
  curl -sL "https://registry.ollama.ai/v2/library/<model>/blobs/${BLOB_HASH}" \
  --max-time 30 -o "${BLOBS}/${BLOB_NAME}"
```

### 4. 构建本地 manifest

```bash
MANIFEST_DIR=~/.ollama/models/manifests/registry.ollama.ai/library/<model>
mkdir -p "$MANIFEST_DIR"
echo '{"compact json 单行..."}' > "$MANIFEST_DIR/<tag>"
```

> ⚠️ manifest **必须**是单行 compact JSON，pretty-print 的 JSON Ollama 识别不了。

### 5. 验证

```bash
ollama list | grep <model>
ollama show <model>:<tag>
shasum -a 256 ~/.ollama/models/blobs/sha256-... | cut -d' ' -f1
```

## 原理

**为什么 `ollama pull` 失败？**
Ollama 的 HTTP 客户端是 Go 的 `net/http` + `crypto/tls`，TLS ClientHello 指纹和 `curl` / 浏览器不同，某些中间设备（Palo Alto、Zscaler、Forcepoint，或国内运营商的 DPI）会在握手中途切断 → EOF。

**为什么挂代理也救不了？**
Clash/Verge 这类代理对 `registry.ollama.ai` 的 HTTPS 隧道经常不稳定，尤其重定向到 Cloudflare R2 的那一跳，常见 HTTP 000。

**为什么 `curl` / `aria2c` 直连可以？**
它们用 LibreSSL/BoringSSL，TLS 指纹更接近"普通浏览器"，中间设备不拦。一旦握手过了，背后的 Cloudflare CDN + R2（AWS4-HMAC-SHA256 签名 URL）数据通道是稳的。

**为什么必须清掉 `HTTP_PROXY` 等环境变量？**
半通不通的代理会让小文件能下、大 blob 半路断，比直接报错更糟。脚本用 `env -i HOME=$HOME PATH=$PATH` 给子进程一个干净环境——只在 shell 里 `unset` 没用，子进程会继承父 shell 的 env。

## FAQ

**Q：下载断了怎么办？**
重跑同一条命令即可。`aria2c -c` 和 `curl` 都会跳过已下载的部分。

**Q：下载完成后 `ollama list` 看不到？**
检查 manifest：
```bash
cat ~/.ollama/models/manifests/registry.ollama.ai/library/<model>/<tag>
```
必须是**单行 compact JSON**，pretty-print 的会被 Ollama 静默忽略。

**Q：磁盘还没下载就快满了？**
脚本默认 `--file-allocation=none`，aria2 不会预分配整个文件大小。如果你改了这个参数，加回去。

**Q：能下非 `library/` 命名空间的模型（比如 ollama.ai 上别人 push 的微调模型）吗？**
脚本目前写死 `library/` 命名空间。欢迎 PR 扩展支持 user/org 命名空间。

**Q：Windows 支持吗？**
没测过。Python 部分跨平台，但 `aria2c` / `curl` 调用和路径处理可能需要调整，欢迎 PR。

## 作为 Claude Code Skill 使用

本仓库本身就是一个合法的 [Claude Code](https://claude.com/claude-code) skill。安装：

```bash
git clone https://github.com/<your-username>/ollama-pull-fix.git ~/.claude/skills/ollama-pull-fix
```

之后在 Claude Code 里说"用 ollama-pull-fix 下载 llama3.2:3b"，Claude 会自动识别 `SKILL.md` 并调用脚本。

## 贡献

欢迎 PR，特别需要：
- 补充**已测试模型**表格
- Linux / Windows 兼容性反馈
- 非 `library/` 命名空间支持
- 其他受限网络环境的实测反馈

## License

[MIT](./LICENSE) © 2026 Xu Jiming
