#!/usr/bin/env bash
# =====================================================================
# 提交前安全自查 —— 防止密钥/素材/大文件被误提交
#
# 为什么需要这个脚本:
#   ".gitignore 写了"和"真的没提交"是两件事。
#   .gitignore 只对"未被跟踪"的文件生效 —— 一旦某个文件曾经被 add 过,
#   后面再改 .gitignore 也不会自动移除它。
#   所以每次提交前必须实际扫描一遍。
#
# 用法:
#   bash tools/preflight.sh          # 检查
#   bash tools/preflight.sh --fix    # 检查并自动取消暂存违规文件
# =====================================================================

set -uo pipefail

FIX=0
[ "${1:-}" = "--fix" ] && FIX=1

RED='\033[0;31m'; YEL='\033[0;33m'; GRN='\033[0;32m'; NC='\033[0m'
fail=0

hr() { printf '%s\n' "------------------------------------------------------------"; }
ok()   { printf "${GRN}[OK ]${NC} %s\n" "$1"; }
bad()  { printf "${RED}[!! ]${NC} %s\n" "$1"; fail=1; }
warn() { printf "${YEL}[?  ]${NC} %s\n" "$1"; }

# 扫描范围策略:
#   用 grep -r 扫描工作区, 但排除"永不会入库的目录"(依赖/环境/产物)。
# 为什么不用 git ls-files 逐文件扫: 每个文件 fork 一次 grep, 大型仓库下
#   慢到不可用 (实测 60 文件也因进程替换开销超时)。
# 为什么必须排除依赖目录: 实测 PIL 的 ImageFont.py 内嵌 base64 字体数据
#   里恰好含 sk- 开头长串, 被误判为 DashScope 密钥。误报比漏报更危险 ——
#   它会让人习惯性加 --no-verify, 安全体系直接失效。
EXCLUDES=(--exclude-dir=.git --exclude-dir=.venv --exclude-dir=venv
          --exclude-dir=node_modules --exclude-dir=__pycache__
          --exclude-dir=runs --exclude-dir=tools/ffmpeg
          --exclude-dir=models --exclude-dir=.pytest_cache
          --exclude-dir=.mypy_cache --exclude-dir=dist --exclude-dir=build
          --exclude-dir=site-packages)
INCLUDES=(--include="*.py" --include="*.yaml" --include="*.yml"
          --include="*.json" --include="*.toml" --include="*.sh"
          --include="*.env" --include="*.md" --include="*.txt"
          --include="*.cfg" --include="*.ini")

echo "============================================================"
echo "提交前安全自查"
echo "============================================================"

# --------------------------------------------------------------------- 1
hr
echo "1. 密钥扫描 (已排除依赖/产物目录)"
# 常见密钥格式: DashScope sk-xxx / 阿里云 LTAI / GitHub token / 各类 secret
PATTERN='(sk-[a-zA-Z0-9]{20,}|LTAI[a-zA-Z0-9]{12,}|AKIA[0-9A-Z]{16}|ghp_[a-zA-Z0-9]{30,}|github_pat_[a-zA-Z0-9_]{50,}|xox[baprs]-[a-zA-Z0-9-]{10,})'

hits=$(grep -rInE "$PATTERN" "${INCLUDES[@]}" "${EXCLUDES[@]}" . 2>/dev/null || true)

if [ -n "$hits" ]; then
  bad "发现疑似密钥:"
  printf '%s\n' "$hits" | sed 's/^/       /'
else
  ok "未发现真实密钥格式"
fi

# 关键词扫描 (排除文档中的说明性文字)
hr
echo "2. 敏感关键词扫描"
kw=$(grep -rInE "(api_?key|password|passwd|secret|access_?token)[[:space:]]*[:=][[:space:]]*[\"'][^\"'\$]{8,}" \
       "${INCLUDES[@]}" "${EXCLUDES[@]}" . 2>/dev/null \
     | grep -viE "os\.environ|getenv|\$\{|example|<your|xxx|占位|placeholder|TODO" \
     || true)
if [ -n "$kw" ]; then
  warn "以下位置疑似硬编码凭证 (请人工确认):"
  printf '%s\n' "$kw" | sed 's/^/       /'
