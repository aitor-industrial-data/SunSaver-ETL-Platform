import pandas as pd
import pvlib
import numpy as np

from logger_config import setup_logging


logger = setup_logging()


# ─────────────────────────────────────────────────────────────────────────────
# SOLAR POSITION
# ─────────────────────────────────────────────────────────────────────────────

def calculate_solar_position(latitude, longitude, forecast_time_utc):
    """
    Computes solar elevation (alfa) and azimuth using pvlib.
    Returns (0.0, 0.0) on any parsing or computation error.
    """
    try:
        dt      = pd.to_datetime(forecast_time_utc, utc=True)
        sol_pos = pvlib.solarposition.get_solarposition(dt, latitude, longitude)
        alfa    = float(sol_pos["elevation"].values[0])
        azimuth = float(sol_pos["azimuth"].values[0])
        return alfa, azimuth

    except Exception as exc:
        logger.warning(
            "[ENGINE] Solar position failed for t=%s (lat=%.4f, lon=%.4f): %s — defaulting to (0, 0)",
            forecast_time_utc, latitude, longitude, exc,
        )
        return 0.0, 0.0


# ─────────────────────────────────────────────────────────────────────────────
# IRRADIANCE — GHI
# ─────────────────────────────────────────────────────────────────────────────

def calculate_ghi(alfa, clouds_pct, weather_id):
    """
    Estimates Global Horizontal Irradiance (GHI) using the Haurwitz clear-sky
    model attenuated by the Kasten-Czeplak cloud factor and a weather-type
    transmittance coefficient.  Returns 0.0 when the sun is below the horizon.
    """
    try:
        if alfa <= 0:
            return 0.0

        weather_factors = {
            "thunderstorm":  0.40,
            "drizzle":       0.85,
            "rain":          0.70,
            "snow":          0.75,
            "atmosphere":    0.60,
            "clear":         1.00,
            "clouds_light":  0.95,
            "clouds_heavy":  0.80,
        }

        wid = int(weather_id)
        if   wid < 300: f_w = weather_factors["thunderstorm"]
        elif wid < 500: f_w = weather_factors["drizzle"]
        elif wid < 600: f_w = weather_factors["rain"]
        elif wid < 700: f_w = weather_factors["snow"]
        elif wid < 800: f_w = weather_factors["atmosphere"]
        elif wid == 800: f_w = weather_factors["clear"]
        elif wid <= 802: f_w = weather_factors["clouds_light"]
        else:            f_w = weather_factors["clouds_heavy"]

        sin_alfa = np.sin(np.radians(alfa))
        g_clear  = 1098 * sin_alfa * np.exp(-0.057 / max(0.001, sin_alfa))
        ghi      = g_clear * (1 - 0.75 * (clouds_pct / 100) ** 3) * f_w

        return float(max(0, ghi))

    except Exception as exc:
        logger.warning("[ENGINE] GHI calculation failed (alfa=%.2f, clouds=%s, wid=%s): %s", alfa, clouds_pct, weather_id, exc)
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# IRRADIANCE — ERBS DECOMPOSITION
# ─────────────────────────────────────────────────────────────────────────────

def decompose_erbs(ghi, alfa, forecast_time_utc):
    """
    Decomposes GHI into Direct Normal Irradiance (DNI) and Diffuse Horizontal
    Irradiance (DHI) using the Erbs clearness-index model.
    Returns (0.0, 0.0) when the sun is too low or GHI is zero.
    """
    try:
        if ghi <= 0 or alfa < 2:
            return 0.0, 0.0

        dni_extra  = pvlib.irradiance.get_extra_radiation(pd.to_datetime(forecast_time_utc))
        sin_alfa   = np.sin(np.radians(alfa))
        ghi_extra_h = dni_extra * sin_alfa
        kt          = ghi / ghi_extra_h if ghi_extra_h > 0 else 0
        kt          = max(0, min(kt, 1))

        if kt <= 0.22:
            diff_frac = 1.0 - 0.09 * kt
        elif kt <= 0.80:
            diff_frac = 0.9511 - 0.1604 * kt + 4.388 * kt**2 - 16.638 * kt**3 + 12.336 * kt**4
        else:
            diff_frac = 0.165

        dhi = ghi * diff_frac
        dni = (ghi - dhi) / sin_alfa if sin_alfa > 0.01 else 0
        dni = min(dni, dni_extra)

        return float(max(0, dni)), float(max(0, dhi))

    except Exception as exc:
        logger.warning("[ENGINE] Erbs decomposition failed for t=%s: %s", forecast_time_utc, exc)
        return 0.0, 0.0


# ─────────────────────────────────────────────────────────────────────────────
# IRRADIANCE — PLANE OF ARRAY (POA)
# ─────────────────────────────────────────────────────────────────────────────

