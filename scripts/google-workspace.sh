#!/usr/bin/env bash
set -Eeuo pipefail

setup_script="/opt/data/skills/productivity/google-workspace/scripts/setup.py"
client_secret_host="credentials/google-client-secret.json"
client_secret_container="/credentials/google-client-secret.json"

dotenv_number() {
  local key="$1"
  local value=""

  if [[ -f .env ]]; then
    value="$(sed -n "s/^${key}=//p" .env | tail -n 1)"
    value="${value%$'\r'}"
  fi

  if [[ "${value}" =~ ^[0-9]+$ ]]; then
    printf '%s' "${value}"
  fi
}

hermes_uid="${HERMES_UID:-$(dotenv_number HERMES_UID)}"
hermes_gid="${HERMES_GID:-$(dotenv_number HERMES_GID)}"
hermes_uid="${hermes_uid:-1000}"
hermes_gid="${hermes_gid:-1000}"

prepare_client_secret() {
  local credentials_dir
  credentials_dir="$(dirname "${client_secret_host}")"

  if [[ ! -f "${client_secret_host}" ]]; then
    echo "Missing ${client_secret_host}" >&2
    exit 1
  fi

  if [[ "$(id -u)" -eq 0 ]]; then
    chown "${hermes_uid}:${hermes_gid}" \
      "${credentials_dir}" "${client_secret_host}"
    chmod 700 "${credentials_dir}"
    chmod 600 "${client_secret_host}"
  fi

  if ! docker compose run --rm --no-deps \
    --user "${hermes_uid}:${hermes_gid}" \
    --entrypoint /bin/sh hermes \
    -c "test -r '${client_secret_container}'" >/dev/null 2>&1; then
    cat >&2 <<EOF
${client_secret_host} is not readable by Hermes (${hermes_uid}:${hermes_gid}).
Run:
  sudo chown ${hermes_uid}:${hermes_gid} ${credentials_dir} ${client_secret_host}
  sudo chmod 700 ${credentials_dir}
  sudo chmod 600 ${client_secret_host}
EOF
    exit 1
  fi
}

usage() {
  echo "Usage: $0 {check|install-client|auth-url|auth-code <code-or-callback-url>|check-live|revoke}"
}

command_name="${1:-}"

case "${command_name}" in
  check)
    docker compose run --rm hermes python "${setup_script}" --check
    ;;
  install-client)
    prepare_client_secret
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
