#!/usr/bin/env bash
# Prueba extremo a extremo de la API/web sin Raspberry Pi:
#   1. registra el dispositivo de prueba,
#   2. con el Analyzer parado, envía una sesión y comprueba que la API la da
#      por pendiente,
#   3. arranca el Analyzer, envía una sesión con estrés y otra con ruido, y
#      una sesión sin muestras que el Analyzer no puede analizar,
#   4. espera los resultados y comprueba las vistas de usuario y de
#      administración (tests/api/check_api.py).
# Requiere los servicios arriba: docker compose up -d --wait
set -euo pipefail
cd "$(dirname "$0")/../.."

DEVICE="${DEVICE:-test-pi}"
TIMEOUT="${TIMEOUT:-90}"
psql() { docker compose exec -T db psql -U "${POSTGRES_USER:-pulseppg}" -d "${POSTGRES_DB:-pulseppg}" -v ON_ERROR_STOP=1 "$@"; }
fake_pi() { docker compose run --rm --no-deps -T -v ./tests:/tests ingestor python /tests/ingestor/fake_pi.py "$@" 2>/dev/null; }
check_api() { docker compose exec -T api python - "$@" < tests/api/check_api.py; }
# "CODE:ID" a partir del ACK del Ingestor.
code_id() { sed -n 's/.*"session_id": *\([0-9]*\).*"session_code": *"\([A-Z0-9]*\)".*/\2:\1/p'; }
send() {
    local ack; ack=$(fake_pi --device "$DEVICE" --profile "$1")
    local cid; cid=$(echo "$ack" | code_id)
    [ -n "$cid" ] || { echo "FALLO: sin ACK válido para $1: $ack" >&2; exit 1; }
    echo "$cid"
}

echo "== 1. Registro del dispositivo '$DEVICE'"
psql -qc "INSERT INTO devices (device_key) VALUES ('$DEVICE') ON CONFLICT (device_key) DO NOTHING"

echo "== 2. Sesión pendiente (Analyzer parado)"
docker compose stop analyzer >/dev/null 2>&1
trap 'docker compose start analyzer >/dev/null 2>&1' EXIT
RELAXED=$(send relaxed)
echo "relaxed -> $RELAXED"
check_api pending "${RELAXED%%:*}"
docker compose start analyzer >/dev/null 2>&1
trap - EXIT

echo "== 3. Más sesiones"
STRESSED=$(send stressed); echo "stressed -> $STRESSED"
NOISY=$(send noisy); echo "noisy -> $NOISY"
# Sesión sin muestras: el Analyzer la registra como fallo permanente.
FAILED=$(psql -tAc "INSERT INTO sessions (device_id, started_at, finished_at, sample_count, file_hash)
                    SELECT id, now() - interval '5 minutes', now(), 0, md5(random()::text) || md5(random()::text)
                    FROM devices WHERE device_key = '$DEVICE'
                    RETURNING session_code || ':' || id" | head -1)
echo "sin muestras -> $FAILED"
docker compose exec -T mosquitto mosquitto_pub -q 1 -t "analysis/${FAILED##*:}/request" -m "{\"session_id\": ${FAILED##*:}}"

echo "== 4. Esperando al Analyzer (máx. ${TIMEOUT} s)"
IDS="${RELAXED##*:},${STRESSED##*:},${NOISY##*:}"
for ((i = 0; i < TIMEOUT; i++)); do
    N=$(psql -tAc "SELECT (SELECT count(*) FROM hrv.stress_results WHERE session_id IN ($IDS))
                        + (SELECT count(*) FROM hrv.failures WHERE session_id = ${FAILED##*:} AND permanent)" 2>/dev/null || echo 0)
    [ "$N" = 4 ] && break
    sleep 1
done
[ "$N" = 4 ] || { echo "FALLO: solo $N de 4 sesiones procesadas"; docker compose logs --tail 20 analyzer; exit 1; }

echo "== 5. Comprobaciones de la API"
check_api results "$DEVICE" "$RELAXED" "$STRESSED" "$NOISY" "${FAILED##*:}" "${FAILED%%:*}"

echo "API E2E OK (abre http://localhost:${API_PORT:-8000}/sesion/${RELAXED%%:*} y http://localhost:${API_PORT:-8000}/admin)"
