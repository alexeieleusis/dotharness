#!/usr/bin/env bash

# Exit immediately if a command exits with a non-zero status
set -e

# --- Configurations ---
IMAGE_NAME="docker.io/library/sonarqube:community"
PROJECT_PREFIX="podman" # The prefix podman-compose adds to your volumes

# --- Color Helpers for Scannable Output ---
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

confirm_action() {
  read -p "Are you absolutely sure you want to proceed? (y/N): " -n 1 -r
  echo
  if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    log_warn "Action canceled by user."
    exit 0
  fi
}

usage() {
  echo "Usage: $0 [OPTION]"
  echo "Manage the SonarQube Podman stack with varying levels of cleanup."
  echo ""
  echo "Options (Pick only one):"
  echo "  (no flags)    Default: Quick restart with basic sanity checks"
  echo "  -i, --image   Level 1: Wipe local image layers and force re-pull"
  echo "  -v, --volume  Level 2: Wipe SonarQube application/cache volumes (DB preserved)"
  echo "  -r, --reset   Level 3: Full system reset (System caches + Podman engine reset)"
  echo "  -h, --help    Display this help menu"
  exit 1
}

# --- Action Functions ---

stack_down() {
  log_info "Taking down the podman-compose stack..."
  podman-compose down || true
  log_info "Pruning any leftover stopped container instances..."
  podman container prune -f
}

stack_up() {
  log_info "Bringing the stack back up..."
  podman-compose up -d
  log_info "Stack initialized! Check status with: podman-compose logs -f"
}

clean_images() {
  log_warn "Level 1: Purging local SonarQube image layers..."
  podman rmi -f "$IMAGE_NAME" || true
  log_info "Pulling a pristine copy of the image..."
  podman pull "$IMAGE_NAME"
}

clean_volumes() {
  log_warn "Level 2: Purging SonarQube named and anonymous data volumes..."

  # 1. Target known named volumes with project prefixes
  local target_volumes=("${PROJECT_PREFIX}_sonarqube_data" "${PROJECT_PREFIX}_sonarqube_extensions" "${PROJECT_PREFIX}_sonarqube_logs")
  for vol in "${target_volumes[@]}"; do
    if podman volume inspect "$vol" &>/dev/null; then
      podman volume rm "$vol" && log_info "Deleted volume: $vol"
    fi
  done

  # 2. Safely clear dangling hex-string anonymous volumes, protecting postgres
  local anon_vols=$(podman volume ls --format "{{.Name}}" | grep -E '^[a-f0-9]{64}$')
  if [ -not -z "$anon_vols" ]; then
    for vol in $anon_vols; do
      podman volume rm "$vol" && log_info "Deleted anonymous volume: $vol"
    done
  fi
  log_info "SonarQube application cache cleared. Database directory preserved."
}

system_reset() {
  log_error "CRITICAL: Preparing to execute a full system reset."
  log_warn "This will clear all local container states, images, and user-space storage caches."
  log_warn "Your local folder database files (./postgres_data) WILL NOT be deleted."
  confirm_action

  log_info "Stopping background Podman services..."
  systemctl --user stop podman.socket podman.service 2>/dev/null || true

  log_info "Purging OS level temporary cache directories..."
  rm -rf ~/.cache/containers ~/.cache/podman /tmp/podman* /tmp/containers*

  log_info "Resetting Podman storage backend..."
  podman system reset -f

  log_info "Re-pulling necessary base images..."
  podman pull "$IMAGE_NAME"
}

# --- Main CLI Router ---

MODE="default"

while [[ "$#" -gt 0 ]]; do
  case $1 in
  -i | --image)
    MODE="image"
    shift
    ;;
  -v | --volume)
    MODE="volume"
    shift
    ;;
  -r | --reset)
    MODE="reset"
    shift
    ;;
  -h | --help) usage ;;
  *)
    log_error "Unknown parameter passed: $1"
    usage
    ;;
  esac
done

case $MODE in
"default")
  log_info "Starting Standard Sanity Restart..."
  stack_down
  stack_up
  ;;
"image")
  stack_down
  clean_images
  stack_up
  ;;
"volume")
  stack_down
  clean_volumes
  stack_up
  ;;
"reset")
  stack_down
  system_reset
  stack_up
  ;;
esac
