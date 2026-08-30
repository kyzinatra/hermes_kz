#!/command/with-contenv sh
# This image owns exactly one guarded gateway through Docker CMD.
#
# Upstream s6 normally restores per-profile gateway services from persistent
# gateway_state.json. Restoring one here would race the guarded CMD launcher:
# both processes bind the same API port and poll the same Telegram bot. Keep
# the dynamic scandir empty at boot; Docker Compose is the sole supervisor.
set -eu

echo "reconcile: skipped (single guarded gateway is owned by container CMD)"
exit 0
