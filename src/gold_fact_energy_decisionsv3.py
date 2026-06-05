"""
gold_fact_energy_decisions_v3.py
────────────────────────────────
Motor de decisiones energéticas — versión 3.0 (Agente IA)

CAMBIO PRINCIPAL:
  El motor de reglas se reemplaza por un Agente de IA que recibe el mismo
  contexto (df_today, df_assets) y devuelve decisiones en el mismo formato.
  El resto del pipeline permanece idéntico.

CONFIGURACIÓN:
    ENERGY_LLM_API_KEY=
    ENERGY_LLM_MODEL=llama-3.3-70b-versatile
    ENERGY_LLM_BASE_URL=https://api.groq.com/openai/v1
    ENERGY_LLM_TEMPERATURE=0.2
    ENERGY_LLM_MAX_TOKENS=4000
    ENERGY_LLM_FALLBACK_RULES=true

Si no hay LLM configurado, fallback automático al motor de reglas v2.1.
"""

from __future__ import annotations

import json
import math
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np
from dotenv import load_dotenv
from sqlalchemy import text

from database_utils import get_engine
from logger_config import setup_logging

# ── IMPORT PARA LLM ──────────────────────────────────────────────────────────
try:
    import openai
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

load_dotenv()
logger = setup_logging()

# ── UMBRALES ─────────────────────────────────────────────────────────────────
PVP_LOW_EUR_MWH = 80.0
PVP_HIGH_EUR_MWH = 150.0
PV_ACTIVE_KW = 1.0
MIN_SAVING_EUR = 0.05
DEFAULT_CHARGE_HOURS = 4

# ── CONFIG LLM ───────────────────────────────────────────────────────────────
LLM_MODEL = os.getenv("ENERGY_LLM_MODEL", "llama-3.3-70b-versatile")
LLM_API_KEY = os.getenv("ENERGY_LLM_API_KEY")
LLM_BASE_URL = "https://api.groq.com/openai/v1"
LLM_TEMPERATURE = float(os.getenv("ENERGY_LLM_TEMPERATURE", "0.2"))
LLM_MAX_TOKENS = int(os.getenv("ENERGY_LLM_MAX_TOKENS", "4000"))
USE_LLM_FALLBACK = os.getenv("ENERGY_LLM_FALLBACK_RULES", "true").lower() == "true"


# ═════════════════════════════════════════════════════════════════════════════
#  EXTRACT (idéntico a v2.1)
# ═════════════════════════════════════════════════════════════════════════════

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
    tz = ZoneInfo(_get_client_tz(conn, client_id))
    dt_start = datetime(target_date.year, target_date.month, target_date.day,
                        0, 0, 0, tzinfo=tz).astimezone(timezone.utc)
    dt_end = (datetime(target_date.year, target_date.month, target_date.day,
                       tzinfo=tz) + timedelta(days=6)).astimezone(timezone.utc)

    rows = conn.execute(text("""
        SELECT forecast_time_utc, pv_power_gen_kw, pv_performance_ratio,
               poa_wm2, t_cell_celsius, power_consumption_kw,
               temp_celsius, humidity_pct, clouds_pct, rain_prob_norm,
               wind_speed_mps, price_pvpc_eur_mwh, weather_id
        FROM gold.fact_energy_forecast
        WHERE client_id = :cid
          AND forecast_time_utc >= :dt_start
          AND forecast_time_utc <  :dt_end
        ORDER BY forecast_time_utc
    """), {"cid": client_id, "dt_start": dt_start, "dt_end": dt_end}).fetchall()

    if not rows:
        logger.warning("[EXTRACT] Sin previsión para cliente '%s' en %s", client_id, target_date)
        return pd.DataFrame()

    df = pd.DataFrame([dict(r._mapping) for r in rows])
    df["forecast_time_utc"] = pd.to_datetime(df["forecast_time_utc"], utc=True)
    df["forecast_time_local"] = df["forecast_time_utc"].dt.tz_convert(tz)
    df["date"] = df["forecast_time_local"].dt.date
    df["hour"] = df["forecast_time_local"].dt.hour
    df["has_pvp"] = df["price_pvpc_eur_mwh"].notna()
    return df


def _get_client_tz(conn, client_id: str) -> str:
    row = conn.execute(text(
        "SELECT timezone FROM gold.dim_client WHERE client_id = :cid"
    ), {"cid": client_id}).fetchone()
    return row.timezone if row else "Europe/Madrid"


# ═════════════════════════════════════════════════════════════════════════════
#  CLASIFICACIÓN HORARIA Y HELPERS (idénticos a v2.1)
# ═════════════════════════════════════════════════════════════════════════════

def _classify_hour(pvp: float | None, pv_kw: float) -> str:
    if pv_kw >= PV_ACTIVE_KW:
        return "solar"
    if pvp is None:
        return "mid"
    if pvp < PVP_LOW_EUR_MWH:
        return "low"
    if pvp > PVP_HIGH_EUR_MWH:
        return "high"
    return "mid"


def _safe_float(val, default: float = 0.0) -> float:
    try:
        v = float(val)
        return v if not math.isnan(v) else default
    except (TypeError, ValueError):
        return default


