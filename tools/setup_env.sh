#!/usr/bin/env bash
# 环境初始化: 创建独立 venv 并安装依赖
#
# 用法: bash tools/setup_env.sh [venv路径]
#
# 注意: 默认使用阿里云 PyPI 镜像 —— pip >= 24 对清华源的
#       "simple" 索引格式存在兼容问题, 会报 "No matching distribution found"。

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VENV="${1:-$ROOT/.venv}"
MIRROR="-i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com"

echo "==> 仓库: $ROOT"
echo "==> venv: $VENV"

PY="${PYTHON:-python}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "[FAIL] 未找到 python, 请设置 PYTHON 环境变量" >&2
  exit 1
fi

"$PY" -V

if [ ! -e "$VENV" ]; then
  echo "==> 创建 venv"
  "$PY" -m venv "$VENV"
fi

if [ -x "$VENV/Scripts/python.exe" ]; then
  VP="$VENV/Scripts/python.exe"
elif [ -x "$VENV/bin/python" ]; then
  VP="$VENV/bin/python"
else
  echo "[FAIL] venv 结构异常" >&2
  exit 1
fi

echo "==> 安装依赖"
"$VP" -m pip install --upgrade pip -q $MIRROR
"$VP" -m pip install -r "$ROOT/requirements.txt" $MIRROR

echo "==> 验证"
PYTHONPATH="$ROOT/src" "$VP" -c "
import importlib
bad=[]
for m in ['yaml','requests','PIL','numpy','cv2','skimage']:
    try: importlib.import_module(m); print(f'  OK   {m}')
    except ImportError: print(f'  MISS {m}'); bad.append(m)
raise SystemExit(1 if bad else 0)
" || { echo '[FAIL] 依赖不完整' >&2; exit 1; }

echo ""
echo "[OK] 环境就绪"
echo "     激活: source $VENV/Scripts/activate   (Windows bash)"
echo "           $VENV\\Scripts\\activate.bat     (CMD)"
echo "     体检: PYTHONPATH=src $VP -m anime_pv.cli doctor"
