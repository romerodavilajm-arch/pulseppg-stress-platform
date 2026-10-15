#!/usr/bin/env bash
# Prueba extremo a extremo del Analyzer HRV sin Raspberry Pi:
#   1. registra el dispositivo de prueba,
#   2. envía por MQTT tres sesiones simuladas de 5 minutos (relajada, con
#      estrés y con ruido) a través del Ingestor,
#   3. espera a que el Analyzer guarde sus resultados en el esquema hrv,
#   4. comprueba la etiqueta esperada de cada una y las métricas guardadas.
# Requiere los servicios arriba: docker compose up -d --wait
set -euo pipefail
cd "$(dirname "$0")/../.."

DEVICE="${DEVICE:-test-pi}"
TIMEOUT="${TIMEOUT:-90}"
psql() { docker compose exec -T db psql -U "${POSTGRES_USER:-pulseppg}" -d "${POSTGRES_DB:-pulseppg}" -v ON_ERROR_STOP=1 "$@"; }
fake_pi() { docker compose run --rm --no-deps -T -v ./tests:/tests ingestor python /tests/ingestor/fake_pi.py "$@" 2>/dev/null; }
session_id() { sed -n 's/.*"session_id": *\([0-9]*\).*/\1/p'; }

echo "== 1. Registro del dispositivo '$DEVICE'"
psql -qc "INSERT INTO devices (device_key) VALUES ('$DEVICE') ON CONFLICT (device_key) DO NOTHING"

echo "== 2. Envío de sesiones simuladas"
declare -A EXPECTED
for profile in relaxed stressed noisy; do
    ACK=$(fake_pi --device "$DEVICE" --profile "$profile")
    SID=$(echo "$ACK" | session_id)
    [ -n "$SID" ] || { echo "FALLO: sin ACK válido para $profile: $ACK"; exit 1; }
    echo "$profile -> sesión $SID"
    case $profile in
        relaxed)  EXPECTED[$SID]=no_stress ;;
        stressed) EXPECTED[$SID]=stress ;;
        noisy)    EXPECTED[$SID]=low_quality ;;
    esac
done
IDS=$(IFS=,; echo "${!EXPECTED[*]}")

echo "== 3. Esperando resultados del Analyzer (máx. ${TIMEOUT} s)"
for ((i = 0; i < TIMEOUT; i++)); do
    N=$(psql -tAc "SELECT count(*) FROM hrv.stress_results WHERE session_id IN ($IDS)" 2>/dev/null || echo 0)
    [ "$N" = "${#EXPECTED[@]}" ] && break
    sleep 1
done
[ "$N" = "${#EXPECTED[@]}" ] || { echo "FALLO: solo $N de ${#EXPECTED[@]} sesiones analizadas"; docker compose logs --tail 20 analyzer; exit 1; }

echo "== 4. Resultados"
psql -c "SELECT r.session_id, r.label, r.score, r.rules_met,
                round(max(m.value) FILTER (WHERE m.metric = 'bpm')::numeric, 1)         AS bpm,
                round(max(m.value) FILTER (WHERE m.metric = 'sdnn')::numeric, 1)        AS sdnn,
                round(max(m.value) FILTER (WHERE m.metric = 'rmssd')::numeric, 1)       AS rmssd,
                round(max(m.value) FILTER (WHERE m.metric = 'pnn50')::numeric, 1)       AS pnn50,
                round(max(m.value) FILTER (WHERE m.metric = 'lf_hf_ratio')::numeric, 2) AS lf_hf,
                round(max(m.value) FILTER (WHERE m.metric = 'sqi')::numeric, 2)         AS sqi,
                round(max(m.value) FILTER (WHERE m.metric = 'snr_db')::numeric, 1)      AS snr_db,
                count(m.metric) AS metrics
         FROM hrv.stress_results r LEFT JOIN hrv.metrics m USING (session_id)
         WHERE r.session_id IN ($IDS)
         GROUP BY 1, 2, 3, 4 ORDER BY 1"

for SID in "${!EXPECTED[@]}"; do
    GOT=$(psql -tAc "SELECT label FROM hrv.stress_results WHERE session_id = $SID")
    [ "$GOT" = "${EXPECTED[$SID]}" ] || { echo "FALLO: sesión $SID es '$GOT', se esperaba '${EXPECTED[$SID]}'"; exit 1; }
    if [ "$GOT" != low_quality ]; then
        N=$(psql -tAc "SELECT count(*) FROM hrv.metrics WHERE session_id = $SID")
        [ "$N" = 16 ] || { echo "FALLO: sesión $SID tiene $N métricas, se esperaban 16"; exit 1; }
    fi
done

echo "ANALYZER E2E OK"