def _hours_in_window(hours: list[int], ws: int, we: int) -> list[int]:
    if ws <= we:
        return [h for h in hours if ws <= h <= we]
    return [h for h in hours if h >= ws or h <= we]


def _all_hours_in_window(ws: int, we: int) -> list[int]:
    if ws <= we:
        return list(range(ws, we + 1))
    return list(range(ws, 24)) + list(range(0, we + 1))


def _best_n_hours_cheap(df: pd.DataFrame, candidate_hours: list[int], n: int) -> list[int]:
    if not candidate_hours or n <= 0:
        return []
    sub = (df[df["hour"].isin(candidate_hours)]
           .dropna(subset=["price_pvpc_eur_mwh"])
           .sort_values("price_pvpc_eur_mwh", ascending=True))
    return sub["hour"].head(n).tolist()


def _best_consecutive_block(df: pd.DataFrame, candidate_hours: list[int], n: int) -> list[int]:
    if not candidate_hours or n <= 0:
        return []
    s = sorted(set(candidate_hours))
    if len(s) <= n:
        return s
    blocks = []
    current = [s[0]]
    for h in s[1:]:
        if h == current[-1] + 1:
            current.append(h)
        else:
            blocks.append(current)
            current = [h]
    blocks.append(current)

    best_cost = float("inf")
    best_block = s[:n]

    for blk in blocks:
        if len(blk) < n:
            continue
        for start in range(len(blk) - n + 1):
            window = blk[start:start + n]
            prices = df[df["hour"].isin(window)]["price_pvpc_eur_mwh"].dropna()
            if prices.empty:
                continue
            cost = prices.mean()
            if cost < best_cost:
                best_cost = cost
                best_block = window

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
    if not opt_hours or power_kw <= 0:
        return 0.0
    sub = df[df["hour"].isin(opt_hours)]["price_pvpc_eur_mwh"].dropna()
    if sub.empty:
        return 0.0
    saving = sum((pvp_ref - p) * power_kw / 1000.0 for p in sub)
    return round(max(0.0, saving), 3)


def _window_avg_pvp(df: pd.DataFrame, window_hours: list[int]) -> float:
    sub = df[df["hour"].isin(window_hours)]["price_pvpc_eur_mwh"].dropna()
    if sub.empty:
        return float(df["price_pvpc_eur_mwh"].dropna().mean() or 100.0)
    return float(sub.mean())


def _fmt_window(hours: list[int]) -> str:
    if not hours:
        return "—"
    s = sorted(set(hours))
    segments = []
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
    if ws <= we:
        return f"{ws:02d}h–{we:02d}h"
    return f"{ws:02d}h–{we:02d}h (+1d)"


# ═════════════════════════════════════════════════════════════════════════════
#  AGENTE IA — NUEVO EN v3.0
# ═════════════════════════════════════════════════════════════════════════════

def _build_system_prompt() -> str:
    return """Eres un agente de optimización energética industrial. Tu trabajo es analizar los datos de previsión de un día (precios de electricidad, generación fotovoltaica, consumo de fábrica) y una lista de activos industriales, y decidir CUÁNDO y CÓMO operar cada activo para minimizar costes energéticos.

REGLAS DE NEGOCIO FUNDAMENTALES:
1. La fábrica SIEMPRE consume más de lo que genera su FV. El autoconsumo reduce la factura pero NO hace que las horas solares sean gratuitas. Nunca asumas que operar a mediodía es gratis.
2. Los activos flexibles pueden desplazarse dentro de su ventana horaria [flex_window_start, flex_window_end]. Los no flexibles operan cuando toca y solo generan alertas.
3. Las horas se clasifican como: "solar" (FV > 1 kW), "low" (PVP < 80 €/MWh), "mid" (80-150 €/MWh), "high" (PVP > 150 €/MWh).
4. Para baterías Li-ion (forklift_battery) la carga DEBE ser contigua — no puedes interrumpir el ciclo BMS. Calcula hours_needed = ceil(capacity_kwh / power_kw).
5. El ahorro se calcula contra el precio medio de la VENTANA FLEXIBLE del activo, NO contra la media del día.
6. Si no hay horas baratas en la ventana, usa las mejores disponibles dentro de ella.
7. La iluminación no flexible solo genera decisiones en horas "high" (apagar zonas no productivas).
8. Activos no flexibles en horas pico generan alertas de monitorización, no acciones de desplazamiento.

TIPOS DE ACTIVOS Y ESTRATEGIAS:
- forklift_battery: Programar bloque contiguo de carga en horas baratas de su ventana. Urgencia "critical".
- cold_storage: Pre-enfriar (pull-down) 1-2°C en horas baratas para acumular inercia térmica y evitar arranques en pico. Urgencia "high".
- compressor: Programar arranque y mantenimiento (purga, filtros) en ventana barata. El arranque tiene pico de corriente 5-7x. Urgencia "medium".
- pump: Llenar depósito en horas baratas para no bombear en pico. Urgencia "medium".
- autoclave: Concentrar ciclos largos en horario barato. Urgencia "high".
- lighting: Si hay horas "high", sugerir apagar 30% de zonas no productivas. Urgencia "low". Solo si saving_eur >= 0.05€.
- Otros no flexibles: Alerta de monitorización si coinciden con horas "high". Urgencia "low".

FORMATO DE SALIDA (JSON estricto):
{
  "decisions": [
    {
      "asset_id": "string",
      "asset_name": "string",
      "asset_type": "string",
      "priority": int,
      "time_window": "string formato HHh–HHh o HHh, HHh",
      "action": "string descriptivo de la acción",
      "reason": "string detallado con cifras concretas: precios, potencias, comparativas. Mínimo 2 frases.",
      "saving_tag": "string con formato Ahorro ~X.XX €/día o Evitas ~X.XX € vs cargar en pico",
      "saving_eur": float (>= 0),
      "urgency": "critical|high|medium|low",
      "flex_window_label": "string formato HHh–HHh"
    }
  ]
}

RESTRICCIONES:
- Devuelve SOLO el JSON, sin markdown, sin explicaciones previas.
- Cada decisión debe tener saving_eur calculado realista basado en: power_kw × horas × (pvp_referencia - pvp_ventana) / 1000.
- Si un activo no tiene ventana viable, aun así propón el mejor bloque disponible dentro de su ventana.
- No omitas activos flexibles solo porque el ahorro sea pequeño. Si hay ahorro > 0.05€, emite la decisión.
- El campo "reason" debe ser técnico, concreto y justificar la ventana elegida con números."""


