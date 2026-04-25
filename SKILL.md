---
name: ollama-deploy
description: 在受限网络环境（即 ollama pull 因 Go TLS 握手失败返回 EOF）下，用断点续传方式部署 Ollama 模型到本地。核心是 curl 获取 manifest + aria2c 多线程下载大 blob + 手动构建 manifest。使用场景：(1) ollama pull 返回 "EOF" 错误时替代方案，(2) 需要下载大模型的断点续传，(3) 需要精确指定模型版本而非 pull 自动选择。不适用于：普通网络下 ollama pull 正常工作的环境。
---

> 这是 Claude Code skill 安装文件。把整个仓库放到 `~/.claude/skills/ollama-pull-fix/` 即可被 Claude Code 识别。直接用命令行的用户请看 [README.md](./README.md) / [README_zh.md](./README_zh.md)。

# Ollama 断点续传部署

## 背景

在某些网络环境（如公司/校园网、运营商限制、SSL 中间人）下，Ollama 自带的 `ollama pull` 会失败：
```
Error: pull model manifest: Get "https://registry.ollama.ai/v2/...": EOF
```

这是因为 Ollama 底层用 Go 的 HTTP 客户端建立 TLS 连接时失败，但 **curl、aria2c 直连可正常工作**。

本技能提供替代方案：绕过 Go TLS 问题，直接用 curl + aria2c 下载，手动构建 manifest。

## 前置条件

- `curl` — 系统自带
- `aria2c` — `brew install aria2`（macOS）/ `apt install aria2`（Linux）
- Ollama 已安装运行

## 使用方式

### 一键部署脚本

```bash
python3 scripts/ollama_deploy.py <模型名:标签>
```

示例：
```bash
python3 scripts/ollama_deploy.py qwen2.5vl:3b
python3 scripts/ollama_deploy.py llama3.2:3b
```

### 手工部署步骤（如果脚本不适用）

#### Step 1: 获取 manifest

```bash
# 必须用干净环境（unset 代理变量）
env -i HOME=$HOME PATH=$PATH \
  curl -s "https://registry.ollama.ai/v2/library/{model}/manifests/{tag}"
```

#### Step 2: 看 manifest 里有哪些 blob

Manifest 结构：
```json
{
  "config": { "digest": "sha256:...", "size": 567 },
  "layers": [
    { "mediaType": "application/vnd.ollama.image.model",
      "digest": "sha256:...", "size": 3200614720 },
    { "mediaType": "application/vnd.ollama.image.template", ... },
    { "mediaType": "application/vnd.ollama.image.system", ... },
    { "mediaType": "application/vnd.ollama.image.params", ... }
  ]
}
```

#### Step 3: 下载 blob

**大文件**（主模型，几 GB）—— 用 aria2c 多线程断点续传：
```bash
BLOBS=~/.ollama/models/blobs
BLOB_HASH="sha256:..."
BLOB_NAME="${BLOB_HASH/sha256:/sha256-}"

env -i HOME=$HOME PATH=$PATH \
  aria2c -c -x 4 -s 4 --max-connection-per-server=4 \
  --continue=true --file-allocation=none \
  --max-tries=0 --retry-wait=5 \
  -d "$BLOBS" -o "$BLOB_NAME" \
  "https://registry.ollama.ai/v2/library/{model}/blobs/${BLOB_HASH}"
```

**小文件**（template/system/params/config，几 KB~几百 KB）—— 用 curl：
```bash
env -i HOME=$HOME PATH=$PATH \
  curl -sL "https://registry.ollama.ai/v2/library/{model}/blobs/${BLOB_HASH}" \
  --max-time 30 -o "${BLOBS}/${BLOB_NAME}"
```

#### Step 4: 构建 manifest

```bash
MANIFEST_DIR=~/.ollama/models/manifests/registry.ollama.ai/library/{model}
mkdir -p "$MANIFEST_DIR"
echo '{...完整manifest json...}' > "$MANIFEST_DIR/{tag}"
```

#### Step 5: 验证

```bash
ollama list | grep {model}
ollama show {model}:{tag} | head -10
# sha256 校验
shasum -a 256 ~/.ollama/models/blobs/sha256-... | cut -d' ' -f1
```

## 关键原理

### 为什么 ollama pull 失败？
Ollama 用 **Go 的 TLS 实现**（crypto/tls）连接 registry.ollama.ai。在某些网络环境下，Go 的 TLS ClientHello 被中间设备阻断，返回 EOF。
而 **curl** 用 LibreSSL/BoringSSL 的底层库，TLS 指纹不同，握手成功。

### 为什么走代理也失败？
Clash Verge 等代理对 registry.ollama.ai 的 HTTPS 隧道在某些网络下也不工作（HTTP 000）。

### 直连为什么可以？
registry.ollama.ai 使用 Cloudflare CDN + R2 对象存储，重定向到 Cloudflare R2 的 AWS4-HMAC-SHA256 签名的临时 URL。

### 关键环境变量
- **必须清除** `HTTP_PROXY`, `HTTPS_PROXY`, `http_proxy`, `https_proxy`, `NO_PROXY`, `no_proxy`
- 用 `env -i HOME=$HOME PATH=$PATH` 创建干净环境
- 不要用 `unset` 然后再执行——子进程会继承父进程的 env

## 常见问题

### 下载中断了怎么办？
全部用 `-c` / `--continue=true` 模式，重跑同一命令即可续传。

### 下载完成后 ollama list 看不到？
检查 manifest 文件是否正确：
```bash
cat ~/.ollama/models/manifests/registry.ollama.ai/library/{model}/{tag}
```
JSON 必须是一行无格式的 compact JSON（不能用 pretty print）。

### 磁盘空间不足？
`--file-allocation=none` 可以避免 aria2 预分配整个文件。

## 已测试的模型

| 模型 | 大小 | 状态 |
|------|------|------|
| qwen2.5vl:3b | 3.2 GB | ✅ 已验证 |
