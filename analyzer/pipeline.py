"""Pipeline ETL del Analyzer HRV (docs/flujo.md §5, fases 2 y 3).

Recibe las muestras de una sesión (DataFrame con columnas time, ir, red,
ordenado por tiempo) y devuelve las métricas y la clasificación. No toca la
base de datos ni MQTT: es una función pura, fácil de probar por separado.

Pasos sobre la señal completa (sin ventanas):
  1. Limpieza del canal IR: Butterworth paso banda de orden 4 (0.5-5 Hz),
     recorte de outliers por rango intercuartílico y normalización z.
  2. Picos y métricas temporales con HeartPy (bpm, sdnn, rmssd, pnn50,
     nn50, ibi_mean).
  3. Métricas frecuenciales con Welch sobre la serie RR interpolada a 4 Hz
     (lf, hf, total_power, lf_hf_ratio, lf_nu, hf_nu). Sin VLF.
  4. Calidad global: sqi, perfusion_index y snr_db. Si alguna queda por
     debajo de su umbral, la sesión es low_quality y no se clasifica.
  5. Frecuencia respiratoria (RIIV): banda 0.15-0.5 Hz del IR crudo y FFT.
  6. Clasificación por las cuatro reglas fisiológicas.
"""

import os
import warnings

import numpy as np
from scipy import interpolate, signal

with warnings.catch_warnings():
    # HeartPy 1.2.7 importa pkg_resources, que avisa de su obsolescencia.
    warnings.simplefilter("ignore", UserWarning)
    import heartpy as hp

ANALYZER = "hrv"

# Duración mínima para intentar el análisis. La captura normal es de 5 min.
MIN_SECONDS = 60

# Umbrales de calidad. docs/flujo.md no fija valores; estos son provisionales
# y se pueden ajustar por entorno al calibrar con capturas reales del MAX30102.
#   SQI: correlación media de cada latido con el latido promedio (Orphanidou
#        et al., 2015, proponen 0.86 como aceptable).
#   PI:  amplitud pulsátil / componente continua del IR, en %.
#   SNR: potencia en el pulso y sus armónicos frente al resto de 0.5-5 Hz.
SQI_MIN = float(os.environ.get("HRV_SQI_MIN", "0.86"))
PI_MIN = float(os.environ.get("HRV_PI_MIN", "0.1"))
SNR_MIN_DB = float(os.environ.get("HRV_SNR_MIN_DB", "0"))

# Reglas fisiológicas (docs/flujo.md §5.8): cada una que se cumple suma 1.
RULES = (
    ("lf_hf_ratio", ">", 2.0),   # predominancia simpática
    ("sdnn", "<", 30.0),         # variabilidad reducida (ms)
    ("rmssd", "<", 20.0),        # actividad parasimpática baja (ms)
    ("pnn50", "<", 5.0),         # baja variabilidad latido a latido (%)
)

LF_BAND = (0.04, 0.15)
HF_BAND = (0.15, 0.40)
RR_FS = 4.0  # Hz de la serie RR interpolada


class AnalysisError(Exception):
    """La sesión no se puede analizar (no es un problema transitorio)."""


# ------------------------------------------------------------
# Utilidades
# ------------------------------------------------------------

def sample_rate(times):
    """Frecuencia de muestreo a partir de las marcas de tiempo."""
    dt = times.diff().dt.total_seconds().to_numpy()
    dt = dt[dt > 0]
    if len(dt) == 0:
        raise AnalysisError("marcas de tiempo inválidas")
    return 1.0 / float(np.median(dt))


def bandpass(x, fs, low, high, order=4):
    sos = signal.butter(order, [low, high], btype="bandpass", fs=fs, output="sos")
    return signal.sosfiltfilt(sos, x)


def band_power(freqs, psd, low, high):
    mask = (freqs >= low) & (freqs < high)
    return float(np.trapezoid(psd[mask], freqs[mask]))


def _finite(value):
    return value is not None and np.isfinite(value)


# ------------------------------------------------------------
# Fase 2: transformación
# ------------------------------------------------------------