def _build_user_prompt(df_today: pd.DataFrame, df_assets: pd.DataFrame) -> str:
    has_pvp = df_today["has_pvp"].any()
    pvp_avg = df_today["price_pvpc_eur_mwh"].dropna().mean() if has_pvp else 100.0
    pvp_min = df_today["price_pvpc_eur_mwh"].min() if has_pvp else 0.0
    pvp_max = df_today["price_pvpc_eur_mwh"].max() if has_pvp else 200.0

    pv_peak_kw = df_today["pv_power_gen_kw"].max()
    pv_peak_h = int(df_today.loc[df_today["pv_power_gen_kw"].idxmax(), "hour"]) if pv_peak_kw > 0 else None

    avg_consumption_kw = df_today["power_consumption_kw"].dropna().mean()

    low_hours = df_today[df_today["pvp_class"] == "low"]["hour"].tolist()
    high_hours = df_today[df_today["pvp_class"] == "high"]["hour"].tolist()
    solar_hours = df_today[df_today["pvp_class"] == "solar"]["hour"].tolist()

    hourly_data = []
    for _, row in df_today.iterrows():
        hourly_data.append({
            "hour": int(row["hour"]),
            "pvp_eur_mwh": round(float(row["price_pvpc_eur_mwh"]), 2) if pd.notna(row["price_pvpc_eur_mwh"]) else None,
            "pv_gen_kw": round(float(row["pv_power_gen_kw"]), 2),
            "consumption_kw": round(float(row["power_consumption_kw"]), 2) if pd.notna(row["power_consumption_kw"]) else None,
            "class": row["pvp_class"]
        })

    assets_data = []
    for _, asset in df_assets.iterrows():
        power_kw = _safe_float(asset["power_kw"], 0.0)
        cap_kwh = _safe_float(asset.get("capacity_kwh"), 0.0)
        ws = int(_safe_float(asset["flex_window_start"], 0))
        we = int(_safe_float(asset["flex_window_end"], 23))

        hours_needed = None
        if asset["asset_type"] == "forklift_battery" and cap_kwh > 0 and power_kw > 0:
            hours_needed = math.ceil(cap_kwh / power_kw)

        assets_data.append({
            "asset_id": asset["asset_id"],
            "asset_name": asset["asset_name"],
            "asset_type": asset["asset_type"],
            "power_kw": power_kw,
            "capacity_kwh": cap_kwh if cap_kwh > 0 else None,
            "hours_needed": hours_needed,
            "is_flexible": bool(asset["is_flexible"] == 1),
            "flex_window_start": ws,
            "flex_window_end": we,
            "is_overnight": ws > we,
            "priority": int(_safe_float(asset["priority"], 99)),
            "notes": asset.get("notes", "")
        })

    hourly_json = json.dumps(hourly_data, indent=2, ensure_ascii=False)
    assets_json = json.dumps(assets_data, indent=2, ensure_ascii=False)

    target_date_str = str(df_today["date"].iloc[0])
    tz_str = str(df_today["forecast_time_local"].iloc[0].tzname()) if len(df_today) > 0 else "Europe/Madrid"

    lines = [
        "## DATOS DEL DÍA A OPTIMIZAR",
        "",
        f"Fecha: {target_date_str}",
        f"Zona horaria: {tz_str}",
        "",
        "### RESUMEN EJECUTIVO",
        f"- PVP medio del día: {pvp_avg:.1f} €/MWh",
        f"- PVP mínimo: {pvp_min:.1f} €/MWh | PVP máximo: {pvp_max:.1f} €/MWh",
        f"- Pico FV: {pv_peak_kw:.1f} kW a las {pv_peak_h:02d}h (si aplica)" if pv_peak_h is not None else "- Pico FV: 0 kW",
        f"- Consumo medio fábrica: {avg_consumption_kw:.1f} kW",
        f"- Horas baratas (PVP < 80): {_fmt_list(low_hours)}",
        f"- Horas caras (PVP > 150): {_fmt_list(high_hours)}",
        f"- Horas solares (FV > 1kW): {_fmt_list(solar_hours)}",
        "",
        "### DATOS HORA A HORA",
        "```json",
        hourly_json,
        "```",
        "",
        "### ACTIVOS A OPTIMIZAR",
        "```json",
        assets_json,
        "```",
        "",
        "### INSTRUCCIÓN",
        "Analiza los datos y genera las decisiones de optimización para cada activo.",
        "Para cada activo flexible, calcula:",
        "1. La ventana óptima dentro de su flex_window",
        "2. El ahorro en € comparado contra el precio medio de su ventana flexible",
        "3. Un reason técnico con cifras concretas",
        "",
        "Para activos no flexibles en horas pico, genera alertas de monitorización.",
        "",
        "Devuelve ÚNICAMENTE el JSON con la lista de decisions.",
    ]

    return "\n".join(lines)


