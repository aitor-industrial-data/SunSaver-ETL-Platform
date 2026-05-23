"""
gold_fact_energy_decisions_v2.py
────────────────────────────────
Motor de decisiones energéticas — versión 2.1

BUGS CORREGIDOS RESPECTO A v2.0:
  ① cap_kwh NULL → hours_needed=0 → lista vacía → carretilla/cold_storage/pump
     nunca generaban decisión. Corregido: fallback a 4h si cap_kwh es NULL o 0.
  ② solar_hours se clasificaban SOLO como "solar", nunca como "low", aunque a
     mediodía el PVP fuera bajo. La lógica de cold_storage/pump buscaba solar_hours
     en la ventana pero si la ventana era nocturna (ej. 00h–07h) nunca encontraba
     nada y caía al fallback. Corregido: separamos concepto de ventana solar FV
     (para autoconsumo) del concepto de hora barata por precio.
  ③ MIN_WINDOW_ADVANTAGE_PCT=8% demasiado restrictivo en días con precio flat y bajo:
     si pvp_avg=75 €/MWh y la ventana va a 60 €/MWh, la ventaja es 20% → OK.
     Pero si pvp_avg=55 €/MWh (día muy barato) y ventana a 45 €/MWh → 18% → OK.
     El problema era que si todas las horas son baratas (pvp_avg≈pvp_ventana) no
     hay ventaja relativa aunque el ahorro absoluto sea real. Corregido: usamos
     ahorro absoluto en € como criterio principal, no % relativo.
  ④ _best_n_hours_cheap no garantizaba contigüidad. Para carretilla se requieren
     horas seguidas (el cargador no se puede interrumpir). Corregido: función
     _best_consecutive_block que encuentra el bloque contiguo más barato de N horas.
  ⑤ Activos flexibles con is_flexible=1 pero con asset_type no contemplado en el
     motor caían al bloque "not flexible" y generaban alertas de pico incorrectas.
     Corregido: bloque else final solo aplica a is_flexible=0.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import text

from database_utils import get_engine
from logger_config import setup_logging

load_dotenv()
logger = setup_logging()

# ── UMBRALES ──────────────────────────────────────────────────────────────────
PVP_LOW_EUR_MWH  = 80.0    # Hora barata:  PVP < 80 €/MWh
PVP_HIGH_EUR_MWH = 150.0   # Hora cara:    PVP > 150 €/MWh
PV_ACTIVE_KW     = 1.0     # FV activa si genera más de 1 kW

# Ahorro mínimo absoluto en € para que una decisión se emita.
# Muy bajo a propósito: si hay ahorro real de 5 céntimos, se muestra.
# El filtro de calidad es el bloque de texto del reason, no el silencio.
MIN_SAVING_EUR = 0.05

# Horas de carga por defecto si cap_kwh o power_kw son NULL en DB
DEFAULT_CHARGE_HOURS = 4


# ── EXTRACT ───────────────────────────────────────────────────────────────────

def _load_client(conn, client_id: str) -> dict:
    row = conn.execute(text("""
        SELECT client_id, name, description, nominal_load_kw,
               pv_peak_power_kw, has_solar, has_battery, timezone
        FROM gold.dim_client
        WHERE client_id = :cid
    """), {"cid": client_id}).fetchone()
    if not row:
        raise ValueError(f"Cliente '{client_id}' no encontrado en gold.dim_client")
    return dict(row._mapping)


def _load_assets(conn, client_id: str) -> pd.DataFrame:
    rows = conn.execute(text("""
        SELECT asset_id, asset_name, asset_type, power_kw, capacity_kwh,
               is_flexible, flex_window_start, flex_window_end,
               priority, notes, has_capacity, is_overnight_flexible
        FROM gold.dim_assets
        WHERE client_id = :cid
        ORDER BY priority, asset_id
    """), {"cid": client_id}).fetchall()
    if not rows:
        logger.warning("[EXTRACT] Sin activos para cliente '%s'", client_id)
        return pd.DataFrame()
    return pd.DataFrame([dict(r._mapping) for r in rows])


def _load_forecast(conn, client_id: str, target_date: date) -> pd.DataFrame:
    tz       = ZoneInfo(_get_client_tz(conn, client_id))
    dt_start = datetime(target_date.year, target_date.month, target_date.day,
                        0, 0, 0, tzinfo=tz).astimezone(timezone.utc)
    dt_end   = (datetime(target_date.year, target_date.month, target_date.day,
                         tzinfo=tz) + timedelta(days=6)).astimezone(timezone.utc)

    rows = conn.execute(text("""
        SELECT forecast_time_utc, pv_power_gen_kw, pv_performance_ratio,
               poa_wm2, t_cell_celsius, power_consumption_kw,
               temp_celsius, humidity_pct, clouds_pct, rain_prob_norm,
               wind_speed_mps, price_pvpc_eur_mwh, weather_id
        FROM gold.fact_energy_forecast
        WHERE client_id          = :cid
          AND forecast_time_utc >= :dt_start
          AND forecast_time_utc <  :dt_end
        ORDER BY forecast_time_utc
    """), {"cid": client_id, "dt_start": dt_start, "dt_end": dt_end}).fetchall()

    if not rows:
        logger.warning("[EXTRACT] Sin previsión para cliente '%s' en %s", client_id, target_date)
        return pd.DataFrame()

    df = pd.DataFrame([dict(r._mapping) for r in rows])
    df["forecast_time_utc"]   = pd.to_datetime(df["forecast_time_utc"], utc=True)
    df["forecast_time_local"] = df["forecast_time_utc"].dt.tz_convert(tz)
    df["date"]    = df["forecast_time_local"].dt.date
    df["hour"]    = df["forecast_time_local"].dt.hour
    df["has_pvp"] = df["price_pvpc_eur_mwh"].notna()
    return df


def _get_client_tz(conn, client_id: str) -> str:
    row = conn.execute(text(
        "SELECT timezone FROM gold.dim_client WHERE client_id = :cid"
    ), {"cid": client_id}).fetchone()
    return row.timezone if row else "Europe/Madrid"


# ── CLASIFICACIÓN HORARIA ─────────────────────────────────────────────────────

def _classify_hour(pvp: float | None, pv_kw: float) -> str:
    """
    Clasifica cada hora en: low | mid | high | solar.
    La FV activa tiene prioridad: si hay generación propia > 1 kW, la hora es
    'solar' independientemente del PVP de red (autoconsumo siempre es más barato
    que cualquier precio de mercado).
    """
    if pv_kw >= PV_ACTIVE_KW:
        return "solar"
    if pvp is None:
        return "mid"
    if pvp < PVP_LOW_EUR_MWH:
        return "low"
    if pvp > PVP_HIGH_EUR_MWH:
        return "high"
    return "mid"


# ── HELPERS DE VENTANA ────────────────────────────────────────────────────────

def _safe_float(val, default: float = 0.0) -> float:
    """Convierte a float de forma segura. NULL/None/NaN devuelven default."""
    try:
        v = float(val)
        return v if not math.isnan(v) else default
    except (TypeError, ValueError):
        return default


def _hours_in_window(hours: list[int], ws: int, we: int) -> list[int]:
    """
    Filtra 'hours' que caen dentro de la ventana [ws, we].
    Soporta ventanas overnight (ws > we), ej. 22h→06h cruza medianoche.
    """
    if ws <= we:
        return [h for h in hours if ws <= h <= we]
    return [h for h in hours if h >= ws or h <= we]


def _all_hours_in_window(ws: int, we: int) -> list[int]:
    """Todas las horas posibles dentro de la ventana definida para el activo."""
    if ws <= we:
        return list(range(ws, we + 1))
    return list(range(ws, 24)) + list(range(0, we + 1))


def _best_n_hours_cheap(df: pd.DataFrame, candidate_hours: list[int], n: int) -> list[int]:
    """
    De 'candidate_hours', devuelve las N con menor PVP.
    Resultado NO garantiza contigüidad — usar _best_consecutive_block para
    activos que requieren carga continua (carretilla, baterías).
    """
    if not candidate_hours or n <= 0:
        return []
    sub = (df[df["hour"].isin(candidate_hours)]
           .dropna(subset=["price_pvpc_eur_mwh"])
           .sort_values("price_pvpc_eur_mwh", ascending=True))
    return sub["hour"].head(n).tolist()


def _best_consecutive_block(df: pd.DataFrame, candidate_hours: list[int], n: int) -> list[int]:
    """
    Encuentra el bloque contiguo de exactamente N horas (o el mayor disponible
    si hay menos de N horas candidatas) con el menor coste total acumulado.

    Imprescindible para carretillas y baterías: el cargador necesita alimentación
    continua — si le cortas una hora en medio, el ciclo BMS se resetea y la
    batería queda parcialmente cargada. Ejemplo: si hay horas baratas a 01h, 03h, 04h,
    05h, no recomendamos 01h+03h+04h (hay un hueco) sino 03h–05h (3h continuas).

    Algoritmo: ventana deslizante de tamaño n sobre candidate_hours ordenadas.
    Para cada posición calcula el coste medio. Devuelve la ventana de menor coste.
    """
    if not candidate_hours or n <= 0:
        return []

    s = sorted(set(candidate_hours))
    if len(s) <= n:
        return s  # Menos candidatas que horas pedidas: devolvemos todas

    # Construir bloques contiguos dentro de las candidatas
    blocks: list[list[int]] = []
    current = [s[0]]
    for h in s[1:]:
        if h == current[-1] + 1:
            current.append(h)
        else:
            blocks.append(current)
            current = [h]
    blocks.append(current)

    # De cada bloque, extraer todas las sub-ventanas de tamaño n
    best_cost  = float("inf")
    best_block = s[:n]  # fallback: primeras N del sorted

    for blk in blocks:
        if len(blk) < n:
            continue  # Bloque demasiado corto para cubrir N horas
        for start in range(len(blk) - n + 1):
            window = blk[start:start + n]
            prices = df[df["hour"].isin(window)]["price_pvpc_eur_mwh"].dropna()
            if prices.empty:
                continue
            cost = prices.mean()
            if cost < best_cost:
                best_cost  = cost
                best_block = window

    # Si ningún bloque tenía N horas contiguas, tomar el bloque contiguo más
    # largo y más barato (parcialmente satisface el requisito de continuidad).
    if best_block == s[:n]:
        best_blk_sorted = sorted(blocks, key=lambda b: (
            -len(b),
            df[df["hour"].isin(b)]["price_pvpc_eur_mwh"].dropna().mean()
        ))
        if best_blk_sorted:
            best_block = best_blk_sorted[0][:n]

    return best_block


def _estimate_saving_eur(power_kw: float, opt_hours: list[int],
                         df: pd.DataFrame, pvp_ref: float) -> float:
    """
    Ahorro en € de operar 'power_kw' kW en 'opt_hours' vs operar al precio
    de referencia pvp_ref.

    pvp_ref debe ser _window_avg_pvp(df, window_all) — la media de la VENTANA
    FLEXIBLE del activo, NO la media del día entero.

    Por qué importa: ese día tiene 12h solares que tiran pvp_avg_día a ~75 €/MWh.
    La carretilla solo puede cargar de 22h a 06h (todas horas de red, sin solar).
    La media de ESA ventana es ~100 €/MWh. Cargar en el bloque de 45 €/MWh supone
    un ahorro real de 55 €/MWh × kWh. Comparar contra 75 €/MWh da casi 0 — falso.
    """
    if not opt_hours or power_kw <= 0:
        return 0.0
    sub = df[df["hour"].isin(opt_hours)]["price_pvpc_eur_mwh"].dropna()
    if sub.empty:
        return 0.0
    saving = sum((pvp_ref - p) * power_kw / 1000.0 for p in sub)
    return round(max(0.0, saving), 3)


def _window_avg_pvp(df: pd.DataFrame, window_hours: list[int]) -> float:
    """
    Precio medio de la ventana flexible del activo — referencia correcta para
    calcular el ahorro de optimizar dentro de esa ventana.
    Si la ventana no tiene datos PVP (ej. horas solares sin precio de red)
    cae back a la media del día.
    """
    sub = df[df["hour"].isin(window_hours)]["price_pvpc_eur_mwh"].dropna()
    if sub.empty:
        return float(df["price_pvpc_eur_mwh"].dropna().mean() or 100.0)
    return float(sub.mean())


def _fmt_window(hours: list[int]) -> str:
    """
    Formatea horas como rango si son contiguas, lista si no.
    [1,2,3,4,5] → '01h–05h'
    [1,3,4,5]   → '01h, 03h–05h'
    [1,3,5]     → '01h, 03h, 05h'
    """
    if not hours:
        return "—"
    s = sorted(set(hours))
    segments: list[list[int]] = []
    seg = [s[0]]
    for h in s[1:]:
        if h == seg[-1] + 1:
            seg.append(h)
        else:
            segments.append(seg)
            seg = [h]
    segments.append(seg)
    parts = [f"{g[0]:02d}h–{g[-1]:02d}h" if len(g) > 1 else f"{g[0]:02d}h" for g in segments]
    return ", ".join(parts)


def _fmt_list(hours: list[int]) -> str:
    if not hours:
        return "—"
    return ", ".join(f"{h:02d}h" for h in sorted(hours))


def _fmt_eur(v: float) -> str:
    return f"{v:.2f} €" if abs(v) < 10 else f"{v:.1f} €"

def _fmt_flex_window_label(ws: int, we: int) -> str:
    """Etiqueta legible de la ventana flexible del activo (para el report)."""
    if ws <= we:
        return f"{ws:02d}h–{we:02d}h"
    return f"{ws:02d}h–{we:02d}h (+1d)"  # overnight


# ── MOTOR DE DECISIONES ───────────────────────────────────────────────────────

def _build_decisions(df_today: pd.DataFrame, df_assets: pd.DataFrame) -> list[dict]:
    """
    Motor de reglas v2.1 — genera una decisión por activo con:
      · Ventana óptima real calculada hora a hora sobre el PVP previsto.
      · Ahorro económico estimado en €/día.
      · Texto de razón con cifras concretas: precios, potencias, multiplicadores.
      · Ordenación final por ahorro potencial descendente.

    Nota sobre power_consumption_kw:
      El campo está en df_today para contexto pero NO se usa para desplazar carga
      de la demanda base (nunca sobra FV, siempre se consume más de lo que se genera).
      Se usa solo para contextualizar mensajes (p.ej. "la FV cubre el X% de la demanda
      prevista en esa hora").
    """
    decisions: list[dict] = []

    if df_assets.empty:
        return decisions

    has_pvp    = df_today["has_pvp"].any()
    pvp_avg    = df_today["price_pvpc_eur_mwh"].dropna().mean() if has_pvp else 100.0
    pvp_min    = df_today["price_pvpc_eur_mwh"].min()           if has_pvp else 0.0
    pvp_max    = df_today["price_pvpc_eur_mwh"].max()           if has_pvp else 200.0
    pv_peak_kw = df_today["pv_power_gen_kw"].max()
    pv_peak_h  = int(df_today.loc[df_today["pv_power_gen_kw"].idxmax(), "hour"])

    # Consumo medio previsto de fábrica (power_consumption_kw del forecast)
    avg_consumption_kw = df_today["power_consumption_kw"].dropna().mean()

    low_hours   = df_today[df_today["pvp_class"] == "low"]["hour"].tolist()
    high_hours  = df_today[df_today["pvp_class"] == "high"]["hour"].tolist()
    solar_hours = df_today[df_today["pvp_class"] == "solar"]["hour"].tolist()

    # cheap_hours = solo horas con PVP de RED bajo (< 80 €/MWh).
    # Las horas solares NO se incluyen aquí porque la fábrica consume siempre más
    # de lo que genera — las líneas de producción son ininterrumpibles y se comen
    # toda la FV. En hora solar el precio que pagas sigue siendo el de mercado
    # por la potencia que la FV no cubre. Tratar horas solares como "baratas"
    # llevaría a recomendar cargar carretillas a mediodía cuando la fábrica está
    # a pleno rendimiento — exactamente lo contrario de lo que se necesita.
    cheap_hours = low_hours  # solo precio de red < 80 €/MWh

    logger.debug(
        "[MOTOR] pvp_avg=%.0f low=%s high=%s solar=%s cheap=%s",
        pvp_avg, low_hours, high_hours, solar_hours, cheap_hours
    )

    for _, asset in df_assets.iterrows():
        atype    = asset["asset_type"]
        flexible = bool(asset["is_flexible"] == 1)
        # _safe_float evita crashes por NULL en campos numéricos de la DB
        power_kw = _safe_float(asset["power_kw"], 0.0)
        cap_kwh  = _safe_float(asset.get("capacity_kwh"), 0.0)
        ws       = int(_safe_float(asset["flex_window_start"], 0))
        we       = int(_safe_float(asset["flex_window_end"], 23))
        priority = int(_safe_float(asset["priority"], 99))
        name     = asset["asset_name"]
        asset_id = asset["asset_id"]

        window_all = _all_hours_in_window(ws, we)

        # ── CARRETILLA ELEVADORA / BATERÍA DE IONES DE LITIO ─────────────────
        if atype == "forklift_battery" and flexible:
            # Horas de carga necesarias: cap_kwh / power_kw redondeado arriba.
            # Si cap_kwh es NULL en DB (dato no introducido), asumimos 4h —
            # valor conservador para un turno de 8h con carga al 50%.
            if cap_kwh > 0 and power_kw > 0:
                hours_needed = math.ceil(cap_kwh / power_kw)
            else:
                hours_needed = DEFAULT_CHARGE_HOURS
                logger.warning("[%s] cap_kwh o power_kw NULL/0 → asumiendo %dh de carga",
                               name, hours_needed)

            # Candidatas: horas baratas (precio bajo o solar) dentro de la ventana.
            # Si no hay ninguna, tomamos todas las de la ventana y elegimos las mejores.
            cheap_in_win = _hours_in_window(cheap_hours, ws, we)
            if not cheap_in_win:
                cheap_in_win = window_all
                logger.info("[%s] Sin cheap_hours en ventana %02dh–%02dh, usando ventana completa",
                            name, ws, we)

            # Bloque contiguo más barato de N horas — crítico para baterías Li-ion:
            # interrumpir la carga a mitad de ciclo degrada el BMS y deja la
            # batería desbalanceada entre celdas.
            opt_hours = _best_consecutive_block(df_today, cheap_in_win, hours_needed)

            if not opt_hours:
                logger.warning("[%s] No se encontró bloque de carga válido — SKIP", name)
                continue

            saving = _estimate_saving_eur(power_kw, opt_hours, df_today, _window_avg_pvp(df_today, window_all))

            win_pvp_avg  = df_today[df_today["hour"].isin(opt_hours)]["price_pvpc_eur_mwh"].mean()
            win_pvp_str  = f"{win_pvp_avg:.0f}" if not pd.isna(win_pvp_avg) else "—"
            high_str     = _fmt_list(high_hours) if high_hours else "ninguna hora cara identificada"
            cost_optimal = power_kw * len(opt_hours) * (win_pvp_avg or 0) / 1000
            cost_peak    = power_kw * len(opt_hours) * pvp_max / 1000

            reason = (
                f"La batería necesita {hours_needed}h de carga continua "
                f"({cap_kwh:.0f} kWh a {power_kw:.1f} kW). "
                f"El bloque óptimo es {_fmt_window(opt_hours)}: PVP medio "
                f"{win_pvp_str} €/MWh, coste total ~{cost_optimal:.2f} €. "
                f"Si se carga en pico ({high_str}, {pvp_max:.0f} €/MWh) el mismo "
                f"ciclo costaría ~{cost_peak:.2f} € — {cost_peak/max(cost_optimal,0.01):.1f}x más caro. "
                f"Conectar el cargador antes de las {opt_hours[0]:02d}h y no desenchufar "
                f"hasta las {opt_hours[-1]:02d}h para que el BMS complete el ciclo "
                f"de balanceo de celdas sin interrupciones."
            )

            # Ahorro concreto: coste del ciclo en el bloque óptimo vs en la hora pico.
            # Siempre > 0 porque opt_hours es el bloque más barato de la ventana
            # y pvp_max es siempre ≥ win_pvp_avg.
            cost_vs_peak = round(power_kw * len(opt_hours) * (pvp_max - (win_pvp_avg or pvp_max)) / 1000, 2)

            decisions.append({
                "asset_id":    asset_id,
                "asset_name":  name,
                "asset_type":  atype,
                "priority":    priority,
                "time_window": _fmt_window(opt_hours),
                "action":      "Programar carga nocturna — bloque óptimo continuo",
                "reason":      reason,
                "saving_tag":  f"Evitas ~{_fmt_eur(abs(cost_vs_peak))} vs cargar en pico ({pvp_max:.0f} €/MWh)",
                "saving_eur":  abs(cost_vs_peak),
                "urgency":     "critical",
                "flex_window_label": _fmt_flex_window_label(ws, we),
            })

        # ── CÁMARA FRIGORÍFICA / COLD STORAGE ────────────────────────────────
        elif atype == "cold_storage" and flexible:
            # Estrategia pull-down: bajar consigna 1–2°C en horas baratas/solares
            # para acumular frío. La inercia térmica de la cámara mantiene temperatura
            # durante las horas de pico sin que el compresor arranque.
            # Cuánto aguanta sin compresor depende del aislamiento, pero
            # típicamente 2–4h para una cámara bien aislada.

            cheap_in_win = _hours_in_window(cheap_hours, ws, we)
            if not cheap_in_win:
                cheap_in_win = _best_n_hours_cheap(df_today, window_all, 4)

            opt_hours = sorted(cheap_in_win)

            if not opt_hours:
                continue

            saving       = _estimate_saving_eur(power_kw, opt_hours, df_today, _window_avg_pvp(df_today, window_all))
            win_pvp_avg  = df_today[df_today["hour"].isin(opt_hours)]["price_pvpc_eur_mwh"].mean()
            # La FV reduce la importación de red pero NO hace que las horas solares
            # sean gratuitas: la fábrica consume más de lo que genera en todo momento.
            # El mensaje correcto es el precio de red que se paga en esa ventana.
            solar_in_win = [h for h in opt_hours if h in solar_hours]
            mode_str     = f"precio mínimo de red {win_pvp_avg:.0f} €/MWh"
            high_str     = _fmt_list(high_hours) if high_hours else "—"
            cost_compressor_peak = power_kw * len(high_hours) * pvp_max / 1000

            reason = (
                f"Bajar consigna de temperatura 1–2°C durante {_fmt_window(opt_hours)} "
                f"aprovechando {mode_str}. "
                f"La masa térmica de la cámara absorbe el frío extra y mantiene la "
                f"temperatura de seguridad durante {len(high_hours)}h de pico sin arranques. "
                f"Si el compresor trabajara a plena carga en {high_str} "
                f"({pvp_max:.0f} €/MWh) consumiría ~{cost_compressor_peak:.2f} € "
                f"solo en esa franja. Cada arranque evitado en pico ahorra también "
                f"desgaste mecánico — el arranque es el momento de mayor estrés del compresor."
            )

            decisions.append({
                "asset_id":    asset_id,
                "asset_name":  name,
                "asset_type":  atype,
                "priority":    priority,
                "time_window": _fmt_window(opt_hours),
                "action":      "Pre-enfriamiento pull-down en ventana solar/barata",
                "reason":      reason,
                "saving_tag":  f"Ahorro ~{_fmt_eur(saving)}/día",
                "saving_eur":  saving,
                "urgency":     "high",
                "flex_window_label": _fmt_flex_window_label(ws, we),
            })

        # ── COMPRESOR DE AIRE ─────────────────────────────────────────────────
        elif atype == "compressor" and flexible:
            cheap_in_win = _hours_in_window(cheap_hours, ws, we)
            if not cheap_in_win:
                cheap_in_win = _best_n_hours_cheap(df_today, window_all, 2)

            opt_hours = sorted(cheap_in_win[:3])  # máximo 3h de ventana de mantenimiento

            if not opt_hours:
                continue

            saving      = _estimate_saving_eur(power_kw, opt_hours, df_today, _window_avg_pvp(df_today, window_all))
            win_pvp_avg = df_today[df_today["hour"].isin(opt_hours)]["price_pvpc_eur_mwh"].mean()
            high_str    = _fmt_list(high_hours) if high_hours else "—"

            reason = (
                f"Arrancar el compresor y ejecutar purga de condensados + revisión "
                f"de filtros durante {_fmt_window(opt_hours)} "
                f"(PVP ~{win_pvp_avg:.0f} €/MWh). "
                f"El pico de corriente en el arranque del motor ({power_kw:.1f} kW) "
                f"es 5–7x la corriente nominal durante 2–3 segundos — si ese arranque "
                f"coincide con {high_str} ({pvp_max:.0f} €/MWh) el impacto en la "
                f"factura de término de potencia puede ser desproporcionado. "
                f"Mantenimiento en esta ventana no afecta a la presión del circuito "
                f"de producción porque el depósito buffer aguanta la demanda mientras "
                f"el compresor está en modo purga."
            )

            decisions.append({
                "asset_id":    asset_id,
                "asset_name":  name,
                "asset_type":  atype,
                "priority":    priority,
                "time_window": _fmt_window(opt_hours),
                "action":      "Programar arranque y mantenimiento en ventana económica",
                "reason":      reason,
                "saving_tag":  f"Ahorro ~{_fmt_eur(saving)}/día",
                "saving_eur":  saving,
                "urgency":     "medium",
                "flex_window_label": _fmt_flex_window_label(ws, we),
            })

        # ── BOMBAS DE PROCESO ─────────────────────────────────────────────────
        elif atype == "pump" and flexible:
            cheap_in_win = _hours_in_window(cheap_hours, ws, we)
            if not cheap_in_win:
                cheap_in_win = _best_n_hours_cheap(df_today, window_all, 3)

            opt_hours = sorted(cheap_in_win)

            if not opt_hours:
                continue

            saving       = _estimate_saving_eur(power_kw, opt_hours, df_today, _window_avg_pvp(df_today, window_all))
            win_pvp_avg  = df_today[df_today["hour"].isin(opt_hours)]["price_pvpc_eur_mwh"].mean()
            # Misma lógica: solar reduce importación pero no es coste cero porque
            # la demanda basal de las líneas de producción supera siempre la generación FV.
            solar_in_win = [h for h in opt_hours if h in solar_hours]
            mode_str     = f"precio mínimo de red {win_pvp_avg:.0f} €/MWh"
            high_str     = _fmt_list(high_hours) if high_hours else "—"
            avoid_h      = min(high_hours) if high_hours else 22
            cost_if_peak = power_kw * len(opt_hours) * pvp_max / 1000

            reason = (
                f"Operar bombas durante {_fmt_window(opt_hours)} aprovechando "
                f"{mode_str}. "
                f"Con el depósito lleno antes de las {avoid_h:02d}h, los arranques "
                f"de bomba en la franja cara ({high_str}, {pvp_max:.0f} €/MWh) "
                f"se reducen o eliminan completamente. "
                f"Operar el mismo tiempo en pico costaría ~{cost_if_peak:.2f} € — "
                f"en ventana solar/barata ese coste se reduce a ~{saving:.2f} € de ahorro neto. "
                f"El caudal bombeado es el mismo; solo cambia cuándo se hace."
            )

            decisions.append({
                "asset_id":    asset_id,
                "asset_name":  name,
                "asset_type":  atype,
                "priority":    priority,
                "time_window": _fmt_window(opt_hours),
                "action":      "Llenar depósito de proceso en horario solar/barato",
                "reason":      reason,
                "saving_tag":  f"Ahorro ~{_fmt_eur(saving)}/día",
                "saving_eur":  saving,
                "urgency":     "medium",
                "flex_window_label": _fmt_flex_window_label(ws, we),
            })

        # ── AUTOCLAVE / PASTEURIZADOR ─────────────────────────────────────────
        elif atype == "autoclave" and flexible:
            cheap_in_win = _hours_in_window(cheap_hours, ws, we)
            if not cheap_in_win:
                cheap_in_win = _best_n_hours_cheap(df_today, window_all, 4)

            opt_hours = sorted(cheap_in_win)

            if not opt_hours:
                continue

            saving       = _estimate_saving_eur(power_kw, opt_hours, df_today, _window_avg_pvp(df_today, window_all))
            win_pvp_avg  = df_today[df_today["hour"].isin(opt_hours)]["price_pvpc_eur_mwh"].mean()
            fv_cover_pct = min(100, int(pv_peak_kw / max(power_kw, 0.1) * 100))
            cost_if_peak = power_kw * len(opt_hours) * pvp_max / 1000

            reason = (
                f"Programar ciclos de esterilización durante {_fmt_window(opt_hours)} "
                f"(PVP ~{win_pvp_avg:.0f} €/MWh). "
                f"La instalación FV cubre hasta el {fv_cover_pct}% del consumo del equipo "
                f"en las horas de máxima generación ({pv_peak_kw:.1f} kW FV pico a las "
                f"{pv_peak_h:02d}h vs {power_kw:.1f} kW del autoclave). "
                f"Un ciclo equivalente en pico ({pvp_max:.0f} €/MWh) costaría "
                f"~{cost_if_peak:.2f} € — ahorro directo de {_fmt_eur(saving)} "
                f"por reorganizar el turno. No arrancar en {_fmt_list(high_hours)}."
            )

            decisions.append({
                "asset_id":    asset_id,
                "asset_name":  name,
                "asset_type":  atype,
                "priority":    priority,
                "time_window": _fmt_window(opt_hours),
                "action":      "Concentrar ciclos largos en turno solar/barato",
                "reason":      reason,
                "saving_tag":  f"Ahorro ~{_fmt_eur(saving)}/día",
                "saving_eur":  saving,
                "urgency":     "high",
                "flex_window_label": _fmt_flex_window_label(ws, we),
            })

        # ── ILUMINACIÓN ───────────────────────────────────────────────────────
        elif atype == "lighting":
            if not high_hours:
                continue
            cost_if_on  = sum(
                df_today[df_today["hour"].isin(high_hours)]["price_pvpc_eur_mwh"].dropna()
            ) * power_kw / 1000.0
            saving_real = round(cost_if_on * 0.30, 3)  # 30% zonas no productivas apagables

            if saving_real < MIN_SAVING_EUR:
                continue

            reason = (
                f"El precio alcanza {pvp_max:.0f} €/MWh en {_fmt_list(high_hours)}. "
                f"Mantener {power_kw:.1f} kW de iluminación total en esas horas "
                f"cuesta ~{cost_if_on:.2f} €. "
                f"Apagando el 30% de zonas no productivas (pasillos, vestuarios, "
                f"almacén secundario) se evitan ~{saving_real:.2f} € sin impacto "
                f"en operaciones. En horas solares ({_fmt_window(solar_hours)}) "
                f"la iluminación está cubierta por FV propia — sin coste de red."
            )

            decisions.append({
                "asset_id":    asset_id,
                "asset_name":  name,
                "asset_type":  atype,
                "priority":    priority + 10,
                "time_window": _fmt_list(high_hours),
                "action":      "Apagar iluminación no esencial en horas pico",
                "reason":      reason,
                "saving_tag":  f"Ahorro ~{_fmt_eur(saving_real)}/día",
                "saving_eur":  saving_real,
                "urgency":     "low",
                "flex_window_label": _fmt_flex_window_label(ws, we),
            })

        # ── ACTIVOS NO FLEXIBLES: ALERTA DE COINCIDENCIA EN PICO ─────────────
        elif not flexible:
            if not high_hours:
                continue
            high_cost = sum(
                df_today[df_today["hour"].isin(high_hours)]["price_pvpc_eur_mwh"].dropna()
            ) * power_kw / 1000.0

            # Consumo previsto de fábrica en esas horas para dar contexto de potencia
            factory_kw_peak = df_today[df_today["hour"].isin(high_hours)][
                "power_consumption_kw"].mean()

            reason = (
                f"Activo no desplazable: operará {power_kw:.1f} kW en pico "
                f"({_fmt_list(high_hours)}, hasta {pvp_max:.0f} €/MWh). "
                f"Coste estimado solo en esa franja: ~{high_cost:.2f} €. "
                f"La demanda prevista de fábrica en esas horas es "
                f"~{factory_kw_peak:.0f} kW — este equipo representa el "
                f"{min(100, int(power_kw/max(factory_kw_peak,1)*100))}% de esa demanda. "
                f"Evitar arrancar otros flexibles simultáneamente para no disparar "
                f"el máximo de potencia registrado en el periodo de facturación."
            )

            decisions.append({
                "asset_id":    asset_id,
                "asset_name":  name,
                "asset_type":  atype,
                "priority":    priority + 50,
                "time_window": _fmt_list(high_hours),
                "action":      "Monitorizar consumo — activo no flexible",
                "reason":      reason,
                "saving_tag":  "Alerta pico",
                "saving_eur":  0.0,
                "urgency":     "low",
                "flex_window_label": _fmt_flex_window_label(ws, we),
            })

    # Ordenar: primero urgencia (critical → low), luego por ahorro descendente.
    urgency_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    decisions.sort(key=lambda d: (
        urgency_rank.get(d.get("urgency", "low"), 9),
        -d.get("saving_eur", 0.0),
    ))
    return decisions


# ── ÍNDICE DE OPORTUNIDAD ─────────────────────────────────────────────────────

def _opportunity_index(df_today: pd.DataFrame, decisions: list[dict]) -> int:
    """
    Índice 0–100: cuán buen día es para optimizar el consumo energético.
    Tres componentes con su interpretación:

      A) Spread PVP (max−min), hasta 40 pts:
         Mide cuánto varía el precio a lo largo del día. A mayor spread, mayor
         valor tiene desplazar cargas de las horas caras a las baratas.
         Escala: spread 0 €/MWh → 0 pts | spread ≥ 120 €/MWh → 40 pts (techo).
         Ejemplo del día de la captura: 151−45 = 106 €/MWh → 35 pts.

      B) Horas de FV activa (>1 kW), hasta 35 pts:
         Más horas solares = más autoconsumo disponible y más ventanas de bajo
         coste para desplazar cargas.
         Escala: 0h → 0 pts | ≥10h → 35 pts (techo).
         Ejemplo: 13h de FV → 35 pts (techo alcanzado).

      C) Ahorro potencial total de las decisiones generadas, hasta 25 pts:
         Cuánto valen concretamente las recomendaciones del día en €.
         Escala: 0 € → 0 pts | ≥10 € → 25 pts (techo).
         Este componente sube ahora que el ahorro se calcula contra la media
         de la ventana flexible (no del día entero).

    Con los datos del día: 35 + 35 + (ahorro/0.4 capeado a 25) ≈ 80-90.
    Antes salía 52 porque: score_spread usaba /4.0 (muy lento en escalar) y
    score_saving era 0 porque el ahorro se calculaba mal.
    """
    has_pvp = df_today["has_pvp"].any()
    if not has_pvp:
        return 30

    spread       = df_today["price_pvpc_eur_mwh"].max() - df_today["price_pvpc_eur_mwh"].min()
    hours_solar  = int((df_today["pv_power_gen_kw"] >= PV_ACTIVE_KW).sum())
    total_saving = sum(d.get("saving_eur", 0.0) for d in decisions)

    # A) Spread: 0 €/MWh → 0 pts, 120 €/MWh → 40 pts
    score_spread = min(40, spread / 3.0)
    # B) Solar: 0h → 0 pts, 10h → 35 pts (techo)
    score_solar  = min(35, hours_solar * 3.5)
    # C) Ahorro: 0 € → 0 pts, 10 € → 25 pts (techo)
    score_saving = min(25, total_saving * 2.5)

    return min(100, int(score_spread + score_solar + score_saving))


# ── KPIs DEL DÍA ─────────────────────────────────────────────────────────────

def _build_kpis(df_today: pd.DataFrame) -> dict:
    has_pvp     = df_today["has_pvp"].any()
    pvp_s       = df_today[df_today["has_pvp"]]
    pv_peak_row = df_today.loc[df_today["pv_power_gen_kw"].idxmax()]

    return {
        "pv_peak_kw":          round(float(df_today["pv_power_gen_kw"].max()), 1),
        "pv_peak_hour":        int(pv_peak_row["hour"]),
        "pv_total_kwh":        round(float(df_today["pv_power_gen_kw"].sum()), 1),
        "pvp_min":             round(float(pvp_s["price_pvpc_eur_mwh"].min()), 2) if has_pvp else None,
        "pvp_min_hour":        int(pvp_s.loc[pvp_s["price_pvpc_eur_mwh"].idxmin(), "hour"]) if has_pvp else None,
        "pvp_max":             round(float(pvp_s["price_pvpc_eur_mwh"].max()), 2) if has_pvp else None,
        "pvp_max_hour":        int(pvp_s.loc[pvp_s["price_pvpc_eur_mwh"].idxmax(), "hour"]) if has_pvp else None,
        "pvp_avg":             round(float(pvp_s["price_pvpc_eur_mwh"].mean()), 2) if has_pvp else None,
        "avg_consumption_kw":  round(float(df_today["power_consumption_kw"].mean()), 1),
        "hours_solar":         int((df_today["pv_power_gen_kw"] >= PV_ACTIVE_KW).sum()),
        "hours_cheap":         int((df_today["price_pvpc_eur_mwh"] < PVP_LOW_EUR_MWH).sum()),
        "hours_expensive":     int((df_today["price_pvpc_eur_mwh"] > PVP_HIGH_EUR_MWH).sum()),
        "has_pvp":             bool(has_pvp),
        "forecast_reliability": "alta" if has_pvp else "baja",
    }


# ── OUTLOOK SEMANAL ───────────────────────────────────────────────────────────

def _build_outlook(df_forecast: pd.DataFrame, target_date: date) -> dict:
    future = df_forecast[df_forecast["date"] > target_date].copy()
    if future.empty:
        return {"summary_text": "Sin datos de previsión para los próximos días.", "days": []}

    days_out = []
    for day, grp in future.groupby("date"):
        wx_id = (grp["weather_id"].dropna().mode().iloc[0]
                 if not grp["weather_id"].dropna().empty else None)
        days_out.append({
            "date":       str(day),
            "pv_peak_kw": round(float(grp["pv_power_gen_kw"].max()), 1),
            "clouds_pct": round(float(grp["clouds_pct"].mean()), 0),
            "rain_prob":  round(float(grp["rain_prob_norm"].mean()), 2),
            "temp_max":   round(float(grp["temp_celsius"].max()), 1),
            "temp_min":   round(float(grp["temp_celsius"].min()), 1),
            "hours_pv":   int((grp["pv_power_gen_kw"] >= PV_ACTIVE_KW).sum()),
            "weather_id": int(wx_id) if wx_id is not None else None,
            "reliability": "baja",
        })

    avg_pv     = sum(d["pv_peak_kw"] for d in days_out) / len(days_out)
    avg_clouds = sum(d["clouds_pct"]  for d in days_out) / len(days_out)
    rainy_days = sum(1 for d in days_out if d["rain_prob"] > 0.5)

    if avg_clouds < 40 and avg_pv > 7:
        tone = "semana con buena generación fotovoltaica prevista"
        rec  = "Planificar cargas intensivas para mediodía solar."
    elif avg_clouds > 65 or rainy_days >= 3:
        tone = "semana con nubosidad alta y generación FV limitada"
        rec  = "Priorizar eficiencia en consumo base. FV no será determinante."
    else:
        tone = "semana con generación FV moderada e inestable"
        rec  = "Confirmar previsión cada mañana antes de planificar cargas."

    summary_text = (
        f"Previsión orientativa para los próximos {len(days_out)} días: {tone}. "
        f"FV media prevista {avg_pv:.1f} kW pico, nubosidad media {avg_clouds:.0f}%. "
        f"{rainy_days} día(s) con probabilidad de lluvia >50%. {rec} "
        f"⚠ Sin PVP disponible — datos climáticos con umbral de confianza extendido."
    )
    return {"summary_text": summary_text, "days": days_out}


# ── ORCHESTRATOR ──────────────────────────────────────────────────────────────

def build_energy_decisions(client_id: str) -> dict[str, Any]:
    """
    Punto de entrada principal. target_date = mañana (today + 1).
    Devuelve el dict completo de decisiones listo para report_generator.
    """
    logger.info("[INIT] ── build_energy_decisions v2.1 — cliente: %s ──", client_id)

    engine      = get_engine()
    target_date = date.today() + timedelta(days=1)

    with engine.connect() as conn:
        client      = _load_client(conn, client_id)
        df_assets   = _load_assets(conn, client_id)
        df_forecast = _load_forecast(conn, client_id, target_date)

    if df_forecast.empty:
        logger.error("[ERROR] Sin datos de previsión — abortando")
        return {}

    df_today = df_forecast[df_forecast["date"] == target_date].copy()
    if df_today.empty:
        logger.error("[ERROR] Sin registros de previsión para %s", target_date)
        return {}

    df_today["pvp_class"] = df_today.apply(
        lambda r: _classify_hour(r["price_pvpc_eur_mwh"], r["pv_power_gen_kw"]), axis=1
    )

    kpis      = _build_kpis(df_today)
    decisions = _build_decisions(df_today, df_assets)
    outlook   = _build_outlook(df_forecast, target_date)

    opp_index    = _opportunity_index(df_today, decisions)
    total_saving = round(sum(d.get("saving_eur", 0.0) for d in decisions), 2)

    pvp_hours = df_today[["hour", "price_pvpc_eur_mwh", "pvp_class"]].to_dict(orient="records")
    pv_hours  = df_today[["hour", "pv_power_gen_kw", "power_consumption_kw"]].to_dict(orient="records")

    tz_name   = client.get("timezone", "Europe/Madrid")
    now_local = datetime.now(ZoneInfo(tz_name))

    result = {
        "client": client,
        "today": {
            "date":              str(target_date),
            "pvp_hours":         pvp_hours,
            "pv_hours":          pv_hours,
            "kpis":              kpis,
            "decisions":         decisions,
            "opportunity_index": opp_index,
            "total_saving_eur":  total_saving,
        },
        "outlook":      outlook,
        "generated_at": now_local.strftime("%Y-%m-%d %H:%M hora local"),
    }

    logger.info(
        "[DONE] v2.1 — %d decisiones, ahorro ~%.2f €, oportunidad %d/100",
        len(decisions), total_saving, opp_index,
    )
    return result


if __name__ == "__main__":
    import json
    data = build_energy_decisions("CLT-0001")
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))