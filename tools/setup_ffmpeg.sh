#!/usr/bin/env bash
# 一键把 ffmpeg 装到项目内 tools/ffmpeg/ (不污染系统 PATH)
#
# 为什么要自备 ffmpeg:
#   1. 部分环境 winget 装完符号链接会失败
#   2. 项目自带可保证任何人 clone 后行为一致 (可复现性要求)
#   3. 不占用系统环境, 卸载即删目录
#
# 用法: bash tools/setup_ffmpeg.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HERE/ffmpeg"
# 钉死 6.1.1, 不要用 latest / 9.x:
#   实测 ffmpeg 9.0.2 (GyanD full_build) 的 ffprobe 在本机直接段错误,
#   连 `ffprobe -version` 都崩溃 (rc=139 / 3221225477), 属该版本二进制缺陷.
#   6.1.1 同源同架构实测正常. 详见 docs/04-交付说明.md 失败案例 4.
VERSION="6.1.1"
NAME="ffmpeg-${VERSION}-full_build"

# 多个镜像依次尝试 (GitHub 直连在部分网络不可用)
URLS=(
  "https://ghproxy.net/https://github.com/GyanD/codexffmpeg/releases/download/${VERSION}/${NAME}.zip"
  "https://gh-proxy.com/https://github.com/GyanD/codexffmpeg/releases/download/${VERSION}/${NAME}.zip"
  "https://github.com/GyanD/codexffmpeg/releases/download/${VERSION}/${NAME}.zip"
)

if [ -x "$DEST/bin/ffmpeg" ] || [ -x "$DEST/bin/ffmpeg.exe" ]; then
  echo "[OK] ffmpeg 已存在: $DEST"
  exit 0
fi

echo "==> 下载 ffmpeg ${VERSION}"
ZIP="$HERE/ffmpeg.zip"
ok=0
for u in "${URLS[@]}"; do
  echo "    try: ${u%%/https*}..."
  if curl -L --fail -s -o "$ZIP" "$u"; then
    sz=$(wc -c < "$ZIP" 2>/dev/null || echo 0)
    if [ "$sz" -gt 10000000 ]; then ok=1; echo "    ok ($((sz/1024/1024)) MB)"; break; fi
  fi
done
if [ "$ok" -ne 1 ]; then
  echo "[FAIL] 所有镜像下载失败, 请手动下载并解压到 $DEST" >&2
  exit 1
fi

echo "==> 解压"
rm -rf "$HERE/_x" && mkdir -p "$HERE/_x"
if command -v unzip >/dev/null 2>&1; then
  unzip -q "$ZIP" -d "$HERE/_x"
else
  tar -xf "$ZIP" -C "$HERE/_x"          # Windows 自带 tar 支持 zip
fi

mkdir -p "$DEST"
SRC="$HERE/_x/$NAME"
cp -r "$SRC"/* "$DEST"/ 2>/dev/null || cp -r "$HERE/_x"/*/.* "$DEST"/ 2>/dev/null || true
rm -rf "$HERE/_x" "$ZIP"

if [ -x "$DEST/bin/ffmpeg" ] || [ -x "$DEST/bin/ffmpeg.exe" ]; then
  echo "[OK] 安装完成: $DEST/bin"
  echo "     用法: export PATH=\"$DEST/bin:\$PATH\""
  "$DEST/bin/ffmpeg" -version 2>/dev/null | head -1 || true
  # 健康自检: 光有可执行文件不代表能用 (9.0.2 就是装得上但 ffprobe 段错误)
  if "$DEST/bin/ffprobe" -version >/dev/null 2>&1; then
    echo "[OK] ffprobe 自检通过"
  else
    echo "[WARN] ffprobe 自检失败 (可能段错误), 请更换 ffmpeg 版本" >&2
    echo "       已知 9.0.2 full_build 有此问题, 建议 6.1.1" >&2
  fi
else
  echo "[FAIL] 解压后未找到 ffmpeg 可执行文件, 请检查 $DEST" >&2
  exit 1
fi
