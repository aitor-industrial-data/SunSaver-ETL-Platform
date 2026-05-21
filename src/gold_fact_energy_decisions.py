"""
gold_fact_energy_decisions.py
─────────────────────────────
Motor de reglas energéticas para el informe diario de planta.

NO persiste en base de datos — devuelve un dict estructurado que el
report_generator consume directamente.

Entrada:
  · gold.fact_energy_forecast  (mañana + próximos 5 días, ya en DB)
  · gold.dim_assets             (activos del cliente)
  · gold.dim_client             (metadatos del cliente, incluye timezone)

Salida (dict):
  {
    "client":    {...},
    "today":     { "date", "pvp_hours": [...], "pv_hours": [...],
                   "kpis": {...}, "decisions": [...] },
    "outlook":   { "summary_text": str, "days": [...] },
    "generated_at": str,
  }

Nota de fechas:
  · Toda la lógica interna trabaja en UTC.
  · Se convierte a hora local (timezone del cliente) SOLO para
    las horas que se muestran en el informe y en los textos de decisión.
  · target_date = date.today() + 1 día  (el ETL corre a las ~21h locales,
    generando el informe del día siguiente completo 00–23h).
"""

from __future__ import annotations

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

# ── UMBRALES DEL MOTOR DE REGLAS ─────────────────────────────────────────────
PVP_LOW_EUR_MWH  = 80.0   # Por debajo → precio barato, activar cargas flexibles
PVP_HIGH_EUR_MWH = 150.0  # Por encima → precio caro, reducir consumo
PV_ACTIVE_KW     = 1.0    # Por encima → generación FV activa


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
    """
    Carga previsión para target_date y los 5 días siguientes.
    Convierte forecast_time_utc a hora local del cliente.
    flex_window_start/end del Excel están en hora local → no necesitan conversión.
    """
    # Ventana UTC: desde medianoche local del target_date hasta fin del día +5
    tz       = ZoneInfo(_get_client_tz(conn, client_id))
    dt_start = datetime(target_date.year, target_date.month, target_date.day,
                        0, 0, 0, tzinfo=tz).astimezone(timezone.utc)
    dt_end   = datetime(target_date.year, target_date.month, target_date.day,
                        tzinfo=tz) + timedelta(days=6)
    dt_end   = dt_end.astimezone(timezone.utc)

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
    df["forecast_time_utc"] = pd.to_datetime(df["forecast_time_utc"], utc=True)

    # Convertir a hora local — todo lo que ve el operario usa esta columna
    df["forecast_time_local"] = df["forecast_time_utc"].dt.tz_convert(tz)
    df["date"]   = df["forecast_time_local"].dt.date
    df["hour"]   = df["forecast_time_local"].dt.hour   # ← hora local España
    df["has_pvp"] = df["price_pvpc_eur_mwh"].notna()
    return df


def _get_client_tz(conn, client_id: str) -> str:
    """Helper para obtener timezone del cliente sin recargar todo el registro."""
    row = conn.execute(text(
        "SELECT timezone FROM gold.dim_client WHERE client_id = :cid"
    ), {"cid": client_id}).fetchone()
    return row.timezone if row else "Europe/Madrid"


# ── MOTOR DE REGLAS ───────────────────────────────────────────────────────────

def _classify_hour(pvp: float | None, pv_kw: float) -> str:
    """Devuelve 'low' | 'mid' | 'high' | 'solar' según condiciones horarias."""
    if pv_kw >= PV_ACTIVE_KW:
        return "solar"
    if pvp is None:
        return "mid"
    if pvp < PVP_LOW_EUR_MWH:
        return "low"
    if pvp > PVP_HIGH_EUR_MWH:
        return "high"
    return "mid"