def _call_llm(system_prompt: str, user_prompt: str) -> dict:
    if not HAS_OPENAI:
        raise RuntimeError("Paquete 'openai' no instalado")

    if not LLM_API_KEY:
        raise RuntimeError("Variable ENERGY_LLM_API_KEY no configurada")

    client = openai.OpenAI(
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL if LLM_BASE_URL else None
    )

    logger.info("[AGENTE] Llamando a LLM modelo=%s", LLM_MODEL)

    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        temperature=LLM_TEMPERATURE,
        max_tokens=LLM_MAX_TOKENS,
        response_format={"type": "json_object"}
    )

    content = response.choices[0].message.content
    logger.debug("[AGENTE] Respuesta cruda (primeros 500 chars): %s", content[:500])

    return json.loads(content)


def _validate_and_clean_decisions(raw_decisions: list[dict], df_assets: pd.DataFrame) -> list[dict]:
    required_keys = {
        "asset_id", "asset_name", "asset_type", "priority", "time_window",
        "action", "reason", "saving_tag", "saving_eur", "urgency", "flex_window_label"
    }

    valid_asset_ids = set(df_assets["asset_id"].tolist())
    cleaned = []

    for i, d in enumerate(raw_decisions):
        missing = required_keys - set(d.keys())
        if missing:
            logger.warning("[AGENTE] Decisión #%d incompleta, faltan: %s", i, missing)
            continue

        if d["asset_id"] not in valid_asset_ids:
            logger.warning("[AGENTE] Decisión #%d con asset_id desconocido: %s", i, d["asset_id"])
            continue

        d["priority"] = int(_safe_float(d.get("priority"), 99))
        # FIX: Asegurar que saving_eur sea un float válido, nunca NaN
        saving_val = d.get("saving_eur", 0.0)
        if isinstance(saving_val, (int, float)):
            d["saving_eur"] = float(saving_val) if not math.isnan(float(saving_val)) else 0.0
        else:
            d["saving_eur"] = 0.0
        d["saving_eur"] = max(0.0, d["saving_eur"])

        d["urgency"] = d.get("urgency", "low")
        if d["urgency"] not in {"critical", "high", "medium", "low"}:
            d["urgency"] = "low"

        asset_row = df_assets[df_assets["asset_id"] == d["asset_id"]].iloc[0]
        ws = int(_safe_float(asset_row["flex_window_start"], 0))
        we = int(_safe_float(asset_row["flex_window_end"], 23))
        d["flex_window_label"] = _fmt_flex_window_label(ws, we)

        cleaned.append(d)

    return cleaned


def _build_decisions_agent(df_today: pd.DataFrame, df_assets: pd.DataFrame) -> list[dict]:
    if df_assets.empty:
        return []

    if not HAS_OPENAI or not LLM_API_KEY:
        logger.warning("[AGENTE] LLM no disponible (openai=%s, key=%s), usando fallback a reglas",
                       HAS_OPENAI, bool(LLM_API_KEY))
        return _build_decisions_rules(df_today, df_assets)

    try:
        system_prompt = _build_system_prompt()
        user_prompt = _build_user_prompt(df_today, df_assets)

        result = _call_llm(system_prompt, user_prompt)
        raw_decisions = result.get("decisions", [])

        if not raw_decisions:
            logger.warning("[AGENTE] LLM devolvió decisions vacío, usando fallback")
            return _build_decisions_rules(df_today, df_assets)

        decisions = _validate_and_clean_decisions(raw_decisions, df_assets)

        urgency_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        decisions.sort(key=lambda d: (
            urgency_rank.get(d.get("urgency", "low"), 9),
            -d.get("saving_eur", 0.0),
        ))

        logger.info("[AGENTE] %d decisiones generadas por IA", len(decisions))
        return decisions

    except Exception as e:
        logger.error("[AGENTE] Error en LLM: %s", e)
        if USE_LLM_FALLBACK:
            logger.info("[AGENTE] Fallback a motor de reglas v2.1")
            return _build_decisions_rules(df_today, df_assets)
        raise


# ═════════════════════════════════════════════════════════════════════════════
#  MOTOR DE REGLAS v2.1 (fallback, preservado íntegramente)
#  FIX: Manejo seguro de NaN cuando no hay datos PVP
# ═════════════════════════════════════════════════════════════════════════════

