#!/usr/bin/env bash
set -Eeuo pipefail

setup_script="/opt/data/plugins/yandex-mail/setup.py"
credentials_host="credentials/yandex-mail-oauth.json"
credentials_container="/credentials/yandex-mail-oauth.json"

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

prepare_credentials() {
  local credentials_dir
  credentials_dir="$(dirname "${credentials_host}")"

  if [[ ! -f "${credentials_host}" ]]; then
    cat >&2 <<EOF
Missing ${credentials_host}.
Copy credentials/yandex-mail-oauth.example.json and fill in the values first.
EOF
    exit 1
  fi

  if [[ "$(id -u)" -eq 0 ]]; then
    chown "${hermes_uid}:${hermes_gid}" \
      "${credentials_dir}" "${credentials_host}"
  fi

  if ! chmod 700 "${credentials_dir}" || \
    ! chmod 600 "${credentials_host}"; then
    echo "Could not secure ${credentials_host} with permissions 600." >&2
    exit 1
  fi

  if ! docker compose run --rm --no-deps \
    --user "${hermes_uid}:${hermes_gid}" \
    --entrypoint /bin/sh hermes \
    -c "test -r '${credentials_container}'" >/dev/null 2>&1; then
    cat >&2 <<EOF
${credentials_host} is not readable by Hermes (${hermes_uid}:${hermes_gid}).
Run:
  sudo chown ${hermes_uid}:${hermes_gid} ${credentials_dir} ${credentials_host}
  sudo chmod 700 ${credentials_dir}
  sudo chmod 600 ${credentials_host}
EOF
    exit 1
  fi
}

run_setup() {
  docker compose run --rm --no-deps hermes \
    python "${setup_script}" "$@"
}

usage() {
  echo "Usage: $0 {status|auth-url|auth-code|check-live}"
}

command_name="${1:-}"

case "${command_name}" in
  status|auth-url|check-live)
    prepare_credentials
    run_setup "${command_name}"
    ;;
  auth-code)
    if [[ $# -ne 1 ]]; then
      usage >&2
      exit 2
    fi
    prepare_credentials
    run_setup auth-code
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
