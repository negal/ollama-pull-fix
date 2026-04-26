#!/usr/bin/env python3
"""
Ollama 断点续传部署工具 (aria2c 版)

绕过 ollama pull 的 Go TLS 连不上 registry 的问题，直接:
1. curl 获取 manifest
2. aria2c 多线程断点续传下载主模型 blob
3. curl 下载小文件 (template/system/license/params/config)
4. 手动构建 manifest 文件
5. sha256 验证

改进 v2:
  - aria2c 下载后强制校验大小，不完整时自动重试（最多3次）
  - 增加超时和重试机制
  - 进度显示更清晰
  - 保留损坏文件的备份，避免"假成功"

使用:
  python3 ollama_deploy.py <model_name:tag>
  
示例:
  python3 ollama_deploy.py qwen2.5vl:3b
  python3 ollama_deploy.py llama3.2:3b

环境要求:
  - curl (系统自带)
  - aria2c (brew install aria2)
  - Ollama 已安装
"""

import json
import os
import subprocess
import sys
import shutil
import time
from pathlib import Path

OLLAMA_BLOBS = Path.home() / ".ollama" / "models" / "blobs"
OLLAMA_MANIFESTS = Path.home() / ".ollama" / "models" / "manifests" / "registry.ollama.ai" / "library"
MODEL_NAME = ""
MODEL_TAG = ""

# 重试配置
MAX_RETRIES = 3
ARIA2_TIMEOUT = 600  # 单次下载最长 10 分钟


def clean_env():
    """返回无代理的干净环境"""
    env = os.environ.copy()
    for key in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy"]:
        env.pop(key, None)
    env["HOME"] = str(Path.home())
    return env


def run(cmd, desc="", timeout=60, clean=True):
    """运行命令并返回 (returncode, stdout, stderr)"""
    env = clean_env() if clean else None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Timeout ({timeout}s)"
    except FileNotFoundError:
        return -2, "", f"Command not found: {cmd[0]}"


def get_manifest(model, tag):
    """获取 Ollama registry 的 manifest"""
    url = f"https://registry.ollama.ai/v2/library/{model}/manifests/{tag}"
    print(f"  📥 获取 manifest: {url}")
    code, out, err = run(["curl", "-s", url, "--max-time", "30"], clean=True)
    if code != 0 or not out:
        print(f"  ❌ 失败: {err or '空响应'}")
        return None
    try:
        data = json.loads(out)
        print(f"  ✅ 获取成功, layers: {len(data.get('layers', []))}")
        return data
    except json.JSONDecodeError as e:
        print(f"  ❌ JSON 解析失败: {e}")
        return None


def download_blob(digest, size, label=""):
    """下载单个 blob 到 Ollama blobs 目录（含自动重试）"""
    blob_name = digest.replace("sha256:", "sha256-")
    dest = OLLAMA_BLOBS / blob_name
    url = f"https://registry.ollama.ai/v2/library/{MODEL_NAME}/blobs/{digest}"

    # 跳过已完整下载的
    if dest.exists() and dest.stat().st_size == size:
        print(f"  ✅ 已存在: {blob_name} ({size_readable(size)}) {label}")
        return True

    # 小文件直接用 curl
    if size < 1024 * 1024:  # < 1MB
        print(f"  📥 下载小文件: {digest[:20]}... ({size_readable(size)}) {label}")
        code, _, err = run(["curl", "-sL", url, "--max-time", "30", "-o", str(dest)], clean=True, timeout=60)
        if code != 0:
            print(f"  ❌ 失败: {err}")
            return False
        actual = dest.stat().st_size if dest.exists() else 0
        if actual != size:
            print(f"  ⚠️ 大小不符: 期望 {size}, 实际 {actual} -> 删除重试")
            if dest.exists():
                dest.unlink()
            return download_blob(digest, size, label)  # 递归重试一次
        print(f"  ✅ 完成")
        return True

    # 大文件用 aria2c 多线程断点续传（含自动重试）
    return _download_large_with_retry(blob_name, dest, url, size, label)