def _build_decisions_rules(df_today: pd.DataFrame, df_assets: pd.DataFrame) -> list[dict]:
    decisions = []

    if df_assets.empty:
        return decisions

    has_pvp = df_today["has_pvp"].any()

    # FIX: Usar valores seguros cuando no hay PVP
    pvp_avg_raw = df_today["price_pvpc_eur_mwh"].dropna().mean()
    pvp_avg = float(pvp_avg_raw) if pd.notna(pvp_avg_raw) else 100.0

    pvp_min_raw = df_today["price_pvpc_eur_mwh"].min()
    pvp_min = float(pvp_min_raw) if pd.notna(pvp_min_raw) else 0.0

    pvp_max_raw = df_today["price_pvpc_eur_mwh"].max()
    pvp_max = float(pvp_max_raw) if pd.notna(pvp_max_raw) else 200.0

    pv_peak_kw = df_today["pv_power_gen_kw"].max()
    pv_peak_h = int(df_today.loc[df_today["pv_power_gen_kw"].idxmax(), "hour"])

    avg_consumption_kw = df_today["power_consumption_kw"].dropna().mean()

    low_hours = df_today[df_today["pvp_class"] == "low"]["hour"].tolist()
    high_hours = df_today[df_today["pvp_class"] == "high"]["hour"].tolist()
    solar_hours = df_today[df_today["pvp_class"] == "solar"]["hour"].tolist()
    cheap_hours = low_hours

    logger.debug(
        "[MOTOR-RULES] pvp_avg=%.0f low=%s high=%s solar=%s cheap=%s",
        pvp_avg, low_hours, high_hours, solar_hours, cheap_hours
    )

    for _, asset in df_assets.iterrows():
        atype = asset["asset_type"]
        flexible = bool(asset["is_flexible"] == 1)
        power_kw = _safe_float(asset["power_kw"], 0.0)
        cap_kwh = _safe_float(asset.get("capacity_kwh"), 0.0)
        ws = int(_safe_float(asset["flex_window_start"], 0))
        we = int(_safe_float(asset["flex_window_end"], 23))
        priority = int(_safe_float(asset["priority"], 99))
        name = asset["asset_name"]
        asset_id = asset["asset_id"]

        window_all = _all_hours_in_window(ws, we)

        if atype == "forklift_battery" and flexible:
            if cap_kwh > 0 and power_kw > 0:
                hours_needed = math.ceil(cap_kwh / power_kw)
            else:
                hours_needed = DEFAULT_CHARGE_HOURS
                logger.warning("[%s] cap_kwh o power_kw NULL/0 -> asumiendo %dh", name, hours_needed)

            cheap_in_win = _hours_in_window(cheap_hours, ws, we)
            if not cheap_in_win:
                cheap_in_win = window_all

            opt_hours = _best_consecutive_block(df_today, cheap_in_win, hours_needed)
            if not opt_hours:
                continue

            # FIX: Manejo seguro de NaN en precios
            win_pvp_series = df_today[df_today["hour"].isin(opt_hours)]["price_pvpc_eur_mwh"].dropna()
            win_pvp_avg = float(win_pvp_series.mean()) if not win_pvp_series.empty else 0.0
            win_pvp_str = f"{win_pvp_avg:.0f}" if win_pvp_avg > 0 else "—"

            cost_optimal = power_kw * len(opt_hours) * win_pvp_avg / 1000
            cost_peak = power_kw * len(opt_hours) * pvp_max / 1000

            # FIX: Evitar NaN cuando no hay PVP
            if win_pvp_avg > 0 and pvp_max > 0:
                cost_vs_peak = round(power_kw * len(opt_hours) * (pvp_max - win_pvp_avg) / 1000, 2)
            else:
                cost_vs_peak = 0.0

            reason = (
                f"La batería necesita {hours_needed}h de carga continua "
                f"({cap_kwh:.0f} kWh a {power_kw:.1f} kW). "
                f"El bloque óptimo es {_fmt_window(opt_hours)}: PVP medio "
                f"{win_pvp_str} €/MWh, coste total ~{cost_optimal:.2f} €. "
                f"Si se carga en pico ({_fmt_list(high_hours)}, {pvp_max:.0f} €/MWh) costaría "
                f"~{cost_peak:.2f} € — {cost_peak/max(cost_optimal,0.01):.1f}x más caro. "
                f"Conectar antes de las {opt_hours[0]:02d}h y no desenchufar hasta las {opt_hours[-1]:02d}h."
            )

            decisions.append({
                "asset_id": asset_id, "asset_name": name, "asset_type": atype,
                "priority": priority, "time_window": _fmt_window(opt_hours),
                "action": "Programar carga batería — bloque óptimo continuo",
                "reason": reason, "saving_tag": f"Evitas ~{_fmt_eur(abs(cost_vs_peak))} vs pico",
                "saving_eur": abs(cost_vs_peak), "urgency": "critical",
                "flex_window_label": _fmt_flex_window_label(ws, we),
            })

        elif atype == "cold_storage" and flexible:
            cheap_in_win = _hours_in_window(cheap_hours, ws, we)
            if not cheap_in_win:
                cheap_in_win = _best_n_hours_cheap(df_today, window_all, 4)
            opt_hours = sorted(cheap_in_win)
            if not opt_hours:
                continue

            saving = _estimate_saving_eur(power_kw, opt_hours, df_today, _window_avg_pvp(df_today, window_all))
            win_pvp_series = df_today[df_today["hour"].isin(opt_hours)]["price_pvpc_eur_mwh"].dropna()
            win_pvp_avg = float(win_pvp_series.mean()) if not win_pvp_series.empty else 0.0
            cost_compressor_peak = power_kw * len(high_hours) * pvp_max / 1000

            reason = (
                f"Bajar consigna 1–2°C durante {_fmt_window(opt_hours)} "
                f"(PVP ~{win_pvp_avg:.0f} €/MWh). "
                f"La masa térmica absorbe frío extra y mantiene temperatura durante "
                f"{len(high_hours)}h de pico sin arranques. "
                f"Compresor en pico costaría ~{cost_compressor_peak:.2f} €."
            )

            decisions.append({
                "asset_id": asset_id, "asset_name": name, "asset_type": atype,
                "priority": priority, "time_window": _fmt_window(opt_hours),
                "action": "Pre-enfriamiento pull-down en ventana barata",
                "reason": reason, "saving_tag": f"Ahorro ~{_fmt_eur(saving)}/día",
                "saving_eur": saving, "urgency": "high",
                "flex_window_label": _fmt_flex_window_label(ws, we),
            })

        elif atype == "compressor" and flexible:
            cheap_in_win = _hours_in_window(cheap_hours, ws, we)
            if not cheap_in_win:
                cheap_in_win = _best_n_hours_cheap(df_today, window_all, 2)
            opt_hours = sorted(cheap_in_win[:3])
            if not opt_hours:
                continue

            saving = _estimate_saving_eur(power_kw, opt_hours, df_today, _window_avg_pvp(df_today, window_all))
            win_pvp_series = df_today[df_today["hour"].isin(opt_hours)]["price_pvpc_eur_mwh"].dropna()
            win_pvp_avg = float(win_pvp_series.mean()) if not win_pvp_series.empty else 0.0

            reason = (
                f"Arrancar compresor y purga durante {_fmt_window(opt_hours)} "
                f"(PVP ~{win_pvp_avg:.0f} €/MWh). "
                f"El pico de arranque ({power_kw:.1f} kW) es 5–7x nominal — "
                f"evitar que coincida con horas caras ({_fmt_list(high_hours)})."
            )

            decisions.append({
                "asset_id": asset_id, "asset_name": name, "asset_type": atype,
                "priority": priority, "time_window": _fmt_window(opt_hours),
                "action": "Programar arranque y mantenimiento en ventana económica",
                "reason": reason, "saving_tag": f"Ahorro ~{_fmt_eur(saving)}/día",
                "saving_eur": saving, "urgency": "medium",
                "flex_window_label": _fmt_flex_window_label(ws, we),
            })

        elif atype == "pump" and flexible:
            cheap_in_win = _hours_in_window(cheap_hours, ws, we)
            if not cheap_in_win:
                cheap_in_win = _best_n_hours_cheap(df_today, window_all, 3)
            opt_hours = sorted(cheap_in_win)
            if not opt_hours:
                continue

            saving = _estimate_saving_eur(power_kw, opt_hours, df_today, _window_avg_pvp(df_today, window_all))
            win_pvp_series = df_today[df_today["hour"].isin(opt_hours)]["price_pvpc_eur_mwh"].dropna()
            win_pvp_avg = float(win_pvp_series.mean()) if not win_pvp_series.empty else 0.0
            avoid_h = min(high_hours) if high_hours else 22
            cost_if_peak = power_kw * len(opt_hours) * pvp_max / 1000

            reason = (
                f"Operar bombas durante {_fmt_window(opt_hours)} "
                f"(PVP ~{win_pvp_avg:.0f} €/MWh). "
                f"Depósito lleno antes de las {avoid_h:02d}h evita arranques en pico. "
                f"En pico costaría ~{cost_if_peak:.2f} €."
            )

            decisions.append({
                "asset_id": asset_id, "asset_name": name, "asset_type": atype,
                "priority": priority, "time_window": _fmt_window(opt_hours),
                "action": "Llenar depósito de proceso en horario barato",
                "reason": reason, "saving_tag": f"Ahorro ~{_fmt_eur(saving)}/día",
                "saving_eur": saving, "urgency": "medium",
                "flex_window_label": _fmt_flex_window_label(ws, we),
            })

        elif atype == "autoclave" and flexible:
            cheap_in_win = _hours_in_window(cheap_hours, ws, we)
            if not cheap_in_win:
                cheap_in_win = _best_n_hours_cheap(df_today, window_all, 4)
            opt_hours = sorted(cheap_in_win)
            if not opt_hours:
                continue

            saving = _estimate_saving_eur(power_kw, opt_hours, df_today, _window_avg_pvp(df_today, window_all))
            win_pvp_series = df_today[df_today["hour"].isin(opt_hours)]["price_pvpc_eur_mwh"].dropna()
            win_pvp_avg = float(win_pvp_series.mean()) if not win_pvp_series.empty else 0.0
            fv_cover_pct = min(100, int(pv_peak_kw / max(power_kw, 0.1) * 100))
            cost_if_peak = power_kw * len(opt_hours) * pvp_max / 1000

            reason = (
                f"Ciclos de esterilización en {_fmt_window(opt_hours)} "
                f"(PVP ~{win_pvp_avg:.0f} €/MWh). "
                f"FV cubre {fv_cover_pct}% del consumo en pico. "
                f"En pico costaría ~{cost_if_peak:.2f} €."
            )

            decisions.append({
                "asset_id": asset_id, "asset_name": name, "asset_type": atype,
                "priority": priority, "time_window": _fmt_window(opt_hours),
                "action": "Concentrar ciclos largos en turno barato",
                "reason": reason, "saving_tag": f"Ahorro ~{_fmt_eur(saving)}/día",
                "saving_eur": saving, "urgency": "high",
                "flex_window_label": _fmt_flex_window_label(ws, we),
            })

        elif atype == "lighting":
            if not high_hours:
                continue
            cost_if_on = sum(
                df_today[df_today["hour"].isin(high_hours)]["price_pvpc_eur_mwh"].dropna()
            ) * power_kw / 1000.0
            saving_real = round(cost_if_on * 0.30, 3)
            if saving_real < MIN_SAVING_EUR:
                continue

            reason = (
                f"PVP máximo {pvp_max:.0f} €/MWh en {_fmt_list(high_hours)}. "
                f"Apagando 30% de zonas no productivas se evitan ~{saving_real:.2f} €."
            )

            decisions.append({
                "asset_id": asset_id, "asset_name": name, "asset_type": atype,
                "priority": priority + 10, "time_window": _fmt_list(high_hours),
                "action": "Apagar iluminación no esencial en horas pico",
                "reason": reason, "saving_tag": f"Ahorro ~{_fmt_eur(saving_real)}/día",
                "saving_eur": saving_real, "urgency": "low",
                "flex_window_label": _fmt_flex_window_label(ws, we),
            })

        elif not flexible:
            if not high_hours:
                continue
            high_cost = sum(
                df_today[df_today["hour"].isin(high_hours)]["price_pvpc_eur_mwh"].dropna()
            ) * power_kw / 1000.0
            factory_kw_peak = df_today[df_today["hour"].isin(high_hours)][
                "power_consumption_kw"].mean()

            reason = (
                f"Activo no desplazable: {power_kw:.1f} kW en pico "
                f"({_fmt_list(high_hours)}). Coste: ~{high_cost:.2f} €. "
                f"Representa {min(100, int(power_kw/max(factory_kw_peak,1)*100))}% de la demanda."
            )

            decisions.append({
                "asset_id": asset_id, "asset_name": name, "asset_type": atype,
                "priority": priority + 50, "time_window": _fmt_list(high_hours),
                "action": "Monitorizar consumo — activo no flexible",
                "reason": reason, "saving_tag": "Alerta pico",
                "saving_eur": 0.0, "urgency": "low",
                "flex_window_label": _fmt_flex_window_label(ws, we),
            })

    urgency_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    decisions.sort(key=lambda d: (
        urgency_rank.get(d.get("urgency", "low"), 9),
        -d.get("saving_eur", 0.0),
    ))
    return decisions


