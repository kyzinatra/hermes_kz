#!/command/with-contenv sh
# Repair ownership of the host-mounted always-on memory before Hermes starts.
set -eu

# Upstream 01-hermes-setup may remap the account from the image default to the
# Compose HERMES_UID/HERMES_GID. Query the account after that step instead of
# guessing either value.
owner_uid="$(id -u hermes)"
owner_gid="$(id -g hermes)"

if [ -d /opt/data/memories ]; then
  chown -R "$owner_uid:$owner_gid" /opt/data/memories
  chmod -R u+rwX /opt/data/memories
  echo "writable-state: repaired /opt/data/memories ownership"
fi
