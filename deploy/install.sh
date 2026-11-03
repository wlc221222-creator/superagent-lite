#!/usr/bin/env bash
# Install (or upgrade) superagent-lite as two systemd services.
#
# Idempotent: running it twice re-copies the units and restarts the services;
# it never overwrites an existing config.json, runtime data or env files.
#
#   sudo ./deploy/install.sh                       # defaults below
#   sudo PREFIX=/srv/superagent ./deploy/install.sh
#
# Environment overrides:
#   PREFIX         project install directory        (default /opt/superagent-lite)
#   SERVICE_USER   user the services run as         (default: owner of SUDO_USER
#                                                    when invoked via sudo, else
#                                                    the current user, else root)
#   SYSTEMD_DIR    unit directory                   (default /etc/systemd/system)
#   ENV_DIR        EnvironmentFile directory        (default /etc/superagent)
#   NO_START=1     install and enable, but do not start
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

PREFIX="${PREFIX:-/opt/superagent-lite}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
ENV_DIR="${ENV_DIR:-/etc/superagent}"
NO_START="${NO_START:-0}"
UNITS=(superagentd.service superagent-http.service)

die() { echo "install.sh: error: $*" >&2; exit 1; }
say() { echo "install.sh: $*"; }

# --- pre-flight checks ------------------------------------------------------
command -v systemctl >/dev/null 2>&1 || die "systemctl not found; this script targets systemd hosts"
command -v python3 >/dev/null 2>&1 || die "python3 not found"
PY_OK="$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)')" \
  || die "python3 is not runnable"
[ "${PY_OK}" = "1" ] || die "Python 3.11 or newer is required"
[ -f "${PROJECT_DIR}/superagent_lite/__main__.py" ] || die "not a superagent-lite checkout: ${PROJECT_DIR}"
for unit in "${UNITS[@]}"; do
  [ -f "${PROJECT_DIR}/deploy/${unit}" ] || die "missing unit file: deploy/${unit}"
done
[ "$(id -u)" = "0" ] || die "run as root (sudo $0)"

if [ -z "${SERVICE_USER:-}" ]; then
  if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
    SERVICE_USER="${SUDO_USER}"
  else
    SERVICE_USER="$(id -un)"
  fi
fi
id "${SERVICE_USER}" >/dev/null 2>&1 || die "user does not exist: ${SERVICE_USER}"

say "project:   ${PROJECT_DIR}"
say "install to:${PREFIX}"
say "run as:    ${SERVICE_USER}"

# --- copy the project -------------------------------------------------------
if [ "${PROJECT_DIR}" != "$(cd "${PREFIX}" 2>/dev/null && pwd || true)" ]; then
  mkdir -p "${PREFIX}"
  say "copying project files"
  # Copy source and deploy material; keep any config.json / runtime / .git on
  # the target so an upgrade never discards state.
  for item in superagent_lite tests deploy README.md pyproject.toml main.py; do
    [ -e "${PROJECT_DIR}/${item}" ] || continue
    rm -rf "${PREFIX:?}/${item}"
    cp -R "${PROJECT_DIR}/${item}" "${PREFIX}/${item}"
  done
fi
if [ ! -f "${PREFIX}/config.json" ]; then
  if [ -f "${PROJECT_DIR}/config.json" ]; then
    cp "${PROJECT_DIR}/config.json" "${PREFIX}/config.json"
    say "copied config.json"
  elif [ -f "${PREFIX}/config.example.json" ] || [ -f "${PROJECT_DIR}/config.example.json" ]; then
    cp "${PREFIX}/config.example.json" "${PREFIX}/config.json" 2>/dev/null \
      || cp "${PROJECT_DIR}/config.example.json" "${PREFIX}/config.json"
    say "created config.json from config.example.json -- review it"
  else
    say "no config.json; built-in defaults will be used"
  fi
fi
mkdir -p "${PREFIX}/runtime"
chown -R "${SERVICE_USER}:" "${PREFIX}/runtime" "${PREFIX}/config.json" 2>/dev/null || true
chown -R "${SERVICE_USER}:" "${PREFIX}/superagent_lite" "${PREFIX}/tests" "${PREFIX}/deploy" 2>/dev/null || true

# --- env files --------------------------------------------------------------
mkdir -p "${ENV_DIR}"
chmod 700 "${ENV_DIR}"
for name in superagentd.env superagent-http.env; do
  target="${ENV_DIR}/${name}"
  if [ ! -e "${target}" ]; then
    {
      echo "# superagent-lite environment: ${name}"
      echo "# Created by deploy/install.sh. Keep this file private (chmod 600)."
      if [ "${name}" = "superagent-http.env" ]; then
        echo "# A strong random token is required for any non-loopback bind."
        echo "# SUPERAGENT_HTTP_TOKEN="
        echo "# Feishu channel (optional; leave unset to log replies only):"
        echo "# FEISHU_APP_ID="
        echo "# FEISHU_APP_SECRET="
        echo "# FEISHU_VERIFICATION_TOKEN="
      else
        echo "# The daemon needs no HTTP token. Add per-run variables here if needed."
      fi
    } >"${target}"
    chmod 600 "${target}"
    say "created ${target} (edit it to add secrets)"
  else
    chmod 600 "${target}"
    say "keeping existing ${target}"
  fi
done

# --- units ------------------------------------------------------------------
for unit in "${UNITS[@]}"; do
  source_unit="${PROJECT_DIR}/deploy/${unit}"
  target_unit="${SYSTEMD_DIR}/${unit}"
  say "installing ${target_unit}"
  # Point WorkingDirectory/ExecStart at PREFIX and run as SERVICE_USER.
  sed -e "s#^WorkingDirectory=.*#WorkingDirectory=${PREFIX}#" \
      -e "s#--config /opt/superagent-lite/config.json#--config ${PREFIX}/config.json#" \
      -e "/^\[Service\]/a User=${SERVICE_USER}\nGroup=$(id -gn "${SERVICE_USER}")" \
      "${source_unit}" >"${target_unit}"
  chmod 644 "${target_unit}"
done

systemctl daemon-reload
systemctl enable "${UNITS[@]}"

if [ "${NO_START}" = "1" ]; then
  say "NO_START=1 set; services enabled but not started"
  exit 0
fi
systemctl restart "${UNITS[@]}"
sleep 1
for unit in "${UNITS[@]}"; do
  if systemctl is-active --quiet "${unit}"; then
    say "${unit}: active"
  else
    say "${unit}: NOT active -- inspect with: journalctl -u ${unit} -n 50"
    exit 1
  fi
done
say "done. Web UI: http://127.0.0.1:8765/ (set http_host/http_port in ${PREFIX}/config.json)"