# ═════════════════════════════════════════════════════════════════════════════
#  KPIs, INDEX, OUTLOOK (idénticos a v2.1)
# ═════════════════════════════════════════════════════════════════════════════

def _opportunity_index(df_today: pd.DataFrame, decisions: list[dict]) -> int:
    has_pvp = df_today["has_pvp"].any()
    if not has_pvp:
        return 30

    spread = df_today["price_pvpc_eur_mwh"].max() - df_today["price_pvpc_eur_mwh"].min()
    hours_solar = int((df_today["pv_power_gen_kw"] >= PV_ACTIVE_KW).sum())
    total_saving = sum(d.get("saving_eur", 0.0) for d in decisions)

    score_spread = min(40, spread / 3.0)
    score_solar = min(35, hours_solar * 3.5)
    score_saving = min(25, total_saving * 2.5)

    return min(100, int(score_spread + score_solar + score_saving))


def _build_kpis(df_today: pd.DataFrame) -> dict:
    has_pvp = df_today["has_pvp"].any()
    pvp_s = df_today[df_today["has_pvp"]]
    pv_peak_row = df_today.loc[df_today["pv_power_gen_kw"].idxmax()]

    return {
        "pv_peak_kw": round(float(df_today["pv_power_gen_kw"].max()), 1),
        "pv_peak_hour": int(pv_peak_row["hour"]),
        "pv_total_kwh": round(float(df_today["pv_power_gen_kw"].sum()), 1),
        "pvp_min": round(float(pvp_s["price_pvpc_eur_mwh"].min()), 2) if has_pvp else None,
        "pvp_min_hour": int(pvp_s.loc[pvp_s["price_pvpc_eur_mwh"].idxmin(), "hour"]) if has_pvp else None,
        "pvp_max": round(float(pvp_s["price_pvpc_eur_mwh"].max()), 2) if has_pvp else None,
        "pvp_max_hour": int(pvp_s.loc[pvp_s["price_pvpc_eur_mwh"].idxmax(), "hour"]) if has_pvp else None,
        "pvp_avg": round(float(pvp_s["price_pvpc_eur_mwh"].mean()), 2) if has_pvp else None,
        "avg_consumption_kw": round(float(df_today["power_consumption_kw"].mean()), 1),
        "hours_solar": int((df_today["pv_power_gen_kw"] >= PV_ACTIVE_KW).sum()),
        "hours_cheap": int((df_today["price_pvpc_eur_mwh"] < PVP_LOW_EUR_MWH).sum()),
        "hours_expensive": int((df_today["price_pvpc_eur_mwh"] > PVP_HIGH_EUR_MWH).sum()),
        "has_pvp": bool(has_pvp),
        "forecast_reliability": "alta" if has_pvp else "baja",
    }


