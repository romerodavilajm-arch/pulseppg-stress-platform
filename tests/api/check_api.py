"""Comprobaciones de la API para tests/api/e2e.sh.

Se ejecuta dentro del contenedor de la API (solo usa la biblioteca estándar):
    docker compose exec -T api python - <fase> [args] < tests/api/check_api.py

Fases:
    pending CODE                     la sesión existe y su análisis está pendiente
    results DEVICE RELAXED STRESSED NOISY FAILED_ID FAILED_CODE
                                     resultados, vistas de usuario y de administración
Cada argumento de sesión es CODE:ID. Sale con 1 al primer fallo.
"""

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
USER_METRICS = {"bpm", "sdnn", "rmssd", "pnn50", "lf_hf_ratio", "respiratory_rate"}
QUALITY = {"sqi", "perfusion_index", "snr_db"}


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def get_json(path, expected=200):
    status, body = get(path)
    check(status == expected, f"GET {path} -> {status}, se esperaba {expected}: {body[:200]}")
    return json.loads(body)


def check(cond, msg):
    if not cond:
        print(f"FALLO: {msg}")
        sys.exit(1)


def html(path, expected, present=(), absent=()):
    status, body = get(path)
    check(status == expected, f"GET {path} -> {status}, se esperaba {expected}")
    for text in present:
        check(text in body, f"GET {path} no contiene {text!r}")
    for text in absent:
        check(text not in body, f"GET {path} contiene {text!r} y no debería")


def split(arg):
    code, sid = arg.split(":")
    return code, int(sid)


def pending(code):
    s = get_json(f"/api/sessions/{code}")
    check(s["analysis"]["status"] == "pending", f"{code}: estado {s['analysis']['status']}, se esperaba pending")
    check(s["analysis"]["metrics"] is None, f"{code}: una sesión pendiente no debe tener métricas")
    html(f"/sesion/{code}", 200, ["pendiente"], ["Índice:"])
    print(f"ok  {code} pendiente (JSON y HTML)")


def completed(code, label):
    s = get_json(f"/api/sessions/{code}")
    a = s["analysis"]
    check(a["status"] == "completed" and a["label"] == label,
          f"{code}: {a['status']}/{a['label']}, se esperaba completed/{label}")
    check("device_key" not in s and "id" not in s, f"{code}: la vista pública expone datos internos")
    check(round(s["duration_seconds"]) == 300, f"{code}: duración {s['duration_seconds']}")
    return a


def results(device, relaxed, stressed, noisy, failed_id, failed_code):
    (rc, rid), (sc, sid), (nc, nid) = split(relaxed), split(stressed), split(noisy)
    failed_id = int(failed_id)

    for code, label in ((rc, "no_stress"), (sc, "stress")):
        a = completed(code, label)
        check(len(a["metrics"]) == 16, f"{code}: {len(a['metrics'])} métricas, se esperaban 16")
        check(a["score"] is not None and 0 <= a["score"] <= 1, f"{code}: score {a['score']}")
        text = "Sin estrés." if label == "no_stress" else "Estrés."
        html(f"/sesion/{code}", 200, [text, f"Índice: {round(a['score'] * 100)}/100", "SDNN", "Ratio LF/HF"])
        print(f"ok  {code} {label} con 16 métricas, índice {round(a['score'] * 100)}/100")

    a = completed(nc, "low_quality")
    check(a["metrics"] is None and a["score"] is None, f"{nc}: low_quality no debe tener métricas ni score")
    html(f"/sesion/{nc}", 200, ["La señal fue de baja calidad"], ["SDNN", "Índice:"])
    d = get_json(f"/api/admin/sessions/{nid}")
    check(set(d["analysis"]["metrics"]) <= QUALITY,
          f"admin {nid}: low_quality con métricas no de calidad {sorted(d['analysis']['metrics'])}")
    html(f"/admin/sesiones/{nid}", 200, ["Baja calidad", "snr_db"], ["rmssd", "lf_hf_ratio"])
    print(f"ok  {nc} low_quality sin métricas HRV (usuario y admin)")

    s = get_json(f"/api/sessions/{failed_code}")
    check(s["analysis"]["status"] == "failed", f"{failed_code}: estado {s['analysis']['status']}, se esperaba failed")
    html(f"/sesion/{failed_code}", 200, ["No se pudo analizar"])
    d = get_json(f"/api/admin/sessions/{failed_id}")
    check(d["analysis"].get("failure", {}).get("error"), f"admin {failed_id}: falta el error del Analyzer")
    print(f"ok  {failed_code} análisis fallido")

    # Código: normalización y no encontrado.
    check(get_json(f"/api/sessions/{rc.lower()}")["session_code"] == rc, "el código en minúsculas no se normaliza")
    get_json("/api/sessions/ZZZZZZ", 404)
    get_json("/api/sessions/no-valido", 404)
    html("/sesion/ZZZZZZ", 404, ["Código no encontrado"])
    html("/consulta?code=ZZZZZZ", 404, ["Código no encontrado"])
    status, _ = get(f"/consulta?code=+{rc.lower()}+")
    check(status == 200, f"/consulta con el código en minúsculas -> {status}")
    print("ok  código no encontrado y normalización")

    # Administración: filtros.
    def ids(query):
        return {r["id"] for r in get_json(f"/api/admin/sessions?{query}")["sessions"]}

    all_ids = ids("")
    check({rid, sid, nid, failed_id} <= all_ids, "la tabla global no contiene todas las sesiones de prueba")
    check(nid in ids("result=low_quality") and rid not in ids("result=low_quality"), "filtro result=low_quality")
    check(rid in ids("result=no_stress") and sid not in ids("result=no_stress"), "filtro result=no_stress")
    check(failed_id in ids("result=failed") and rid not in ids("result=failed"), "filtro result=failed")
    check(rid in ids(f"device={device}") and not ids("device=no-existe"), "filtro device")
    check(rid in ids("date_from=2000-01-01&date_to=2100-01-01") and not ids("date_to=2000-01-01"), "filtro de fechas")
    get_json("/api/admin/sessions?date_from=ayer", 400)
    get_json("/api/admin/sessions?result=otro", 400)
    get_json("/api/admin/sessions/999999999", 404)
    html(f"/admin?device={device}&result=no_stress", 200, [rc, "Sin estrés"], [nc])
    print("ok  tabla de administración y filtros")


if __name__ == "__main__":
    phase, *args = sys.argv[1:]
    {"pending": pending, "results": results}[phase](*args)
