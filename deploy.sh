#!/usr/bin/env bash
# Tranchi engine — deploy script (added 2026-05-27; pattern from gotham/ppg).
#   Frontend: build → backup live dir → rsync --delete to /var/www/tranchi.
#   Backend:  EC2 git pull (origin main) + restart tranchi-api (port 8012).
# Safe by construction: refuses to deploy uncommitted/unpushed work and snapshots
# the live frontend before the destructive --delete. See ~/.claude/CLAUDE.md
# "Deploy & Backup Protocol".
#
# Usage:
#   ./deploy.sh [frontend|backend|all]            # default: all
#   ./deploy.sh --commit "msg" [frontend|backend|all]   # commit+push, then deploy
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EC2_SSH="ssh intelleq-ec2"
EC2_HOST="intelleq-ec2"
EC2_FRONTEND_DIR="/var/www/tranchi"
EC2_BACKEND_DIR="/home/ubuntu/tranchi-engine"
SVC="tranchi-api"

bold(){ printf '\033[1m%s\033[0m\n' "$1"; }
green(){ printf '\033[32m%s\033[0m\n' "$1"; }
red(){ printf '\033[31m%s\033[0m\n' "$1"; }

source "$HOME/.claude/scripts/deploy-guards.sh" || { echo "✗ missing deploy-guards.sh"; exit 1; }

# optional leading: --commit "msg"; remaining positional = mode
DG_COMMIT_MSG=""
if [[ "${1:-}" == "--commit" ]]; then DG_COMMIT_MSG="${2:-}"; shift 2 || true; fi
mode="${1:-all}"

deploy_frontend() {
  bold "==> Building frontend (npm run build)"
  ( cd "$REPO_ROOT/frontend" && npm run build )
  bold "==> Backup live frontend before --delete (keep 5)"
  dg_backup_remote "$EC2_SSH" "$EC2_FRONTEND_DIR"
  bold "==> rsync dist/ → $EC2_HOST:$EC2_FRONTEND_DIR"
  rsync -az --delete -e ssh "$REPO_ROOT/frontend/dist/" "$EC2_HOST:$EC2_FRONTEND_DIR/"
}

deploy_backend() {
  bold "==> EC2 git pull (origin main) + restart $SVC"
  $EC2_SSH "cd $EC2_BACKEND_DIR && git fetch origin && git reset --hard origin/main && sudo systemctl restart $SVC && sleep 2 && (curl -s 127.0.0.1:8012/health || true)"
}

# Gate first: deploy only a committed+pushed state (backend deploys via git pull,
# so unpushed work would silently NOT ship — the gate makes that impossible).
dg_git_gate "$REPO_ROOT" "$DG_COMMIT_MSG"

case "$mode" in
  frontend) deploy_frontend ;;
  backend)  deploy_backend ;;
  all)      deploy_backend; deploy_frontend ;;
  *) red "Unknown mode: $mode (expected: frontend|backend|all)"; exit 1 ;;
esac

green "==> Deploy complete"
bash "$HOME/.claude/scripts/sync-mirrors.sh" >/dev/null 2>&1 || true   # refresh local mirror