def calculate_total_poa(dni, dhi, ghi, alfa, azimuth_solar, angle, aspect):
    """
    Calculates total irradiance on the tilted panel plane (POA) by summing
    beam, isotropic-diffuse (Liu-Jordan) and ground-albedo components.
    """
    try:
        if ghi <= 0 or alfa < 2:
            return 0.0

        zenith   = 90 - alfa
        theta    = pvlib.irradiance.aoi(angle, aspect, zenith, azimuth_solar)
        g_beam   = max(0, dni * np.cos(np.radians(theta)))
        g_diff   = dhi * (1 + np.cos(np.radians(angle))) / 2
        g_albedo = ghi * 0.2 * (1 - np.cos(np.radians(angle))) / 2
        poa      = g_beam + g_diff + g_albedo

        return float(max(0, poa))

    except Exception as exc:
        logger.warning("[ENGINE] POA calculation failed (alfa=%.2f, angle=%s, aspect=%s): %s", alfa, angle, aspect, exc)
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# CELL TEMPERATURE — FAIMAN MODEL
# ─────────────────────────────────────────────────────────────────────────────

def calculate_t_cell(temp_ambient, wind_speed, poa):
    """
    Estimates cell operating temperature using the Faiman model.
    Falls back to ambient temperature when POA is zero.
    """
    u0, u1 = 24.9, 6.1   # Faiman coefficients for standard open-rack mounting

    try:
        if poa <= 0:
            return float(temp_ambient)

        divisor = u0 + u1 * max(0, wind_speed)
        t_cell  = temp_ambient + (poa / divisor)
        return float(t_cell)

    except Exception as exc:
        logger.warning("[ENGINE] T_cell calculation failed — using ambient temperature: %s", exc)
        return float(temp_ambient)


# ─────────────────────────────────────────────────────────────────────────────
# POWER OUTPUT
# ─────────────────────────────────────────────────────────────────────────────

def calculate_power_output(poa, t_cell, peak_power, loss_pct):
    """
    Calculates AC power output (kW) and Performance Ratio (PR) applying
    thermal derating (γ = -0.4 %/°C) and system losses.
    """
    try:
        if poa <= 0:
            return 0.0, 0.0

        gamma  = -0.004                          # Thermal coefficient: -0.4 %/°C
        f_temp = 1 + gamma * (t_cell - 25)
        pr     = f_temp * (1 - (loss_pct / 100))
        p_out  = (poa / 1000) * peak_power * pr

        return float(max(0, p_out)), float(pr)

    except Exception as exc:
        logger.warning("[ENGINE] Power output calculation failed (poa=%.2f, t_cell=%.2f): %s", poa, t_cell, exc)
        return 0.0, 0.0


import hashlib
import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
# PERFIL DE CARGA — breakpoints (hora, fracción_nominal)
# ─────────────────────────────────────────────────────────────────────────────
#
# Cada entrada define un nodo del perfil de carga.
# Entre nodos se interpola linealmente → sin escalones abruptos.
# La hora 24 cierra el día con el mismo valor que la hora 0 del día siguiente
# (ambos son 0.10 en laboral), garantizando continuidad en medianoche.
#
#  Laboral (L–V):
#    · 00–05h  bajo nocturno       0.10
#    · 05–06h  rampa arranque      0.10 → 0.40
#    · 06–09h  rampa primer turno  0.40 → 0.95
#    · 09–13h  pleno primer turno  0.95 → 0.85  (ligera bajada mediodía)
#    · 13–15h  pausa comida        0.85 → 0.60
#    · 15–18h  segundo turno       0.60 → 0.90
#    · 18–22h  bajada tarde        0.90 → 0.60
#    · 22–24h  rampa nocturno      0.60 → 0.10  ← mismo nivel que 00h siguiente
#
#  Sábado: mantenimiento ligero constante  0.20
#  Domingo: guardia mínima                 0.10

_WEEKDAY_PROFILE = [
    (0,  0.10),
    (5,  0.10),
    (6,  0.40),
    (9,  0.95),
    (13, 0.85),
    (15, 0.60),
    (18, 0.90),
    (22, 0.20),
    (24, 0.10),   # ← cierra al mismo nivel que abre → continuidad en medianoche
]

_SATURDAY_PROFILE = [(0, 0.20), (24, 0.20)]
_SUNDAY_PROFILE   = [(0, 0.10), (24, 0.10)]


def _interpolated_base(hour_float: float, weekday: int) -> float:
    """
    Devuelve la fracción de carga nominal para una hora decimal (p.ej. 14.5 = 14:30),
    interpolando linealmente entre los breakpoints del perfil del día.

    Args:
        hour_float: hora del día como float en [0, 24)
        weekday:    0=lunes … 6=domingo

    Returns:
        Fracción de carga en [0, 1]
    """
    if weekday < 5:
        profile = _WEEKDAY_PROFILE
    elif weekday == 5:
        profile = _SATURDAY_PROFILE
    else:
        profile = _SUNDAY_PROFILE

    for i in range(len(profile) - 1):
        h0, v0 = profile[i]
        h1, v1 = profile[i + 1]
        if h0 <= hour_float < h1:
            t = (hour_float - h0) / (h1 - h0)
            return v0 + t * (v1 - v0)

    # fallback: último valor del perfil
    return profile[-1][1]