def clean(ir, fs):
    """Filtrado paso banda, recorte IQR y normalización z del canal IR."""
    x = bandpass(ir, fs, 0.5, 5.0)
    q1, q3 = np.percentile(x, [25, 75])
    iqr = q3 - q1
    # 3 x IQR ("outliers lejanos" de Tukey): recorta artefactos sin aplanar
    # los picos sistólicos normales, que son asimétricos.
    x = np.clip(x, q1 - 3 * iqr, q3 + 3 * iqr)
    std = x.std()
    # Una señal plana (sin dedo) no tiene pulso: el control de calidad la
    # marcará low_quality.
    return (x - x.mean()) / std if std > 0 else x - x.mean()


def time_domain(x, fs):
    """Detección de picos y métricas temporales con HeartPy."""
    wd, m = hp.process(hp.scale_data(x), fs, calc_freq=False,
                       high_precision=True, high_precision_fs=1000.0)
    rr = np.asarray(wd["RR_list_cor"], dtype=float)
    if len(rr) < 10:
        raise hp.exceptions.BadSignalWarning("muy pocos latidos válidos")
    diffs = np.abs(np.diff(rr))
    metrics = {
        "bpm": float(m["bpm"]),
        "ibi_mean": float(m["ibi"]),
        "sdnn": float(m["sdnn"]),
        "rmssd": float(m["rmssd"]),
        "nn50": float(np.sum(diffs > 50)),
        "pnn50": float(100.0 * np.mean(diffs > 50)) if len(diffs) else None,
    }
    return wd, metrics


def frequency_domain(wd, fs):
    """LF, HF y derivadas con Welch sobre la serie RR interpolada a 4 Hz."""
    peaks = np.asarray(wd["peaklist"], dtype=float)
    rr = np.asarray(wd["RR_list"], dtype=float)
    mask = np.asarray(wd["RR_masklist"], dtype=bool)
    # RR_list[i] es el intervalo que termina en peaklist[i + 1].
    t = peaks[1:len(rr) + 1] / fs
    t, rr = t[~mask], rr[~mask]
    if len(rr) < 10:
        return {}

    grid = np.arange(t[0], t[-1], 1 / RR_FS)
    rr_i = interpolate.CubicSpline(t, rr)(grid)
    freqs, psd = signal.welch(rr_i, fs=RR_FS, nperseg=min(256, len(rr_i)), detrend="linear")

    lf = band_power(freqs, psd, *LF_BAND)
    hf = band_power(freqs, psd, *HF_BAND)
    total = lf + hf  # sin VLF (§5.6), la potencia total es LF + HF
    out = {"lf": lf, "hf": hf, "total_power": total}
    if hf > 0:
        out["lf_hf_ratio"] = lf / hf
    if total > 0:
        out["lf_nu"] = 100.0 * lf / total
        out["hf_nu"] = 100.0 * hf / total
    return out


def signal_quality(ir, x, fs, wd, bpm):
    """SQI, índice de perfusión y SNR sobre la señal completa."""
    out = {}

    # PI: amplitud pulsátil robusta (p95 - p5 del IR filtrado) / media del IR.
    ac = bandpass(ir, fs, 0.5, 5.0)
    dc = float(np.mean(ir))
    if dc > 0:
        out["perfusion_index"] = 100.0 * float(np.percentile(ac, 95) - np.percentile(ac, 5)) / dc

    # SNR: potencia en ±0.2 Hz alrededor de cada armónico del pulso frente
    # al resto de la banda 0.5-5 Hz.
    freqs, psd = signal.welch(x, fs=fs, nperseg=min(len(x), int(20 * fs)))
    band = (freqs >= 0.5) & (freqs <= 5.0)
    if _finite(bpm) and bpm > 0:
        f0 = bpm / 60.0
    else:
        f0 = float(freqs[band][np.argmax(psd[band])])
    harmonic = np.zeros_like(freqs, dtype=bool)
    for k in range(1, int(5.0 / f0) + 1):
        harmonic |= np.abs(freqs - k * f0) <= 0.2
    sig = float(np.sum(psd[band & harmonic]))
    noise = float(np.sum(psd[band & ~harmonic]))
    if sig > 0 and noise > 0:
        out["snr_db"] = 10.0 * np.log10(sig / noise)

    # SQI: correlación media de cada latido con el latido promedio.
    if wd is not None:
        peaks = np.round(np.asarray(wd["peaklist"], dtype=float)).astype(int)
        rejected = set(np.round(np.asarray(wd["removed_beats"], dtype=float)).astype(int))
        half = int(0.5 * np.median(np.diff(peaks)))
        beats = np.array([x[p - half:p + half] for p in peaks
                          if p not in rejected and p - half >= 0 and p + half <= len(x)])
        if len(beats) >= 10:
            template = beats.mean(axis=0)
            corr = [np.corrcoef(b, template)[0, 1] for b in beats]
            # Los latidos que HeartPy rechazó cuentan como correlación 0.
            n_total = len(beats) + len(rejected)
            out["sqi"] = float(np.nansum(corr) / n_total)
        else:
            out["sqi"] = 0.0
    else:
        out["sqi"] = 0.0
    return out


