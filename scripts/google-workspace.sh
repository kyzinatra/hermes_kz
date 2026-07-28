#!/usr/bin/env bash
set -Eeuo pipefail

setup_script="/opt/data/skills/productivity/google-workspace/scripts/setup.py"
client_secret_host="credentials/google-client-secret.json"
client_secret_container="/credentials/google-client-secret.json"

usage() {
  echo "Usage: $0 {check|install-client|auth-url|auth-code <code-or-callback-url>|check-live|revoke}"
}

command_name="${1:-}"

case "${command_name}" in
  check)
    docker compose run --rm hermes python "${setup_script}" --check
    ;;
  install-client)
    if [[ ! -f "${client_secret_host}" ]]; then
      echo "Missing ${client_secret_host}" >&2
      exit 1
    fi
    docker compose run --rm hermes python "${setup_script}" \
      --client-secret "${client_secret_container}"
    ;;
  auth-url)
    docker compose run --rm hermes python "${setup_script}" --auth-url
    ;;
  auth-code)
    if [[ $# -ne 2 ]]; then
      usage >&2
      exit 2
    fi
    docker compose run --rm hermes python "${setup_script}" --auth-code "$2"
    ;;
  check-live)
    docker compose run --rm hermes python "${setup_script}" --check-live
    ;;
  revoke)
    docker compose run --rm hermes python "${setup_script}" --revoke
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
