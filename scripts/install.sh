#!/usr/bin/env bash
# Install / remove the user units for tuya-monitor. No root needed.
#   ./scripts/install.sh            install and start
#   ./scripts/install.sh --dry-run  show what would change
#   ./scripts/install.sh --uninstall
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
DRY=0
REMOVE=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --uninstall) REMOVE=1 ;;
    *) echo "unknown flag $arg"; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
run() { if [ "$DRY" = 1 ]; then say "  would run: $*"; else "$@"; fi; }

render() { # $1 = name, $2 = subcommand, $3 = description
  cat <<UNIT
[Unit]
Description=tuya-monitor $3
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
Environment=PYTHONUNBUFFERED=1
ExecStart=$ROOT/.venv/bin/python -m monitor.cli $2
Restart=on-failure
RestartSec=10
Slice=tuya-monitor.slice

[Install]
WantedBy=default.target
UNIT
}

render_weather() {
  cat <<UNIT
[Unit]
Description=tuya-monitor outside conditions (backfill and current hour)

[Service]
Type=oneshot
WorkingDirectory=$ROOT
Environment=PYTHONUNBUFFERED=1
ExecStart=$ROOT/.venv/bin/python -m monitor.cli weather --days ${WEATHER_BACKFILL:-14}
Slice=tuya-monitor.slice
UNIT
}

render_weather_timer() {
  cat <<UNIT
[Unit]
Description=tuya-monitor outside conditions every ten minutes

[Timer]
OnBootSec=60
OnUnitActiveSec=600
AccuracySec=30

[Install]
WantedBy=timers.target
UNIT
}

mkdir -p "$UNIT_DIR"

if [ "$REMOVE" = 1 ]; then
  for u in api collect weather; do
    if [ -f "$UNIT_DIR/tuya-monitor-$u.service" ]; then
      run systemctl --user disable --now "tuya-monitor-$u.service" || true
      run rm -f "$UNIT_DIR/tuya-monitor-$u.service" "$UNIT_DIR/tuya-monitor-$u.timer"
    fi
  done
  run systemctl --user daemon-reload
  say "removed"
  exit 0
fi

for pair in "api:serve:HTTP API" "collect:collect:pollers"; do
  name="${pair%%:*}"; rest="${pair#*:}"; sub="${rest%%:*}"; desc="${rest#*:}"
  if [ "$DRY" = 1 ]; then
    say "--- $UNIT_DIR/tuya-monitor-$name.service"
    render "$name" "$sub" "$desc"
  else
    render "$name" "$sub" "$desc" > "$UNIT_DIR/tuya-monitor-$name.service"
  fi
done

if [ "$DRY" = 1 ]; then
  say "--- $UNIT_DIR/tuya-monitor-weather.service"; render_weather
  say "--- $UNIT_DIR/tuya-monitor-weather.timer"; render_weather_timer
else
  render_weather > "$UNIT_DIR/tuya-monitor-weather.service"
  render_weather_timer > "$UNIT_DIR/tuya-monitor-weather.timer"
fi

run systemctl --user daemon-reload
run systemctl --user enable --now tuya-monitor-api.service tuya-monitor-collect.service \
  tuya-monitor-weather.timer
say "installed: api on 127.0.0.1:8765, collector polling enabled devices"
say "logs: journalctl --user -u tuya-monitor-collect -f"
say "outside weather: journalctl --user -u tuya-monitor-weather -f"