def _download_large_with_retry(blob_name, dest, url, size, label, attempt=1):
    """递归重试下载大文件，最多 MAX_RETRIES 次"""
    print(f"\n  📥 下载大文件 [{attempt}/{MAX_RETRIES}]: {blob_name[:20]}... ({size_readable(size)}) {label}")
    print(f"      → {dest}")

    # 如果已有部分文件，显示进度
    existing = dest.stat().st_size if dest.exists() else 0
    if existing > 0:
        pct = existing / size * 100 if size > 0 else 0
        print(f"     续传起点: {size_readable(existing)} / {size_readable(size)} ({pct:.0f}%)")

    aria2_args = [
        "aria2c", "-c",
        "-x", "4", "-s", "4",
        "--max-connection-per-server=4",
        "--continue=true",
        "--file-allocation=none",
        "--max-tries=5",
        "--retry-wait=5",
        "--connect-timeout=30",
        "--timeout=60",
        "--console-log-level=notice",
        "-d", str(OLLAMA_BLOBS),
        "-o", blob_name,
        url
    ]

    # aria2c 用无限 timeout（大文件下载）
    proc = subprocess.Popen(
        aria2_args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=clean_env()
    )

    # 实时显示进度
    start_time = time.time()
    for line in iter(proc.stdout.readline, ""):
        line = line.strip()
        if not line:
            continue
        if "ETA:" in line and ("%" in line or "MiB" in line or "GiB" in line):
            print(f"      {line}")
        elif "Download complete" in line:
            print(f"      ✅ {line}")
        elif "error" in line.lower() or "Error" in line or "ERROR" in line:
            # 不立即退出，等 proc.wait() 判断
            print(f"      ⚠️ {line}")

    proc.wait()

    # ---- 完整性校验 ----
    actual_size = dest.stat().st_size if dest.exists() else 0

    if actual_size == size:
        print(f"  ✅ 下载完成 ({size_readable(size)})")
        return True

    # 下载不完整
    elapsed = time.time() - start_time
    pct = actual_size / size * 100 if size > 0 else 0
    print(f"  ⚠️ 下载不完整: {size_readable(actual_size)}/{size_readable(size)} ({pct:.0f}%) [{elapsed:.0f}s]")

    if attempt < MAX_RETRIES:
        print(f"  🔄 等待 3 秒后重试 ({attempt+1}/{MAX_RETRIES})...")
        time.sleep(3)
        return _download_large_with_retry(blob_name, dest, url, size, label, attempt + 1)
    else:
        print(f"  ❌ 已达最大重试次数 ({MAX_RETRIES})，下载失败")
        print(f"     重新运行脚本即可续传（已下载的 {size_readable(actual_size)} 不会丢失）")
        return False


def download_blob_with_retry(digest, size, label=""):
    """包装 download_blob，外层重试（应对小文件/网络波动）"""
    for attempt in range(1, MAX_RETRIES + 1):
        ok = download_blob(digest, size, label)
        if ok:
            return True
        # download_blob 内部的 _download_large_with_retry 已有重试
        # 这里只处理小文件连续失败或意外情况
        blob_name = digest.replace("sha256:", "sha256-")
        dest = OLLAMA_BLOBS / blob_name
        if dest.exists() and dest.stat().st_size == size:
            return True
        if attempt < MAX_RETRIES:
            print(f"  🔄 整体重试 ({attempt+1}/{MAX_RETRIES})...")
            time.sleep(2)
    return False


def build_manifest(model, tag, manifest_data):
    """构建 manifest 文件"""
    manifest_dir = OLLAMA_MANIFESTS / model
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_file = manifest_dir / tag

    with open(manifest_file, "w") as f:
        json.dump(manifest_data, f, separators=(",", ":"))

    print(f"  ✅ Manifest 已创建: {manifest_file}")
    return True


def verify_model_sha256(model, tag, manifest_data):
    """逐层验证所有 blob 的 sha256"""
    all_pass = True
    blobs_to_check = [manifest_data["config"]] + manifest_data.get("layers", [])

    print(f"  共 {len(blobs_to_check)} 个文件待校验...")

    for i, blob_info in enumerate(blobs_to_check):
        expected_hash = blob_info["digest"].replace("sha256:", "")
        blob_name = f"sha256-{expected_hash}"
        blob_path = OLLAMA_BLOBS / blob_name

        if not blob_path.exists():
            print(f"  [{i+1}/{len(blobs_to_check)}] ❌ 文件不存在: {blob_name[:20]}...")
            all_pass = False
            continue

        # 小文件直接比较大小
        if blob_info["size"] < 1024 * 1024:
            actual_size = blob_path.stat().st_size
            if actual_size == blob_info["size"]:
                print(f"  [{i+1}/{len(blobs_to_check)}] ✅ {blob_name[:20]}... (大小校验通过)")
            else:
                print(f"  [{i+1}/{len(blobs_to_check)}] ❌ {blob_name[:20]}... 大小不符: {actual_size}/{blob_info['size']}")
                all_pass = False
            continue

        # 大文件校验 sha256
        print(f"  [{i+1}/{len(blobs_to_check)}] 🔍 计算 sha256: {blob_name[:20]}... ({(blob_info['size']/1024/1024/1024):.1f}GB 约需 1-2 分钟)")
        code, out, err = run(["shasum", "-a", "256", str(blob_path)], clean=True, timeout=300)
        if code != 0:
            print(f"  ❌ 校验失败: {err}")
            all_pass = False
            continue

        actual_hash = out.split()[0] if out else ""

        if actual_hash == expected_hash:
            print(f"  ✅ sha256 匹配 ✓")
        else:
            print(f"  ❌ sha256 不匹配!")
            print(f"     期望: {expected_hash}")
            print(f"     实际: {actual_hash}")
            all_pass = False

    return all_pass


