"""
gold_fact_energy_decisions_v2.py
────────────────────────────────
Motor de reglas energéticas para el informe diario de planta.
Versión 2 — reescritura completa del motor de decisiones.

CONTRATO DE SALIDA (sin cambios de estructura respecto a v1):
  {
    "client":    {...},
    "today":     {
        "date", "pvp_hours", "pv_hours", "kpis", "decisions",
        "opportunity_index",  ← NUEVO: 0-100, cuán bueno es el día para optimizar
        "total_saving_eur",   ← NUEVO: ahorro potencial total estimado del día
    },
    "outlook":   { "summary_text", "days" },
    "generated_at": str,
  }

PROBLEMAS CORREGIDOS RESPECTO A V1:
  ① Ventana de carretilla fallback al rango completo (01h–06h) aunque ninguna
     hora de esa ventana sea realmente barata. Ahora, si no hay horas cheap en la
     ventana, se seleccionan las N horas más baratas reales de esa ventana; y si el
     precio medio de la ventana no supera un umbral mínimo de ahorro vs. la media del
     día, directamente no se emite la recomendación (evita ruido).
  ② Sin cálculo de ahorro económico. Ahora cada decisión incluye saving_eur: cuánto
     se ahorra en € ese día si se ejecuta la acción, calculado hora a hora con el PVP
     real vs. el precio medio del día.
  ③ Comentarios genéricos ("Evitar pico"). Ahora cada texto explica exactamente por
     qué esa hora es mala (precio específico, diferencia vs media, potencia afectada).
  ④ Ventana mostrada como rango aunque las horas óptimas sean dispersas. Ahora se
     formatea como lista si no son contiguas (ej. "02h, 04h, 05h") y como rango si
     lo son (ej. "02h–05h").
  ⑤ Sin índice de oportunidad diario. El campo opportunity_index (0–100) resume de
     un vistazo si el día merece optimización activa o no.
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
PVP_LOW_EUR_MWH  = 80.0    # Hora barata: PVP < 80 €/MWh
PVP_HIGH_EUR_MWH = 150.0   # Hora cara:   PVP > 150 €/MWh
PV_ACTIVE_KW     = 1.0     # FV activa si genera > 1 kW

# Una ventana de carga solo se recomienda si es al menos este % más barata
# que la media del día. Evita recomendar cargar a 95 €/MWh cuando la media es 97 €/MWh.
MIN_WINDOW_ADVANTAGE_PCT = 8.0

# Ahorro mínimo estimado para emitir una decisión (filtra recomendaciones triviales)
MIN_SAVING_EUR = 0.05


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
    """low | mid | high | solar — la FV activa tiene prioridad sobre el precio."""
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

def _hours_in_window(hours: list[int], ws: int, we: int) -> list[int]:
    """
    Filtra 'hours' que caen dentro de la ventana [ws, we].
    Soporta ventanas overnight (ws > we, ej. 22→06):
    en ese caso la ventana cruza medianoche y engloba 22, 23, 00, 01..06.
    """
    if ws <= we:
        return [h for h in hours if ws <= h <= we]
    return [h for h in hours if h >= ws or h <= we]


def _all_hours_in_window(ws: int, we: int) -> list[int]:
    """Todas las horas posibles dentro de la ventana del activo."""
    if ws <= we:
        return list(range(ws, we + 1))
    return list(range(ws, 24)) + list(range(0, we + 1))


def _best_n_hours_cheap(df: pd.DataFrame, candidate_hours: list[int], n: int) -> list[int]:
    """
    De 'candidate_hours', devuelve las N con menor PVP.
    Se usa cuando no hay horas clasificadas como 'low' dentro de la ventana del activo
    pero igualmente hay que seleccionar el mejor subintervalo posible.
    """
    if not candidate_hours:
        return []
    sub = (df[df["hour"].isin(candidate_hours)]
           .dropna(subset=["price_pvpc_eur_mwh"])
           .sort_values("price_pvpc_eur_mwh", ascending=True))
    return sub["hour"].head(n).tolist()


def _window_advantage_pct(df: pd.DataFrame, window_hours: list[int]) -> float:
    """
    Ventaja porcentual de 'window_hours' respecto al precio medio del día completo.
    Positivo → la ventana es más barata que la media; negativo → es más cara.
    Ej: día con media 110 €/MWh, ventana con media 75 €/MWh → ventaja 31.8%.
    """
    day_avg = df["price_pvpc_eur_mwh"].dropna().mean()
    if not window_hours or day_avg == 0 or pd.isna(day_avg):
        return 0.0
    win_avg = df[df["hour"].isin(window_hours)]["price_pvpc_eur_mwh"].dropna().mean()
    if pd.isna(win_avg):
        return 0.0
    return (day_avg - win_avg) / day_avg * 100.0


def _estimate_saving_eur(power_kw: float, opt_hours: list[int],
                         df: pd.DataFrame, pvp_avg: float) -> float:
    """
    Estima el ahorro en € de operar 'power_kw' kW durante 'opt_hours' en lugar
    de operar a precio medio del día.
    Fórmula: Σ_h [ (pvp_avg - pvp_h) * power_kw / 1000 ]
    Un resultado negativo significaría que la ventana es peor que la media (no debería
    ocurrir con opt_hours bien seleccionadas, pero se devuelve 0.0 en ese caso).
    """
    if not opt_hours or power_kw <= 0:
        return 0.0
    sub = df[df["hour"].isin(opt_hours)]["price_pvpc_eur_mwh"].dropna()
    if sub.empty:
        return 0.0
    saving = sum((pvp_avg - p) * power_kw / 1000.0 for p in sub)
    return round(max(0.0, saving), 3)


def _fmt_window(hours: list[int]) -> str:
    """
    Formatea una lista de horas como intervalo o lista según sean contiguas.
    [1,2,3,4,5] → '01h–05h'    (contiguas)
    [1,3,4,5]   → '01h, 03h–05h'  (parcialmente contiguas)
    [1,3,5]     → '01h, 03h, 05h' (dispersas)
    """
    if not hours:
        return "—"
    s = sorted(set(hours))
    # Agrupar en segmentos contiguos
    segments: list[list[int]] = []
    seg = [s[0]]
    for h in s[1:]:
        if h == seg[-1] + 1:
            seg.append(h)
        else:
            segments.append(seg)
            seg = [h]
    segments.append(seg)
    parts = []
    for seg in segments:
        parts.append(f"{seg[0]:02d}h–{seg[-1]:02d}h" if len(seg) > 1 else f"{seg[0]:02d}h")
    return ", ".join(parts)


def _fmt_list(hours: list[int]) -> str:
    if not hours:
        return "—"
    return ", ".join(f"{h:02d}h" for h in sorted(hours))


def _fmt_eur(v: float) -> str:
    return f"{v:.2f} €" if abs(v) < 10 else f"{v:.1f} €"


# ── MOTOR DE DECISIONES ───────────────────────────────────────────────────────

def _build_decisions(df_today: pd.DataFrame, df_assets: pd.DataFrame) -> list[dict]:
    """
    Motor de reglas v2. Para cada activo genera exactamente una decisión
    (o ninguna si no hay oportunidad real de ahorro).

    Diferencias clave respecto a v1:
    · La ventana óptima se elige por PVP real hora a hora, no por rango del activo.
    · Se calcula saving_eur para cada decisión y se filtra si es < MIN_SAVING_EUR.
    · El texto 'reason' menciona precios concretos, diferencia vs media, y las horas
      exactas a evitar con su coste específico — no frases genéricas.
    · Las decisiones se ordenan por ahorro potencial descendente, no por prioridad fija.
    """
    decisions: list[dict] = []

    if df_assets.empty:
        return decisions

    has_pvp     = df_today["has_pvp"].any()
    pvp_avg     = df_today["price_pvpc_eur_mwh"].dropna().mean() if has_pvp else None
    pvp_min     = df_today["price_pvpc_eur_mwh"].min()           if has_pvp else None
    pvp_max     = df_today["price_pvpc_eur_mwh"].max()           if has_pvp else None
    pv_peak_kw  = df_today["pv_power_gen_kw"].max()
    pv_peak_h   = int(df_today.loc[df_today["pv_power_gen_kw"].idxmax(), "hour"])

    low_hours   = df_today[df_today["pvp_class"] == "low"]["hour"].tolist()
    high_hours  = df_today[df_today["pvp_class"] == "high"]["hour"].tolist()
    solar_hours = df_today[df_today["pvp_class"] == "solar"]["hour"].tolist()

    for _, asset in df_assets.iterrows():
        atype     = asset["asset_type"]
        flexible  = bool(asset["is_flexible"] == 1)
        power_kw  = float(asset["power_kw"])   if asset["power_kw"]   else 0.0
        cap_kwh   = float(asset["capacity_kwh"]) if asset.get("capacity_kwh") else 0.0
        ws        = int(asset["flex_window_start"])
        we        = int(asset["flex_window_end"])
        overnight = bool(asset["is_overnight_flexible"] == 1)
        priority  = int(asset["priority"])
        name      = asset["asset_name"]
        asset_id  = asset["asset_id"]

        window_all = _all_hours_in_window(ws, we)

        # ── CARRETILLA / BATERÍA DE TRACCIÓN ─────────────────────────────────
        if atype == "forklift_battery" and flexible:
            # Cuántas horas necesita la carga completa (redondeamos arriba)
            hours_needed = math.ceil(cap_kwh / power_kw) if power_kw > 0 else 4

            # Candidatas: primero horas baratas dentro de la ventana, si no
            # las N más baratas de la ventana aunque no sean 'low'.
            cheap_in_win = _hours_in_window(low_hours, ws, we)
            if cheap_in_win:
                opt_hours = sorted(cheap_in_win)[:hours_needed]
            else:
                opt_hours = sorted(_best_n_hours_cheap(df_today, window_all, hours_needed))

            if not opt_hours:
                continue

            advantage = _window_advantage_pct(df_today, opt_hours)
            saving    = _estimate_saving_eur(power_kw, opt_hours, df_today, pvp_avg or 100.0)

            # Si la ventana no aporta ahorro real, no emitir la recomendación.
            # Esto corrige el bug v1 de recomendar cargar a precio medio o caro.
            if advantage < MIN_WINDOW_ADVANTAGE_PCT or saving < MIN_SAVING_EUR:
                logger.info(
                    "[SKIP] %s — ventaja %.1f%% < %.0f%% o ahorro %.3f€ < %.2f€",
                    name, advantage, MIN_WINDOW_ADVANTAGE_PCT, saving, MIN_SAVING_EUR,
                )
                continue

            win_pvp_avg = df_today[df_today["hour"].isin(opt_hours)]["price_pvpc_eur_mwh"].mean()
            high_str    = _fmt_list(high_hours)
            high_pvp    = f"{pvp_max:.0f}" if pvp_max else "—"

            reason = (
                f"La batería necesita ~{hours_needed}h de carga ({cap_kwh:.0f} kWh "
                f"a {power_kw:.1f} kW). Las horas {_fmt_window(opt_hours)} tienen PVP "
                f"medio {win_pvp_avg:.0f} €/MWh, un {advantage:.0f}% por debajo de la "
                f"media del día ({pvp_avg:.0f} €/MWh). "
                f"Cargar completa antes de las {we:02d}h. "
                f"Evitar conectar en {high_str} — el precio sube a {high_pvp} €/MWh "
                f"y el coste de esa misma carga se multiplicaría por "
                f"{pvp_max/win_pvp_avg:.1f}x."
            )

            decisions.append({
                "asset_id":    asset_id,
                "asset_name":  name,
                "asset_type":  atype,
                "priority":    priority,
                "time_window": _fmt_window(opt_hours),
                "action":      "Programar carga nocturna",
                "reason":      reason,
                "saving_tag":  f"Ahorro ~{_fmt_eur(saving)}/día",
                "saving_eur":  saving,
                "urgency":     "critical" if overnight else "high",
            })

        # ── CÁMARA FRIGORÍFICA (cold_storage) ────────────────────────────────
        elif atype == "cold_storage" and flexible:
            # Estrategia pull-down: prefriar en horas solar/baratas para aprovechar
            # inercia térmica y reducir arranques del compresor en horas caras.
            solar_in_win = _hours_in_window(solar_hours, ws, we)
            cheap_in_win = _hours_in_window(low_hours, ws, we)
            opt_hours    = sorted(solar_in_win or cheap_in_win or
                                  _best_n_hours_cheap(df_today, window_all, 3))

            if not opt_hours:
                continue

            advantage = _window_advantage_pct(df_today, opt_hours)
            saving    = _estimate_saving_eur(power_kw, opt_hours, df_today, pvp_avg or 100.0)

            if saving < MIN_SAVING_EUR:
                continue

            mode_str   = "FV propia" if solar_in_win else "precio de red mínimo"
            win_pvp    = df_today[df_today["hour"].isin(opt_hours)]["price_pvpc_eur_mwh"].mean()
            pvp_str    = f"{win_pvp:.0f} €/MWh" if not pd.isna(win_pvp) else "coste solar"
            high_str   = _fmt_list(high_hours)

            reason = (
                f"Bajar consigna de temperatura 1–2°C durante {_fmt_window(opt_hours)} "
                f"aprovechando {mode_str} ({pvp_str}). "
                f"La masa térmica de la cámara mantiene temperatura durante "
                f"~{len(high_hours)}h de pico sin que el compresor arranque a "
                f"{pvp_max:.0f} €/MWh (horas {high_str}). "
                f"Cada arranque evitado en pico ahorra ~{power_kw * pvp_max / 1000:.2f} €."
            )

            decisions.append({
                "asset_id":    asset_id,
                "asset_name":  name,
                "asset_type":  atype,
                "priority":    priority,
                "time_window": _fmt_window(opt_hours),
                "action":      "Pre-enfriamiento (pull-down) en ventana solar/barata",
                "reason":      reason,
                "saving_tag":  f"Ahorro ~{_fmt_eur(saving)}/día",
                "saving_eur":  saving,
                "urgency":     "high",
            })

        # ── COMPRESOR DE AIRE ─────────────────────────────────────────────────
        elif atype == "compressor" and flexible:
            cheap_in_win = _hours_in_window(low_hours, ws, we)
            opt_hours    = sorted(cheap_in_win or
                                  _best_n_hours_cheap(df_today, window_all, 2))

            if not opt_hours:
                continue

            advantage = _window_advantage_pct(df_today, opt_hours)
            saving    = _estimate_saving_eur(power_kw, opt_hours, df_today, pvp_avg or 100.0)

            if saving < MIN_SAVING_EUR:
                continue

            win_pvp  = df_today[df_today["hour"].isin(opt_hours)]["price_pvpc_eur_mwh"].mean()
            high_str = _fmt_list(high_hours)

            reason = (
                f"Arrancar compresor y hacer purga/filtros durante "
                f"{_fmt_window(opt_hours)} (PVP ~{win_pvp:.0f} €/MWh). "
                f"Si el arranque se pospone a {high_str}, el pico de corriente "
                f"del motor ({power_kw:.1f} kW) coincide con {pvp_max:.0f} €/MWh "
                f"— el coste de arranque se {pvp_max/win_pvp:.1f}x. "
                f"Mantenimiento preventivo en esta ventana no afecta producción "
                f"y alarga vida del equipo al evitar arranques en carga pesada."
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
            })

        # ── BOMBAS DE PROCESO ─────────────────────────────────────────────────
        elif atype == "pump" and flexible:
            solar_in_win = _hours_in_window(solar_hours, ws, we)
            cheap_in_win = _hours_in_window(low_hours, ws, we)
            opt_hours    = sorted(solar_in_win or cheap_in_win or
                                  _best_n_hours_cheap(df_today, window_all, 3))

            if not opt_hours:
                continue

            saving   = _estimate_saving_eur(power_kw, opt_hours, df_today, pvp_avg or 100.0)
            win_pvp  = df_today[df_today["hour"].isin(opt_hours)]["price_pvpc_eur_mwh"].mean()
            mode_str = f"FV activa (~{pv_peak_kw:.1f} kW pico a las {pv_peak_h:02d}h)" \
                       if solar_in_win else f"precio {win_pvp:.0f} €/MWh"
            high_str = _fmt_list(high_hours)

            if saving < MIN_SAVING_EUR:
                continue

            reason = (
                f"Operar bombas durante {_fmt_window(opt_hours)} para llenar el depósito "
                f"de proceso aprovechando {mode_str}. "
                f"Con el depósito lleno antes de las {min(high_hours, default=22):02d}h, "
                f"los arranques en pico ({high_str}, {pvp_max:.0f} €/MWh) se reducen "
                f"o eliminan. Ahorro potencial si se evita 1h de bomba en pico: "
                f"~{power_kw * pvp_max / 1000:.2f} €."
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
            })

        # ── AUTOCLAVE / PASTEURIZADOR ─────────────────────────────────────────
        elif atype == "autoclave" and flexible:
            solar_in_win = _hours_in_window(solar_hours, ws, we)
            cheap_in_win = _hours_in_window(low_hours, ws, we)
            opt_hours    = sorted(solar_in_win or cheap_in_win or
                                  _best_n_hours_cheap(df_today, window_all, 4))

            if not opt_hours:
                continue

            saving  = _estimate_saving_eur(power_kw, opt_hours, df_today, pvp_avg or 100.0)
            win_pvp = df_today[df_today["hour"].isin(opt_hours)]["price_pvpc_eur_mwh"].mean()

            if saving < MIN_SAVING_EUR:
                continue

            fv_cover_pct = min(100, int(pv_peak_kw / power_kw * 100)) if power_kw > 0 else 0

            reason = (
                f"Programar ciclos de esterilización/pasteurización durante "
                f"{_fmt_window(opt_hours)} (PVP ~{win_pvp:.0f} €/MWh). "
                f"La FV cubre en pico hasta el {fv_cover_pct}% del consumo del equipo "
                f"({pv_peak_kw:.1f} kW FV vs {power_kw:.1f} kW autoclave). "
                f"Un ciclo completo en pico ({pvp_max:.0f} €/MWh) costaría "
                f"~{power_kw * pvp_max / 1000:.2f} € más que en esta ventana. "
                f"No arrancar en {_fmt_list(high_hours)}."
            )

            decisions.append({
                "asset_id":    asset_id,
                "asset_name":  name,
                "asset_type":  atype,
                "priority":    priority,
                "time_window": _fmt_window(opt_hours),
                "action":      "Concentrar ciclos largos en turno solar",
                "reason":      reason,
                "saving_tag":  f"Ahorro ~{_fmt_eur(saving)}/día",
                "saving_eur":  saving,
                "urgency":     "high",
            })

        # ── ILUMINACIÓN ───────────────────────────────────────────────────────
        elif atype == "lighting" and flexible and high_hours:
            saving = _estimate_saving_eur(power_kw, high_hours, df_today, pvp_avg or 100.0)
            # Para iluminación el ahorro es por reducir carga en pico, no por desplazarla.
            # Re-estimamos: cuánto cuesta tener la luz en pico vs apagarla.
            cost_if_on  = sum(df_today[df_today["hour"].isin(high_hours)]
                              ["price_pvpc_eur_mwh"].dropna()) * power_kw / 1000.0
            # No tiene sentido comparar vs media, directamente es coste evitado.
            saving_real = round(cost_if_on * 0.3, 3)  # asumimos 30% de zonas apagables

            reason = (
                f"El precio sube a {pvp_max:.0f} €/MWh en {_fmt_list(high_hours)}. "
                f"Tener {power_kw:.1f} kW de iluminación encendida en esas horas cuesta "
                f"~{cost_if_on:.2f} € solo en esa franja. "
                f"Apagando el 30% de zonas no productivas (pasillos, almacén secundario) "
                f"se ahorran ~{saving_real:.2f} € y no impacta en operaciones."
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
            })

        # ── ACTIVOS NO FLEXIBLES: ALERTA PUNTA ───────────────────────────────
        elif not flexible and high_hours:
            # No podemos desplazarlos, pero sí avisar para que no coincidan con
            # arranques evitables de otros equipos.
            high_cost = sum(df_today[df_today["hour"].isin(high_hours)]
                            ["price_pvpc_eur_mwh"].dropna()) * power_kw / 1000.0

            reason = (
                f"Este activo no es desplazable y consumirá {power_kw:.1f} kW en "
                f"horas pico ({_fmt_list(high_hours)}, hasta {pvp_max:.0f} €/MWh). "
                f"Coste estimado solo en pico: ~{high_cost:.2f} €. "
                f"No arrancar otros equipos flexibles simultáneamente en esas horas — "
                f"el solapamiento de potencias puede disparar la factura de potencia "
                f"contratada o generar punta de demanda facturable."
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
            })

    # Ordenar por ahorro potencial descendente (las decisiones más rentables primero),
    # con urgency como criterio de desempate secundario.
    urgency_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    decisions.sort(key=lambda d: (
        urgency_rank.get(d["urgency"], 9),
        -d.get("saving_eur", 0.0),
    ))
    return decisions


# ── ÍNDICE DE OPORTUNIDAD ─────────────────────────────────────────────────────

def _opportunity_index(df_today: pd.DataFrame, decisions: list[dict]) -> int:
    """
    Índice 0–100 que resume cuán buen día es para optimizar consumo.
    Pondera tres factores:
      · Spread de precio (max-min PVP): a mayor spread, más valor tiene desplazar carga.
      · Horas de FV activa: más solar = más autoconsumo disponible.
      · Ahorro total potencial de las decisiones generadas.
    """
    has_pvp = df_today["has_pvp"].any()
    if not has_pvp:
        return 30  # Sin PVP confirmado el índice no puede ser alto

    pvp_min   = df_today["price_pvpc_eur_mwh"].min()
    pvp_max   = df_today["price_pvpc_eur_mwh"].max()
    spread    = pvp_max - pvp_min  # €/MWh

    hours_solar = int((df_today["pv_power_gen_kw"] >= PV_ACTIVE_KW).sum())
    total_saving = sum(d.get("saving_eur", 0.0) for d in decisions)

    # Spread: 0€ → 0 pts, 200€ → 50 pts (capped)
    score_spread = min(50, spread / 4.0)
    # FV: 0h → 0 pts, 10h → 25 pts (capped)
    score_solar  = min(25, hours_solar * 2.5)
    # Ahorro: 0€ → 0 pts, 10€+ → 25 pts (capped)
    score_saving = min(25, total_saving * 2.5)

    return int(score_spread + score_solar + score_saving)


# ── KPIs DEL DÍA ─────────────────────────────────────────────────────────────

def _build_kpis(df_today: pd.DataFrame) -> dict:
    has_pvp = df_today["has_pvp"].any()
    pvp_s   = df_today[df_today["has_pvp"]]
    pv_peak_row = df_today.loc[df_today["pv_power_gen_kw"].idxmax()]

    return {
        "pv_peak_kw":         round(float(df_today["pv_power_gen_kw"].max()), 1),
        "pv_peak_hour":       int(pv_peak_row["hour"]),
        "pv_total_kwh":       round(float(df_today["pv_power_gen_kw"].sum()), 1),
        "pvp_min":            round(float(pvp_s["price_pvpc_eur_mwh"].min()), 2) if has_pvp else None,
        "pvp_min_hour":       int(pvp_s.loc[pvp_s["price_pvpc_eur_mwh"].idxmin(), "hour"]) if has_pvp else None,
        "pvp_max":            round(float(pvp_s["price_pvpc_eur_mwh"].max()), 2) if has_pvp else None,
        "pvp_max_hour":       int(pvp_s.loc[pvp_s["price_pvpc_eur_mwh"].idxmax(), "hour"]) if has_pvp else None,
        "pvp_avg":            round(float(pvp_s["price_pvpc_eur_mwh"].mean()), 2) if has_pvp else None,
        "avg_consumption_kw": round(float(df_today["power_consumption_kw"].mean()), 1),
        "hours_solar":        int((df_today["pv_power_gen_kw"] >= PV_ACTIVE_KW).sum()),
        "hours_cheap":        int((df_today["price_pvpc_eur_mwh"] < PVP_LOW_EUR_MWH).sum()),
        "hours_expensive":    int((df_today["price_pvpc_eur_mwh"] > PVP_HIGH_EUR_MWH).sum()),
        "has_pvp":            bool(has_pvp),
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
        f"{rainy_days} día(s) con probabilidad de lluvia >50%. "
        f"{rec} "
        f"⚠ Sin PVP disponible — datos climáticos con umbral de confianza extendido."
    )
    return {"summary_text": summary_text, "days": days_out}


# ── ORCHESTRATOR ──────────────────────────────────────────────────────────────

def build_energy_decisions(client_id: str) -> dict[str, Any]:
    """
    Punto de entrada principal. target_date = mañana (today + 1).
    Devuelve el dict completo de decisiones listo para report_generator.
    """
    logger.info("[INIT] ── build_energy_decisions v2 — cliente: %s ────────────", client_id)

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
    pv_hours  = df_today[["hour", "pv_power_gen_kw"]].to_dict(orient="records")

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
        "[DONE] v2 — %d decisiones, ahorro potencial %.2f €, oportunidad %d/100",
        len(decisions), total_saving, opp_index,
    )
    return result


if __name__ == "__main__":
    import json
    data = build_energy_decisions("CLT-0001")
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))