def _day_rng(date_obj, hour: int) -> np.random.Generator:
    """
    Generador determinista por (fecha, hora).

    Usar una semilla derivada de la fecha garantiza:
      · Reproducibilidad: re-ejecutar el ETL para el mismo día da exactamente
        los mismos valores (idempotencia).
      · Coherencia inter-día: la variabilidad no "salta" entre el último punto
        de un día y el primero del siguiente porque ambos están anclados al
        mismo nivel base del perfil (0.10).

    Args:
        date_obj: objeto datetime.date
        hour:     hora entera del día

    Returns:
        numpy Generator listo para usar
    """
    seed_str = f"{date_obj.isoformat()}:{hour:02d}"
    seed_int = int(hashlib.md5(seed_str.encode()).hexdigest(), 16) % (2 ** 32)
    return np.random.default_rng(seed_int)


def calculate_industrial_consumption(
    forecast_time_utc,
    nominal_load_kw: float,
    temp_ambient_celsius: float,
) -> float:
    """
    High-fidelity industrial consumption simulation scaled to nominal load.

    Models shift patterns, HVAC thermal load and Gaussian process variability.
    Excludes deferrable loads (EVs, batteries) to allow downstream optimisation.

    Mejoras respecto a la versión anterior:
      1. Perfil suavizado: interpolación lineal entre breakpoints → sin escalones
         abruptos en los cambios de tramo (p.ej. 22h: 0.60 → 0.12 instantáneo).
      2. Semilla determinista por (fecha, hora): la variabilidad aleatoria es
         reproducible, por lo que re-ejecutar el ETL para el mismo timestamp
         produce exactamente el mismo valor (idempotencia).
      3. Continuidad en medianoche: el perfil laboral cierra a 0.10 a las 24h,
         igual que abre a las 00h → no hay salto entre el último punto del día
         y el primero del día siguiente.

    Args:
        forecast_time_utc:    timestamp UTC (str, pd.Timestamp o datetime)
        nominal_load_kw:      potencia nominal de la planta [kW]
        temp_ambient_celsius: temperatura ambiente [°C]

    Returns:
        Consumo estimado en kW (≥ 0).
    """
    try:
        dt      = pd.to_datetime(forecast_time_utc)
        hour    = dt.hour
        minute  = dt.minute
        weekday = dt.weekday()

        # ── 1. Base del perfil (interpolada, sin escalones) ───────────────────
        hour_float = hour + minute / 60.0
        base = _interpolated_base(hour_float, weekday)

        # ── 2. Corrección térmica HVAC ────────────────────────────────────────
        #    Refrigeración activa por encima de 25 °C; calefacción por debajo de 15 °C.
        if temp_ambient_celsius > 25:
            thermal = (temp_ambient_celsius - 25) * 0.02
        elif temp_ambient_celsius < 15:
            thermal = (15 - temp_ambient_celsius) * 0.01
        else:
            thermal = 0.0

        # ── 3. Variabilidad gaussiana determinista ────────────────────────────
        #    Semilla derivada de (fecha, hora) → reproducible e idempotente.
        rng         = _day_rng(dt.date(), hour)
        variability = rng.normal(1.0, 0.03)

        # ── 4. Consumo final ──────────────────────────────────────────────────
        consumption = nominal_load_kw * (base + thermal) * variability
        return float(max(0.0, consumption))

    except Exception as exc:
        # logger.warning(...) — mantener el logger del módulo llamante
        raise RuntimeError(
            f"[ENGINE] Industrial consumption calculation failed for t={forecast_time_utc}: {exc}"
        ) from exc



# ─────────────────────────────────────────────────────────────────────────────
# STANDALONE TEST
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    lat          = 42.803852359174265
    lon          = -1.701961806168645
    forecast_t   = "2026-05-01 15:00:00"
    pv_peak      = 16
    loss         = 14
    clouds       = 82
    weather_id   = 803
    angle        = 30
    aspect       = 0
    temp_ambient = 20.29
    wind         = 5.54

    logger.info("[TEST] ── PV engine standalone run ─────────────────────────")

    alfa, azimuth = calculate_solar_position(lat, lon, forecast_t)
    logger.info("[TEST] Solar position — elevation: %.2f° | azimuth: %.2f°", alfa, azimuth)

    ghi = calculate_ghi(alfa, clouds, weather_id)
    logger.info("[TEST] GHI: %.2f W/m²", ghi)

    dni, dhi = decompose_erbs(ghi, alfa, forecast_t)
    logger.info("[TEST] DNI: %.2f W/m² | DHI: %.2f W/m²", dni, dhi)

    poa = calculate_total_poa(dni, dhi, ghi, alfa, azimuth, angle, aspect)
    logger.info("[TEST] POA: %.2f W/m²", poa)

    t_cell = calculate_t_cell(temp_ambient, wind, poa)
    logger.info("[TEST] T_cell: %.2f °C", t_cell)

    p_gen, pr = calculate_power_output(poa, t_cell, pv_peak, loss)
    logger.info("[TEST] P_gen: %.3f kW | PR: %.3f", p_gen, pr)

    p_con = calculate_industrial_consumption(forecast_t, pv_peak, temp_ambient)
    logger.info("[TEST] P_consumption: %.3f kW", p_con)

    logger.info("[TEST] ── Standalone run complete ──────────────────────────")