def _build_outlook(df_forecast: pd.DataFrame, target_date: date) -> dict:
    future = df_forecast[df_forecast["date"] > target_date].copy()
    if future.empty:
        return {"summary_text": "Sin datos de previsión para los próximos días.", "days": []}

    days_out = []
    for day, grp in future.groupby("date"):
        wx_id = (grp["weather_id"].dropna().mode().iloc[0]
                 if not grp["weather_id"].dropna().empty else None)
        days_out.append({
            "date": str(day),
            "pv_peak_kw": round(float(grp["pv_power_gen_kw"].max()), 1),
            "clouds_pct": round(float(grp["clouds_pct"].mean()), 0),
            "rain_prob": round(float(grp["rain_prob_norm"].mean()), 2),
            "temp_max": round(float(grp["temp_celsius"].max()), 1),
            "temp_min": round(float(grp["temp_celsius"].min()), 1),
            "hours_pv": int((grp["pv_power_gen_kw"] >= PV_ACTIVE_KW).sum()),
            "weather_id": int(wx_id) if wx_id is not None else None,
            "reliability": "baja",
        })

    avg_pv = sum(d["pv_peak_kw"] for d in days_out) / len(days_out)
    avg_clouds = sum(d["clouds_pct"] for d in days_out) / len(days_out)
    rainy_days = sum(1 for d in days_out if d["rain_prob"] > 0.5)

    if avg_clouds < 40 and avg_pv > 7:
        tone = "semana con buena generación fotovoltaica prevista"
        rec = "Planificar cargas intensivas para mediodía solar."
    elif avg_clouds > 65 or rainy_days >= 3:
        tone = "semana con nubosidad alta y generación FV limitada"
        rec = "Priorizar eficiencia en consumo base. FV no será determinante."
    else:
        tone = "semana con generación FV moderada e inestable"
        rec = "Confirmar previsión cada mañana antes de planificar cargas."

    summary_text = (
        f"Previsión orientativa para los próximos {len(days_out)} días: {tone}. "
        f"FV media prevista {avg_pv:.1f} kW pico, nubosidad media {avg_clouds:.0f}%. "
        f"{rainy_days} día(s) con probabilidad de lluvia >50%. {rec} "
        f"⚠ Sin PVP disponible — datos climáticos con umbral de confianza extendido."
    )
    return {"summary_text": summary_text, "days": days_out}