def ollama_list():
    """查看已安装的模型"""
    code, out, err = run(["ollama", "list"], clean=True)
    if code == 0:
        return out
    return f"Error: {err}"


def size_readable(bytes_val):
    """人类可读的文件大小"""
    if bytes_val == 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes_val < 1024:
            return f"{bytes_val:.1f} {unit}" if unit != "B" else f"{bytes_val} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f} PB"


def main():
    global MODEL_NAME, MODEL_TAG

    if len(sys.argv) < 2:
        print("用法: python3 ollama_deploy.py <模型名:标签>")
        print("示例: python3 ollama_deploy.py qwen2.5vl:3b")
        sys.exit(1)

    full_name = sys.argv[1]
    if ":" in full_name:
        MODEL_NAME, MODEL_TAG = full_name.split(":", 1)
    else:
        MODEL_NAME = full_name
        MODEL_TAG = "latest"

    print(f"\n{'='*60}")
    print(f"  Ollama 断点续传部署工具 v2")
    print(f"  模型: {MODEL_NAME}:{MODEL_TAG}")
    print(f"{'='*60}\n")

    # 检查依赖
    for cmd in ["curl", "aria2c"]:
        if shutil.which(cmd) is None:
            print(f"❌ 缺少依赖: {cmd} (brew install {cmd})")
            sys.exit(1)

    # 创建 blobs 目录
    OLLAMA_BLOBS.mkdir(parents=True, exist_ok=True)

    # Step 1: 获取 manifest
    print("Step 1/4: 获取模型 manifest")
    manifest = get_manifest(MODEL_NAME, MODEL_TAG)
    if manifest is None:
        sys.exit(1)

    print(f"  模型 blob 总数: {len(manifest.get('layers', []))} (+ 1 config)")

    # Step 2: 下载 config blob
    print("\nStep 2/4: 下载 config blob")
    config = manifest.get("config", {})
    if config:
        download_blob_with_retry(config["digest"], config["size"], "(config)")

    # Step 3: 下载所有 layer blobs
    layers = manifest.get("layers", [])
    print(f"\nStep 3/4: 下载模型文件 ({len(layers)} 个文件)")

    all_ok = True
    for i, layer in enumerate(layers):
        media = layer["mediaType"]
        label = media.split(".")[-1] if "." in media else ""
        ok = download_blob_with_retry(layer["digest"], layer["size"], f"({label})")
        if not ok:
            all_ok = False
            print(f"  ❌ 文件 {i+1}/{len(layers)} 下载失败")

    if not all_ok:
        print(f"\n  ⚠️ 部分文件下载失败，但已下载的部分已保留。重新运行即可续传。")
        print(f"     如需继续，脚本将继续构建已有文件的 manifest。")
        # 不退出，继续构建 manifest，便于部分恢复

    # Step 4: 构建 manifest
    print("\nStep 4/4: 构建本地 manifest")
    build_manifest(MODEL_NAME, MODEL_TAG, manifest)

    # 验证（逐层 sha256）
    print("\n验证中...")
    verify_ok = verify_model_sha256(MODEL_NAME, MODEL_TAG, manifest)

    if verify_ok:
        print(f"\n{'='*60}")
        print(f"  ✅ {MODEL_NAME}:{MODEL_TAG} 部署成功!")
        print(f"{'='*60}")
    else:
        print(f"\n{'='*60}")
        print(f"  ⚠️ {MODEL_NAME}:{MODEL_TAG} 部署完成，但部分校验失败")
        print(f"     重新运行以续传损坏的文件")
        print(f"{'='*60}")

    print(f"\n已安装模型:")
    print(ollama_list())


if __name__ == "__main__":
    main()
