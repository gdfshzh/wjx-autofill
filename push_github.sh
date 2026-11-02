#!/usr/bin/env bash
# 推送到 GitHub（SSH）
#
# 用法：
#   bash push_github.sh <GitHub用户名>              # 仓库名默认 wjx-autofill
#   bash push_github.sh <GitHub用户名> <仓库名>
#
# 前置：
#   1. ~/.ssh/github_wjx.pub 已加到 GitHub → Settings → SSH and GPG keys
#   2. GitHub 上已建同名空仓库（不要勾 README / .gitignore / license）
#
# 脚本会重置 origin 再推；重复执行不会产生多条 remote。

set -euo pipefail

USER_NAME="${1:-}"
REPO="${2:-wjx-autofill}"

if [ -z "$USER_NAME" ]; then
    echo "用法: bash push_github.sh <GitHub用户名> [仓库名]" >&2
    exit 1
fi

cd "$(dirname "$0")"

echo "[1/4] 检查 SSH 是否已被 GitHub 认可"
if ! ssh -o BatchMode=yes -o ConnectTimeout=10 -T git@github.com 2>&1 | grep -q "successfully authenticated"; then
    echo "  公钥未生效。先到 https://github.com/settings/ssh/new 粘贴：" >&2
    cat "$HOME/.ssh/github_wjx.pub" >&2
    exit 1
fi

echo "[2/4] 设置 origin"
git remote remove origin 2>/dev/null || true
git remote add origin "git@github.com:${USER_NAME}/${REPO}.git"
git remote -v

echo "[3/4] 推送 main"
git push -u origin main

echo "[4/4] 推送标签"
git push origin --tags

echo
echo "完成: https://github.com/${USER_NAME}/${REPO}"