else
  ok "未发现硬编码凭证"
fi

# --------------------------------------------------------------------- 3
hr
echo "3. 被跟踪文件中的敏感/大体积文件"
bad_tracked=0
while IFS= read -r f; do
  [ -z "$f" ] && continue
  case "$f" in
    *.env|*.key|*.pem|secrets.*|credentials.*|auth.json|token.json)
      bad "跟踪了敏感文件: $f"; bad_tracked=1 ;;
    assets/source/*|assets/character/*)
      case "$f" in *.gitkeep) ;; *)
        bad "跟踪了版权素材: $f"; bad_tracked=1 ;; esac ;;
    runs/*)
      case "$f" in *.gitkeep) ;; *)
        bad "跟踪了运行产物: $f"; bad_tracked=1 ;; esac ;;
    tools/ffmpeg/*|tools/*.zip)
      bad "跟踪了大体积工具: $f"; bad_tracked=1 ;;
    *.mp4|*.mov|*.avi|*.mkv)
      bad "跟踪了视频文件: $f"; bad_tracked=1 ;;
  esac
done < <(git ls-files 2>/dev/null)
[ "$bad_tracked" -eq 0 ] && ok "无敏感/大体积文件被跟踪"

# --------------------------------------------------------------------- 4
hr
echo "4. 大文件检查 (> 1MB)"
big=$(git ls-files -z 2>/dev/null | xargs -0 -I{} sh -c \
        'test -f "{}" && s=$(wc -c < "{}") && [ "$s" -gt 1048576 ] && echo "$s {}"' \
        2>/dev/null | sort -rn || true)
if [ -n "$big" ]; then
  while read -r sz path; do
    warn "$(( sz / 1024 )) KB  $path"
  done <<< "$big"
else
  ok "无超过 1MB 的文件被跟踪"
fi

# --------------------------------------------------------------------- 5
hr
echo "5. git 历史中的敏感内容"
hist=$(git log --all -p 2>/dev/null | grep -E "$PATTERN" | head -5 || true)
if [ -n "$hist" ]; then
  bad "历史提交中发现疑似密钥 (需要 git filter-repo 清洗):"
  printf '%s\n' "$hist" | sed 's/^/       /'
else
  ok "历史提交中无密钥"
fi

# --------------------------------------------------------------------- 6
hr
echo "6. .gitignore 规则有效性"
gi_fail=0
for f in \
  "tools/ffmpeg/bin/ffmpeg.exe" \
  "assets/source/pv.mp4" \
  "assets/character/A.png" \
  "runs/test/manifest.json" \
  ".env" \
  "secrets.yaml" \
  "configs/_local.yaml" \
; do
  if git check-ignore -q "$f" 2>/dev/null; then
    ok "会被忽略: $f"
  else
    bad "未被忽略: $f"; gi_fail=1
  fi
done

# --------------------------------------------------------------------- 7
hr
echo "7. 必须入库的关键文件是否存在"
for f in README.md REPORT.md LICENSE .gitignore .gitattributes \
         AGENTS.md ARCHITECTURE.md TASK.md \
         docs/00-需求分析.md docs/04-交付说明.md docs/素材来源与授权.md; do
  if [ -f "$f" ]; then ok "存在: $f"; else bad "缺失: $f"; fi
done

# --------------------------------------------------------------------- 结果
echo "============================================================"
if [ "$fail" -eq 0 ]; then
  printf "${GRN}自查通过 — 可以提交${NC}\n"
else
  printf "${RED}自查未通过 — 请先处理上方 [!! ] 项${NC}\n"
  if [ "$FIX" -eq 1 ]; then
    echo ""
    echo "==> --fix: 尝试取消暂存违规文件"
    git ls-files | grep -E '^(runs/|assets/(source|character)/|tools/ffmpeg/)|\.(env|key|pem|mp4|mov)$' \
      | grep -v '\.gitkeep$' \
      | while read -r f; do
          echo "    git rm --cached $f"
          git rm --cached -q "$f" 2>/dev/null || true
        done
    echo "完成。请重新运行本脚本确认。"
  fi
fi
echo "============================================================"
exit "$fail"