# ═════════════════════════════════════════════════════════════════════════════
#  ORCHESTRATOR (interfaz idéntica, motor intercambiable)
# ═════════════════════════════════════════════════════════════════════════════

def build_energy_decisions(client_id: str) -> dict[str, Any]:
    logger.info("[INIT] ── build_energy_decisions v3.0 — cliente: %s ──", client_id)

    engine = get_engine()
    target_date = date.today() + timedelta(days=1)

    with engine.connect() as conn:
        client = _load_client(conn, client_id)
        df_assets = _load_assets(conn, client_id)
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

    kpis = _build_kpis(df_today)
    decisions = _build_decisions_agent(df_today, df_assets)
    outlook = _build_outlook(df_forecast, target_date)

    opp_index = _opportunity_index(df_today, decisions)
    # FIX: Asegurar que total_saving nunca sea NaN
    total_saving_raw = sum(d.get("saving_eur", 0.0) for d in decisions)
    total_saving = round(float(total_saving_raw) if not math.isnan(float(total_saving_raw)) else 0.0, 2)

    pvp_hours = df_today[["hour", "price_pvpc_eur_mwh", "pvp_class"]].to_dict(orient="records")
    pv_hours = df_today[["hour", "pv_power_gen_kw", "power_consumption_kw"]].to_dict(orient="records")

    tz_name = client.get("timezone", "Europe/Madrid")
    now_local = datetime.now(ZoneInfo(tz_name))

    result = {
        "client": client,
        "today": {
            "date": str(target_date),
            "pvp_hours": pvp_hours,
            "pv_hours": pv_hours,
            "kpis": kpis,
            "decisions": decisions,
            "opportunity_index": opp_index,
            "total_saving_eur": total_saving,
        },
        "outlook": outlook,
        "generated_at": now_local.strftime("%Y-%m-%d %H:%M hora local"),
    }

    logger.info(
        "[DONE] v3.0 — %d decisiones, ahorro ~%.2f €, oportunidad %d/100",
        len(decisions), total_saving, opp_index,
    )
    return result


if __name__ == "__main__":
    import json as _json
    data = build_energy_decisions("CLT-0001")
    print(_json.dumps(data, indent=2, ensure_ascii=False, default=str))