def respiratory_rate(ir, fs):
    """Respiraciones por minuto: pico del espectro del IR en 0.15-0.5 Hz."""
    x = bandpass(ir - np.mean(ir), fs, 0.15, 0.5, order=2)
    x = x * np.hanning(len(x))
    spectrum = np.abs(np.fft.rfft(x))
    freqs = np.fft.rfftfreq(len(x), d=1 / fs)
    band = (freqs >= 0.15) & (freqs <= 0.5)
    if not band.any() or spectrum[band].max() == 0:
        return None
    return 60.0 * float(freqs[band][np.argmax(spectrum[band])])


# ------------------------------------------------------------
# Fase 3: análisis
# ------------------------------------------------------------

def quality_ok(metrics):
    return (metrics.get("sqi", 0) >= SQI_MIN
            and metrics.get("perfusion_index", 0) >= PI_MIN
            and metrics.get("snr_db", -np.inf) >= SNR_MIN_DB)


def classify(metrics):
    """Devuelve (label, score, reglas_cumplidas) según §5.8."""
    if not quality_ok(metrics):
        return "low_quality", None, None
    if not all(_finite(metrics.get(name)) for name, _, _ in RULES):
        # Sin las cuatro métricas no se puede aplicar la regla completa.
        return "low_quality", None, None
    met = 0
    for name, op, threshold in RULES:
        value = metrics[name]
        met += value > threshold if op == ">" else value < threshold
    score = met / len(RULES)
    label = "stress" if met >= 3 else "no_stress"
    return label, score, met


def analyze(df):
    """Pipeline completo. df: columnas time, ir, red ordenadas por time.

    Devuelve {"metrics": {nombre: valor}, "label", "score", "rules_met"}.
    Las métricas que no se pudieron calcular se omiten.
    """
    if len(df) < 2:
        raise AnalysisError("la sesión no tiene muestras")
    fs = sample_rate(df["time"])
    if len(df) / fs < MIN_SECONDS:
        raise AnalysisError(f"señal demasiado corta: {len(df) / fs:.0f} s (mínimo {MIN_SECONDS} s)")

    ir = df["ir"].to_numpy(dtype=float)
    x = clean(ir, fs)

    metrics = {}
    wd = None
    try:
        wd, td = time_domain(x, fs)
        metrics.update(td)
        metrics.update(frequency_domain(wd, fs))
    except (hp.exceptions.BadSignalWarning, ValueError, IndexError, ZeroDivisionError):
        # HeartPy no encontró un pulso utilizable: solo quedan métricas de calidad.
        wd = None
    metrics.update(signal_quality(ir, x, fs, wd, metrics.get("bpm")))
    rr = respiratory_rate(ir, fs)
    if rr is not None:
        metrics["respiratory_rate"] = rr

    metrics = {k: float(v) for k, v in metrics.items() if _finite(v)}
    label, score, met = classify(metrics)
    return {"metrics": metrics, "label": label, "score": score, "rules_met": met}
