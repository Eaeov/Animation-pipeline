#!/usr/bin/env bash
# =====================================================================
# 安装 git 钩子到 .git/hooks/
#
# 用法: bash tools/install_hooks.sh
#
# 为什么不把钩子直接放进 .git/hooks/:
#   .git/ 目录不入版本控制, 所以钩子无法随仓库分发。
#   正确做法是把钩子脚本放进 tools/ 并入库, 用本脚本安装到 .git/hooks/。
#   这样协作者 clone 后跑一次本脚本即可获得同样的保护。
# =====================================================================

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
HOOKS="$ROOT/.git/hooks"

if [ ! -d "$ROOT/.git" ]; then
  echo "[FAIL] 当前目录不是 git 仓库: $ROOT" >&2
  exit 1
fi

mkdir -p "$HOOKS"

install_one() {
  local name="$1"
  local src="$HERE/$name"
  local dst="$HOOKS/$name"
  if [ ! -f "$src" ]; then
    echo "[skip] 源文件不存在: $src"
    return
  fi
  cp "$src" "$dst"
  chmod +x "$dst" 2>/dev/null || true
  echo "[OK ] 已安装钩子: .git/hooks/$name"
}

install_one pre-commit

echo ""
echo "钩子安装完成。之后每次 git commit 都会自动跑安全自查。"
echo ""
echo "测试方式:"
echo "  bash tools/preflight.sh        # 手动跑一次看结果"
echo "  随便改个文件然后 git commit    # 观察钩子是否触发"
echo ""
echo "临时跳过 (不推荐):"
echo "  git commit --no-verify"