def _build_decisions(df_today: pd.DataFrame, df_assets: pd.DataFrame) -> list[dict]:
    """
    Aplica el motor de reglas hora a hora por activo.
    Todas las horas en df_today["hour"] ya son hora local.
    Devuelve lista de decisiones ordenadas por prioridad.
    """
    decisions = []

    if df_assets.empty:
        return decisions

    # Ventanas clave del día en hora local
    low_hours   = df_today[df_today["pvp_class"] == "low"]["hour"].tolist()
    high_hours  = df_today[df_today["pvp_class"] == "high"]["hour"].tolist()
    solar_hours = df_today[df_today["pvp_class"] == "solar"]["hour"].tolist()

    pvp_min    = df_today["price_pvpc_eur_mwh"].min()
    pvp_max    = df_today["price_pvpc_eur_mwh"].max()
    pv_peak_kw = df_today["pv_power_gen_kw"].max()
    pv_peak_h  = int(df_today.loc[df_today["pv_power_gen_kw"].idxmax(), "hour"])

    for _, asset in df_assets.iterrows():
        atype     = asset["asset_type"]
        flexible  = asset["is_flexible"] == 1
        ws        = int(asset["flex_window_start"])   # ya en hora local (Excel)
        we        = int(asset["flex_window_end"])
        overnight = asset["is_overnight_flexible"] == 1

        # ── Carretillas / baterías ────────────────────────────────────────────
        if atype == "forklift_battery" and flexible:
            night_low  = [h for h in low_hours if ws <= h <= we]
            window_str = _fmt_window(night_low or list(range(ws, we + 1)))
            decisions.append({
                "asset_id":    asset["asset_id"],
                "asset_name":  asset["asset_name"],
                "asset_type":  atype,
                "priority":    int(asset["priority"]),
                "time_window": window_str,
                "action":      "Programar carga nocturna",
                "reason":      (
                    f"PVP en mínimos ({pvp_min:.0f} €/MWh). "
                    f"Cargar al 100% entre {window_str} antes del turno. "
                    f"Evitar carga en horas pico ({_fmt_list(high_hours)}, "
                    f">{PVP_HIGH_EUR_MWH:.0f} €/MWh)."
                ),
                "saving_tag":  "Ahorro en carga",
                "urgency":     "critical" if overnight else "high",
            })

        # ── Cámara frigorífica ────────────────────────────────────────────────
        elif atype == "cold_storage" and flexible:
            solar_in_window = [h for h in solar_hours if ws <= h <= we]
            best_hours      = solar_in_window or [h for h in low_hours if ws <= h <= we]
            window_str      = _fmt_window(best_hours) if best_hours else f"{ws:02d}h–{we:02d}h"
            decisions.append({
                "asset_id":    asset["asset_id"],
                "asset_name":  asset["asset_name"],
                "asset_type":  atype,
                "priority":    int(asset["priority"]),
                "time_window": window_str,
                "action":      "Pre-enfriamiento (pull-down) en ventana solar/barata",
                "reason":      (
                    f"Bajar consigna durante {window_str} aprovechando "
                    f"{'FV activa' if solar_in_window else 'PVP bajo'}. "
                    f"La inercia térmica reduce arranques en horas pico "
                    f"({_fmt_list(high_hours)}, >{PVP_HIGH_EUR_MWH:.0f} €/MWh)."
                ),
                "saving_tag":  "Diferir compresor",
                "urgency":     "high",
            })

        # ── Compresor de aire ─────────────────────────────────────────────────
        elif atype == "compressor" and flexible:
            maint_hours = [h for h in low_hours if ws <= h <= we]
            window_str  = _fmt_window(maint_hours) if maint_hours else f"{ws:02d}h–{we:02d}h"
            decisions.append({
                "asset_id":    asset["asset_id"],
                "asset_name":  asset["asset_name"],
                "asset_type":  atype,
                "priority":    int(asset["priority"]),
                "time_window": window_str,
                "action":      "Programar arranque y mantenimiento en ventana económica",
                "reason":      (
                    f"Arrancar compresor y hacer purga/filtros durante {window_str} "
                    f"(PVP ~{pvp_min:.0f}–{PVP_LOW_EUR_MWH:.0f} €/MWh). "
                    f"Evitar arranque en pico ({_fmt_list(high_hours)})."
                ),
                "saving_tag":  "Ventana mantenimiento",
                "urgency":     "medium",
            })

        # ── Bombas de proceso ─────────────────────────────────────────────────
        elif atype == "pump" and flexible:
            pump_hours = ([h for h in solar_hours if ws <= h <= we]
                          or [h for h in low_hours if ws <= h <= we])
            window_str = _fmt_window(pump_hours) if pump_hours else f"{ws:02d}h–{we:02d}h"
            decisions.append({
                "asset_id":    asset["asset_id"],
                "asset_name":  asset["asset_name"],
                "asset_type":  atype,
                "priority":    int(asset["priority"]),
                "time_window": window_str,
                "action":      "Llenar depósito de proceso en horario solar/barato",
                "reason":      (
                    f"Operar bombas durante {window_str} para llenar depósito. "
                    f"Evitar arranques en horas caras ({_fmt_list(high_hours)})."
                ),
                "saving_tag":  "Desplazar carga",
                "urgency":     "medium",
            })

        # ── Autoclave / pasteurizador ─────────────────────────────────────────
        elif atype == "autoclave" and flexible:
            prod_hours = ([h for h in solar_hours if ws <= h <= we]
                          or [h for h in low_hours if ws <= h <= we])
            window_str = _fmt_window(prod_hours) if prod_hours else f"{ws:02d}h–{we:02d}h"
            decisions.append({
                "asset_id":    asset["asset_id"],
                "asset_name":  asset["asset_name"],
                "asset_type":  atype,
                "priority":    int(asset["priority"]),
                "time_window": window_str,
                "action":      "Concentrar ciclos largos en turno solar",
                "reason":      (
                    f"Programar ciclos de esterilización/pasteurización durante "
                    f"{window_str}. FV pico {pv_peak_kw:.1f} kW a las {pv_peak_h:02d}h "
                    f"cubre parte del consumo. "
                    f"No arrancar en horas pico ({_fmt_list(high_hours)})."
                ),
                "saving_tag":  "FV activa",
                "urgency":     "high",
            })

        # ── Iluminación ───────────────────────────────────────────────────────
        elif atype == "lighting" and flexible:
            decisions.append({
                "asset_id":    asset["asset_id"],
                "asset_name":  asset["asset_name"],
                "asset_type":  atype,
                "priority":    int(asset["priority"]),
                "time_window": _fmt_list(high_hours) if high_hours else "—",
                "action":      "Apagar iluminación no esencial en horas pico",
                "reason":      (
                    f"Reducir iluminación de zonas no productivas durante horas pico "
                    f"({_fmt_list(high_hours)}, >{PVP_HIGH_EUR_MWH:.0f} €/MWh). "
                    f"PVP máx. del día: {pvp_max:.0f} €/MWh."
                ),
                "saving_tag":  "Reducción carga",
                "urgency":     "low",
            })

        # ── Activos no flexibles: alerta pico general ─────────────────────────
        elif not flexible and high_hours:
            decisions.append({
                "asset_id":    asset["asset_id"],
                "asset_name":  asset["asset_name"],
                "asset_type":  atype,
                "priority":    int(asset["priority"]) + 50,
                "time_window": _fmt_list(high_hours),
                "action":      "Monitorizar consumo — activo no flexible",
                "reason":      (
                    f"Este activo no es desplazable. "
                    f"Vigilar consumo en horas pico ({_fmt_list(high_hours)}). "
                    f"Asegurar que no arrancan otros equipos simultáneamente."
                ),
                "saving_tag":  "Alerta pico",
                "urgency":     "low",
            })

    decisions.sort(key=lambda d: (
        {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(d["urgency"], 9),
        d["priority"],
    ))
    return decisions


# ── OUTLOOK SEMANAL ───────────────────────────────────────────────────────────

def _build_outlook(df_forecast: pd.DataFrame, target_date: date) -> dict:
    """
    Genera resumen textual de los 5 días tras target_date (sin PVP).
    Confianza extendidda — solo orientativo.
    Las fechas ya están en hora local via df["date"].
    """
    future = df_forecast[df_forecast["date"] > target_date].copy()
    if future.empty:
        return {"summary_text": "Sin datos de previsión para los próximos días.", "days": []}

    days_out = []
    for day, grp in future.groupby("date"):
        # weather_id más frecuente del día para el icono representativo
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
        outlook_tone   = "semana con buena generación fotovoltaica prevista"
        recommendation = "Planificar cargas intensivas para mediodía solar."
    elif avg_clouds > 65 or rainy_days >= 3:
        outlook_tone   = "semana con nubosidad alta y generación FV limitada"
        recommendation = "Priorizar eficiencia en consumo base. FV no será determinante."
    else:
        outlook_tone   = "semana con generación FV moderada e inestable"
        recommendation = "Confirmar previsión cada mañana antes de planificar cargas."

    summary_text = (
        f"Previsión orientativa para los próximos {len(days_out)} días: {outlook_tone}. "
        f"FV media prevista {avg_pv:.1f} kW pico, nubosidad media {avg_clouds:.0f}%. "
        f"{rainy_days} día(s) con probabilidad de lluvia >50%. "
        f"{recommendation} "
        f"⚠ Sin PVP disponible — datos climáticos con umbral de confianza extendido."
    )

    return {"summary_text": summary_text, "days": days_out}


# ── KPIs DEL DÍA ─────────────────────────────────────────────────────────────

def _build_kpis(df_today: pd.DataFrame) -> dict:
    has_pvp = df_today["has_pvp"].any()
    pvp_s   = df_today[df_today["has_pvp"]]

    pv_peak_row = df_today.loc[df_today["pv_power_gen_kw"].idxmax()]

    return {
        "pv_peak_kw":         round(float(df_today["pv_power_gen_kw"].max()), 1),
        "pv_peak_hour":       int(pv_peak_row["hour"]),          # hora local
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


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _fmt_window(hours: list[int]) -> str:
    """[1,2,3,4,5] → '01h–05h'  (hora local)"""
    if not hours:
        return "—"
    return f"{min(hours):02d}h–{max(hours):02d}h"


def _fmt_list(hours: list[int]) -> str:
    """[19,20,21] → '19h, 20h, 21h'  (hora local)"""
    if not hours:
        return "—"
    return ", ".join(f"{h:02d}h" for h in sorted(hours))


# ── ORCHESTRATOR ──────────────────────────────────────────────────────────────

def build_energy_decisions(client_id: str) -> dict[str, Any]:
    """
    Punto de entrada principal.
    target_date = mañana (today + 1).
    Devuelve el dict completo de decisiones listo para el report_generator.
    """
    logger.info("[INIT] ── build_energy_decisions — cliente: %s ────────────", client_id)

    engine      = get_engine()
    target_date = date.today() + timedelta(days=1)

    logger.info("[INIT] Generando informe para: %s (hora local cliente)", target_date)

    with engine.connect() as conn:
        client      = _load_client(conn, client_id)
        df_assets   = _load_assets(conn, client_id)
        df_forecast = _load_forecast(conn, client_id, target_date)

    if df_forecast.empty:
        logger.error("[ERROR] Sin datos de previsión — abortando")
        return {}

    # ── Día objetivo ──────────────────────────────────────────────────────────
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

    # Serializar horas locales para el renderer (0–23h locales)
    pvp_hours = df_today[["hour", "price_pvpc_eur_mwh", "pvp_class"]].to_dict(orient="records")
    pv_hours  = df_today[["hour", "pv_power_gen_kw"]].to_dict(orient="records")

    # Timezone local para el header del informe
    tz_name = client.get("timezone", "Europe/Madrid")
    now_local = datetime.now(ZoneInfo(tz_name))

    result = {
        "client":   client,
        "today": {
            "date":      str(target_date),
            "pvp_hours": pvp_hours,
            "pv_hours":  pv_hours,
            "kpis":      kpis,
            "decisions": decisions,
        },
        "outlook":      outlook,
        "generated_at": now_local.strftime("%Y-%m-%d %H:%M hora local"),
    }

    logger.info(
        "[DONE] build_energy_decisions — %d decisiones generadas para %s",
        len(decisions), target_date,
    )
    return result


if __name__ == "__main__":
    import json
    data = build_energy_decisions("CLT-0001")
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))