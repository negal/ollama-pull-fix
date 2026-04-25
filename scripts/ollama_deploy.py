#!/usr/bin/env python3
"""
Ollama 断点续传部署工具 (aria2c 版)

绕过 ollama pull 的 Go TLS 连不上 registry 的问题，直接:
1. curl 获取 manifest
2. aria2c 多线程断点续传下载主模型 blob
3. curl 下载小文件 (template/system/license/params/config)
4. 手动构建 manifest 文件

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
from pathlib import Path

OLLAMA_BLOBS = Path.home() / ".ollama" / "models" / "blobs"
OLLAMA_MANIFESTS = Path.home() / ".ollama" / "models" / "manifests" / "registry.ollama.ai" / "library"
MODEL_NAME = ""
MODEL_TAG = ""

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
    """下载单个 blob 到 Ollama blobs 目录"""
    blob_name = digest.replace("sha256:", "sha256-")
    dest = OLLAMA_BLOBS / blob_name
    url = f"https://registry.ollama.ai/v2/library/{MODEL_NAME}/blobs/{digest}"
    
    # 跳过已完整下载的
    if dest.exists() and dest.stat().st_size == size:
        print(f"  ✅ 已存在: {blob_name} ({size_readable(size)})")
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
            print(f"  ⚠️ 大小不符: 期望 {size}, 实际 {actual}")
            return False
        print(f"  ✅ 完成")
        return True
    
    # 大文件用 aria2c 多线程断点续传
    print(f"  📥 下载大文件: {digest[:20]}... ({size_readable(size)}) {label}")
    print(f"      → {dest}")
    
    aria2_args = [
        "aria2c", "-c",
        "-x", "4", "-s", "4",
        "--max-connection-per-server=4",
        "--continue=true",
        "--file-allocation=none",
        "--max-tries=0",
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
    last_line = ""
    for line in iter(proc.stdout.readline, ""):
        line = line.strip()
        if line:
            last_line = line
            # 只显示进度摘要行
            if "ETA:" in line and ("%" in line or "MiB" in line or "GiB" in line):
                print(f"      {line}")
            elif "Download complete" in line or "error" in line.lower():
                print(f"      {line}")
    
    proc.wait()
    
    if dest.exists() and dest.stat().st_size == size:
        print(f"  ✅ 下载完成, sha256 待验证")
        return True
    else:
        actual = dest.stat().st_size if dest.exists() else 0
        print(f"  ⚠️ 下载不完整: {size_readable(actual)}/{size_readable(size)}")
        print(f"     重新运行即可续传")
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

def verify_model(model, tag, manifest_data):
    """验证 sha256 校验"""
    blob_name = manifest_data["layers"][0]["digest"].replace("sha256:", "sha256-")
    blob_path = OLLAMA_BLOBS / blob_name
    
    if not blob_path.exists():
        print(f"  ❌ 模型文件不存在!")
        return False
    
    print(f"  🔍 计算 sha256... (3.2GB 可能需 1-2 分钟)")
    code, out, err = run(["shasum", "-a", "256", str(blob_path)], clean=True, timeout=300)
    if code != 0:
        print(f"  ❌ 校验失败: {err}")
        return False
    
    actual_hash = out.split()[0] if out else ""
    expected_hash = manifest_data["layers"][0]["digest"].replace("sha256:", "")
    
    if actual_hash == expected_hash:
        print(f"  ✅ sha256 校验通过!")
        return True
    else:
        print(f"  ❌ sha256 校验失败!")
        print(f"     期望: {expected_hash}")
        print(f"     实际: {actual_hash}")
        return False

def ollama_list():
    """查看已安装的模型"""
    code, out, err = run(["ollama", "list"], clean=True)
    if code == 0:
        return out
    return f"Error: {err}"

def size_readable(bytes_val):
    """人类可读的文件大小"""
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
    print(f"  Ollama 断点续传部署工具")
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
    
    # 保存 manifest
    manifest_json = json.dumps(manifest, separators=(",", ":"))
    print(f"  模型 blob 总数: {len(manifest.get('layers', []))} (+ 1 config)")
    
    # Step 2: 下载 config blob
    print("\nStep 2/4: 下载 config blob")
    config = manifest.get("config", {})
    if config:
        download_blob(config["digest"], config["size"], f"(config)")
    
    # Step 3: 下载所有 layer blobs
    print(f"\nStep 3/4: 下载模型文件 ({len(manifest.get('layers', []))} 个文件)")
    for layer in manifest.get("layers", []):
        media = layer["mediaType"]
        label = media.split(".")[-1] if "." in media else ""
        download_blob(layer["digest"], layer["size"], f"({label})")
    
    # Step 4: 构建 manifest
    print("\nStep 4/4: 构建本地 manifest")
    build_manifest(MODEL_NAME, MODEL_TAG, manifest)
    
    # 验证
    print("\n验证中...")
    verify_model(MODEL_NAME, MODEL_TAG, manifest)
    
    print(f"\n{'='*60}")
    print(f"  ✅ {MODEL_NAME}:{MODEL_TAG} 部署完成!")
    print(f"{'='*60}")
    print(f"\n已安装模型:")
    print(ollama_list())

if __name__ == "__main__":
    main()
