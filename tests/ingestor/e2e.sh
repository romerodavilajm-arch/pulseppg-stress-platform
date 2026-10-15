#!/usr/bin/env bash
# Prueba extremo a extremo del Ingestor sin Raspberry Pi:
#   1. registra el dispositivo de prueba en devices,
#   2. publica una sesión válida y espera el ACK con session_code,
#   3. comprueba en la BD la sesión y sus 15 000 muestras,
#   4. publica una sesión con hash incorrecto y espera ACK de error,
#   5. publica desde un dispositivo no registrado y espera ACK de error,
#   6. reenvía dos veces la misma sesión y comprueba que no se duplica.
# Requiere los servicios arriba: docker compose up -d --wait
set -euo pipefail
cd "$(dirname "$0")/../.."

DEVICE="${DEVICE:-test-pi}"
psql() { docker compose exec -T db psql -U "${POSTGRES_USER:-pulseppg}" -d "${POSTGRES_DB:-pulseppg}" -v ON_ERROR_STOP=1 "$@"; }
fake_pi() { docker compose run --rm --no-deps -T -v ./tests:/tests ingestor python /tests/ingestor/fake_pi.py "$@"; }

echo "== 1. Registro del dispositivo '$DEVICE'"
psql -qc "INSERT INTO devices (device_key) VALUES ('$DEVICE') ON CONFLICT (device_key) DO NOTHING"

echo "== 2. Sesión válida"
ACK=$(fake_pi --device "$DEVICE")
echo "ACK: $ACK"
CODE=$(echo "$ACK" | sed -n 's/.*"session_code": *"\([A-Z2-9]\{6\}\)".*/\1/p')
[ -n "$CODE" ] || { echo "FALLO: el ACK no trae session_code"; exit 1; }

echo "== 3. Verificación en la BD"
psql -v code="$CODE" < tests/ingestor/check_session.sql

echo "== 4. Hash incorrecto"
fake_pi --device "$DEVICE" --corrupt-hash --samples 100 --expect error

echo "== 5. Dispositivo no registrado"
fake_pi --device "no-registrado-$$" --samples 100 --expect error

echo "== 6. Reenvío de la misma sesión"
START=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
ACK1=$(fake_pi --device "$DEVICE" --samples 100 --start "$START")
ACK2=$(fake_pi --device "$DEVICE" --samples 100 --start "$START")
echo "ACK 1: $ACK1"
echo "ACK 2: $ACK2"
[ "$ACK1" = "$ACK2" ] || { echo "FALLO: el reenvío devolvió otro ACK"; exit 1; }
SID=$(echo "$ACK1" | sed -n 's/.*"session_id": *\([0-9]*\).*/\1/p')
N=$(psql -tAc "SELECT count(*) FROM sessions WHERE file_hash = (SELECT file_hash FROM sessions WHERE id = $SID)")
[ "$N" = 1 ] || { echo "FALLO: hay $N sesiones con el mismo archivo"; exit 1; }

echo "INGESTOR E2E OK (session_code=$CODE)"
