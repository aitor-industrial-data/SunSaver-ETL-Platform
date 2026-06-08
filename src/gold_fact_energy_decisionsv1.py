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

import httpx

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
    return """Eres APEX — Agente de Planificación Energética eXperta. Tu única función es completar el razonamiento que te proporciona el usuario. NO inventes. NO uses plantillas. NO ignores los datos.

════════════════════════════════════════════════════════════
REGLA SUPREMA: USA LOS DATOS QUE TE PASAN
════════════════════════════════════════════════════════════

El usuario te pasa:
1. Datos hora a hora de FV, consumo, importación neta.
2. Para cada activo: un objeto "window_context" con el análisis YA HECHO.
3. El bloque óptimo YA CALCULADO en "optimal_charge_block".
4. Si hay PVP: precios reales. Si NO hay PVP: NO hay precios.

TÚ NO DEBES:
❌ Calcular bloques óptimos (ya están calculados).
❌ Inventar precios de 100 €/MWh.
❌ Decir "precio medio de la ventana" si no hay PVP.
❌ Usar la misma frase para dos activos distintos.
❌ Decir "ahorro de 0.0 €/día" sin explicar por qué.
❌ Ignorar si la ventana tiene o no tiene FV.
❌ Decir "la más barata" si no hay precios.
❌ Decir "menor consumo" — el consumo no cambia con la hora.

TÚ DEBES:
✅ Usar EXACTAMENTE el bloque óptimo de "optimal_charge_block".
✅ Si NO hay PVP: usar SOLO datos de FV (kW, kWh, % cobertura).
✅ Si NO hay PVP: saving_eur = potencia × horas × 0.12 × (%FV/100).
✅ Si NO hay PVP: NO menciones €/MWh, precio, barato, económico.
✅ Si la ventana NO tiene FV: dilo claramente. "Ventana sin FV. Todo importado."
✅ Si la ventana SÍ tiene FV: dilo. "FV de X kW cubre Y% de la carga."
✅ Cada "reason" debe ser ÚNICO. No repitas estructuras.
✅ Mínimo 2 frases, máximo 4. Prosa técnica directa.

════════════════════════════════════════════════════════════
SIN PVP — REGLAS ESPECÍFICAS
════════════════════════════════════════════════════════════

Cuando NO hay datos PVP (has_pvp=false en el contexto):

1. NO hay precio de mercado. NO digas "100 €/MWh". NO digas "más barata".
2. La única variable es: ¿hay FV en la ventana? ¿Cuánta? ¿Cuándo?
3. Clasificación horaria por FV:
   - LOW    → FV > 50% del pico (aprovechar para cargas pesadas)
   - MID    → FV > 0, ≤ 50% del pico (cargas ligeras)
   - HIGH   → FV = 0 (sin generación, minimizar consumo)
4. Baterías: bloque contiguo con MAYOR FV ACUMULADA. Ya calculado en optimal_charge_block.
5. saving_eur = power_kw × horas × 0.12 €/kWh × (% cobertura FV / 100).
6. Si % cobertura FV = 0: saving_eur = 0.0. saving_tag = "Sin FV en ventana — decisión operativa".
7. Si % cobertura FV > 80: saving_tag = "Autoconsumo ~X kWh → ahorro ~Y €".

════════════════════════════════════════════════════════════
FRASES PROHIBIDAS — USARLAS ES UN ERROR
════════════════════════════════════════════════════════════

NUNCA uses estas frases. Si las usas, estás ignorando los datos:

❌ "La ventana flexible del activo es de Xh a Yh. Se ha elegido esta ventana porque es la más barata dentro de la flexibilidad del activo."
❌ "El ahorro se calcula comparando el precio medio de la ventana flexible con el precio medio del día, lo que supone un ahorro de 0.0 €/día, ya que el precio medio de la ventana flexible es igual al precio medio del día."
❌ "precio medio de esta ventana es de 100.0 €/MWh"
❌ "la más barata dentro de la flexibilidad del activo"
❌ "menor consumo de energía"
❌ "aprovecha la generación fotovoltaica" (si la ventana es de noche)
❌ "ventana económica"
❌ "horario valle"

════════════════════════════════════════════════════════════
CÓMO ESCRIBIR EL CAMPO "reason"
════════════════════════════════════════════════════════════

El campo "reason" debe ser ÚNICO para cada activo. Usa los datos específicos del activo.

Estructuras de apertura PERMITIDAS (usa una diferente por activo):

1. Para baterías con FV: "El bloque óptimo de {N} horas con mayor FV acumulada es {Xh–Yh}, cubriendo el {Z}% de la carga de {W} kW."
2. Para baterías sin FV: "La ventana {Xh–Yh} no incluye horas de FV. El bloque de {N} horas se importa íntegramente de la red."
3. Para cold storage con FV: "Pre-enfriar entre {Xh–Yh} aprovecha {Y} kW pico de FV, acumulando inercia para {Z} horas sin compresor."
4. Para cold storage sin FV: "La ventana nocturna {Xh–Yh} carece de FV. El pre-enfriado acumula inercia térmica para evitar arranques durante el día."
5. Para compressor: "Arrancar a las {Xh} coincide con {Y} kW de FV, absorbiendo el pico de {Z}× corriente nominal en autoconsumo."
6. Para pump: "Llenar el depósito entre {Xh–Yh} acumula {Y} kWh de FV, garantizando {Z} horas de autonomía hidráulica."
7. Para lighting: "Programar encendido en {Xh–Yh} aprovecha {Y} kW de FV disponible. En horas sin FV, reducir al 70% si hay dimmer."
8. Para no flexibles: "El activo de {X} kW opera en horas sin FV, importando {Y} kWh de la red. Representa el {Z}% de la demanda total."
9. Para autoclave: "Iniciar el ciclo de {N} horas a las {Xh} permite completar antes de la caída de FV a las {Yh}."
10. Para mantenimiento: "Programar la revisión entre {Xh–Yh} coincide con {Y} kW de FV, minimizando la importación neta durante el parada."

REGLAS DE REDACCIÓN:
- Mínimo 2 frases, máximo 4.
- Sin bullet points, sin asteriscos.
- Siempre incluye cifras: kW, kWh, horas, %.
- Si NO hay PVP: NO incluyas €/MWh.
- Termina con la consecuencia práctica.

════════════════════════════════════════════════════════════
FORMATO DE SALIDA — JSON ESTRICTO
════════════════════════════════════════════════════════════

{
  "decisions": [
    {
      "asset_id": "string",
      "asset_name": "string",
      "asset_type": "string",
      "priority": int,
      "time_window": "string — formato HHh–HHh",
      "action": "string — verbo en infinitivo, específico, max 80 chars",
      "reason": "string — 2-4 frases, prosa técnica, con cifras reales",
      "saving_tag": "string",
      "saving_eur": float (≥ 0, realista, nunca NaN),
      "urgency": "critical|high|medium|low",
      "flex_window_label": "string — HHh–HHh"
    }
  ]
}

RESTRICCIONES ABSOLUTAS:
• Devuelve SOLO el JSON. Sin markdown, sin texto previo.
• saving_eur: float ≥ 0. Si no hay PVP y no hay FV: 0.0.
• time_window: debe ser EXACTAMENTE el optimal_charge_block para baterías, o las horas de FV para otros activos.
• Nunca omitas un activo flexible.
• El campo "action" debe ser ejecutable: "Programar carga 11h–14h" es bueno; "Optimizar batería" es inútil.
• Cada "reason" debe ser diferente a las demás del mismo informe.
"""



def _build_user_prompt(df_today: pd.DataFrame, df_assets: pd.DataFrame) -> str:
    has_pvp = df_today["has_pvp"].any() if "has_pvp" in df_today.columns else False

    # Detectar PVP real (no dummy 100.0)
    pvp_series = df_today["price_pvpc_eur_mwh"].dropna() if "price_pvpc_eur_mwh" in df_today.columns else pd.Series(dtype=float)
    has_real_pvp = has_pvp and len(pvp_series) > 0 and not (pvp_series == 100.0).all()

    # Datos FV globales
    pv_peak_kw = float(df_today["pv_power_gen_kw"].max()) if "pv_power_gen_kw" in df_today.columns else 0.0
    pv_total_kwh = float(df_today["pv_power_gen_kw"].sum()) if "pv_power_gen_kw" in df_today.columns else 0.0
    pv_peak_h = int(df_today.loc[df_today["pv_power_gen_kw"].idxmax(), "hour"]) if pv_peak_kw > 0 and "pv_power_gen_kw" in df_today.columns else None
    avg_consumption_kw = float(df_today["power_consumption_kw"].dropna().mean()) if "power_consumption_kw" in df_today.columns else 0.0

    # Horas donde FV cubre o supera consumo
    hourly_data = []
    fv_cover_hours = []
    total_excess_fv = 0.0
    for _, row in df_today.sort_values("hour").iterrows():
        h = int(row["hour"])
        pv = float(row.get("pv_power_gen_kw", 0))
        cons = float(row.get("power_consumption_kw", 0)) if pd.notna(row.get("power_consumption_kw")) else None
        pvp = float(row.get("price_pvpc_eur_mwh", 0)) if pd.notna(row.get("price_pvpc_eur_mwh")) else None

        net_import = max(0.0, cons - pv) if cons is not None else None
        fv_covers = pv >= cons if cons is not None else False
        excess = max(0.0, pv - cons) if fv_covers else 0.0
        total_excess_fv += excess
        if fv_covers:
            fv_cover_hours.append(h)

        hourly_data.append({
            "hour": h,
            "pv_kw": round(pv, 1),
            "consumption_kw": round(cons, 1) if cons else None,
            "net_import_kw": round(net_import, 1) if net_import is not None else None,
            "fv_covers": fv_covers,
            "excess_fv_kw": round(excess, 1),
            "pvp_eur_mwh": round(pvp, 2) if pvp and has_real_pvp else None
        })

    # Clasificación por FV (sin PVP)
    if not has_real_pvp:
        low_hours = sorted([h["hour"] for h in hourly_data if h["pv_kw"] > pv_peak_kw * 0.5]) if pv_peak_kw > 0 else []
        mid_hours = sorted([h["hour"] for h in hourly_data if 0 < h["pv_kw"] <= pv_peak_kw * 0.5]) if pv_peak_kw > 0 else []
        high_hours = sorted([h["hour"] for h in hourly_data if h["pv_kw"] == 0])
        solar_hours = sorted([h["hour"] for h in hourly_data if h["pv_kw"] > 1.0])
    else:
        low_hours = sorted(df_today[df_today["pvp_class"] == "low"]["hour"].tolist()) if "pvp_class" in df_today.columns else []
        high_hours = sorted(df_today[df_today["pvp_class"] == "high"]["hour"].tolist()) if "pvp_class" in df_today.columns else []
        solar_hours = sorted(df_today[df_today["pvp_class"] == "solar"]["hour"].tolist()) if "pvp_class" in df_today.columns else []
        mid_hours = sorted(df_today[df_today["pvp_class"] == "mid"]["hour"].tolist()) if "pvp_class" in df_today.columns else []

    # ────────────────────────────────────────────────────────────
    # RAZONAMIENTO PRECARGADO POR ACTIVO
    # La IA no inventa, solo completa el JSON con los datos ya calculados
    # ────────────────────────────────────────────────────────────
    asset_decisions = []

    for _, asset in df_assets.iterrows():
        asset_id = asset["asset_id"]
        asset_name = asset["asset_name"]
        asset_type = asset["asset_type"]
        power_kw = _safe_float(asset["power_kw"], 0.0)
        cap_kwh = _safe_float(asset.get("capacity_kwh"), 0.0)
        ws = int(_safe_float(asset["flex_window_start"], 0))
        we = int(_safe_float(asset["flex_window_end"], 23))
        is_flex = bool(asset["is_flexible"] == 1)
        notes = asset.get("notes", "")
        priority = int(_safe_float(asset["priority"], 99))

        window_hours = _all_hours_in_window(ws, we)
        window_df = df_today[df_today["hour"].isin(window_hours)]

        # Datos FV de la ventana
        window_pv = window_df["pv_power_gen_kw"] if "pv_power_gen_kw" in window_df.columns else pd.Series([0.0]*len(window_df))
        window_pv_max = float(window_pv.max()) if not window_pv.empty else 0.0
        window_pv_total = float(window_pv.sum()) if not window_pv.empty else 0.0
        window_pv_peak_h = int(window_pv.idxmax()) if not window_pv.empty and window_pv.max() > 0 else None

        # Horas con/sin FV en ventana
        solar_in_window = sorted(window_df[window_df["pv_power_gen_kw"] > 1.0]["hour"].tolist()) if "pv_power_gen_kw" in window_df.columns else []
        no_fv_in_window = sorted(window_df[window_df["pv_power_gen_kw"] == 0]["hour"].tolist()) if "pv_power_gen_kw" in window_df.columns else []
        high_fv_in_window = sorted(window_df[window_df["pv_power_gen_kw"] > pv_peak_kw * 0.5]["hour"].tolist()) if pv_peak_kw > 0 and "pv_power_gen_kw" in window_df.columns else []

        # Cálculo del bloque óptimo
        hours_needed = None
        if asset_type in ["forklift_battery", "ev_charging_station"] and cap_kwh > 0 and power_kw > 0:
            hours_needed = math.ceil(cap_kwh / power_kw)

        optimal_block = None
        optimal_block_fv_total = 0.0
        optimal_block_fv_pct = 0.0

        if hours_needed and hours_needed > 0 and len(window_hours) >= hours_needed:
            if has_real_pvp:
                # Bloque con menor precio medio
                best_avg = float('inf')
                for i in range(len(window_hours) - hours_needed + 1):
                    block = window_hours[i:i + hours_needed]
                    block_pvp = df_today[df_today["hour"].isin(block)]["price_pvpc_eur_mwh"].dropna()
                    if not block_pvp.empty:
                        block_avg = float(block_pvp.mean())
                        if block_avg < best_avg:
                            best_avg = block_avg
                            optimal_block = block
            else:
                # Bloque con mayor FV acumulada
                best_fv = float('-inf')
                for i in range(len(window_hours) - hours_needed + 1):
                    block = window_hours[i:i + hours_needed]
                    block_fv = df_today[df_today["hour"].isin(block)]["pv_power_gen_kw"].sum()
                    if block_fv > best_fv:
                        best_fv = block_fv
                        optimal_block = block
                if optimal_block:
                    optimal_block_fv_total = round(best_fv, 1)
                    demand_total = power_kw * hours_needed
                    optimal_block_fv_pct = round(min(100.0, best_fv / demand_total * 100), 1) if demand_total > 0 else 0.0

        # ─── Determinar time_window, action, reason, saving ───

        if asset_type == "forklift_battery" and is_flex:
            if optimal_block:
                tw = f"{optimal_block[0]:02d}h–{optimal_block[-1]+1:02d}h"
                action = f"Programar carga {tw}"
                if not has_real_pvp:
                    if optimal_block_fv_pct >= 80:
                        reason = f"El bloque óptimo de {hours_needed} horas con mayor FV acumulada es {tw}. La FV cubre el {optimal_block_fv_pct}% de la carga de {power_kw} kW, permitiendo autoconsumo casi total. Cargar fuera de este bloque importaría toda la energía de la red."
                        saving = round(power_kw * hours_needed * 0.12 * (optimal_block_fv_pct / 100) / 1000, 2)
                        tag = f"Autoconsumo ~{optimal_block_fv_total:.1f} kWh → ahorro ~{saving:.2f} €"
                    elif optimal_block_fv_pct > 0:
                        reason = f"El bloque óptimo {tw} acumula {optimal_block_fv_total:.1f} kWh de FV, cubriendo el {optimal_block_fv_pct}% de la carga de {power_kw} kW. El resto se importa de la red. Cargar en horas sin FV sería 100% importado."
                        saving = round(power_kw * hours_needed * 0.12 * (optimal_block_fv_pct / 100) / 1000, 2)
                        tag = f"Autoconsumo parcial ~{optimal_block_fv_total:.1f} kWh → ahorro ~{saving:.2f} €"
                    else:
                        reason = f"La ventana {ws:02d}h–{we:02d}h no incluye generación FV. El bloque óptimo {tw} se importa íntegramente de la red. Se programa por necesidad operativa, no por ahorro."
                        saving = 0.0
                        tag = "Sin FV en ventana — decisión operativa"
                else:
                    # Con PVP real
                    window_pvp = window_df["price_pvpc_eur_mwh"].dropna()
                    window_avg = float(window_pvp.mean()) if not window_pvp.empty else 0.0
                    block_pvp = df_today[df_today["hour"].isin(optimal_block)]["price_pvpc_eur_mwh"].dropna()
                    block_avg = float(block_pvp.mean()) if not block_pvp.empty else window_avg
                    saving = round(power_kw * hours_needed * (window_avg - block_avg) / 1000, 2) if window_avg > block_avg else 0.0
                    reason = f"El bloque óptimo {tw} tiene precio medio {block_avg:.1f} €/MWh, {window_avg - block_avg:.1f} €/MWh por debajo de la media de la ventana. Cargar la batería de {power_kw} kW durante {hours_needed} horas ahorra {saving:.2f} € vs el peor momento."
                    tag = f"Evitas ~{saving:.2f} € vs cargar en pico"
                urgency = "critical"
            else:
                tw = f"{ws:02d}h–{we:02d}h"
                action = f"Programar carga en ventana {tw}"
                reason = f"No se ha podido determinar un bloque óptimo contiguo de {hours_needed} horas dentro de la ventana {tw}. Se recomienda revisar la ventana flexible del activo."
                saving = 0.0
                tag = "Revisar ventana — bloque no encaja"
                urgency = "critical"

        elif asset_type == "cold_storage" and is_flex:
            if solar_in_window and not has_real_pvp:
                # Hay FV en la ventana → pre-enfriar en horas de FV
                best_fv_h = solar_in_window[-1] if solar_in_window else ws
                tw = f"{ws:02d}h–{best_fv_h+1:02d}h" if best_fv_h != ws else f"{ws:02d}h–{we:02d}h"
                action = f"Pre-enfriar 1-2°C {tw}"
                reason = f"La ventana incluye {len(solar_in_window)} horas de FV, con pico de {window_pv_max:.1f} kW a las {window_pv_peak_h:02d}h. Pre-enfriar durante estas horas acumula inercia térmica para {len(no_fv_in_window)} horas sin FV, evitando arranques del compresor cuando el consumo total es máximo."
                saving = round(power_kw * 3 * 0.12 * (window_pv_total / (power_kw * len(window_hours)) if power_kw > 0 else 0) / 1000, 2)
                tag = f"Reducción importación ~{saving:.2f} €"
                urgency = "high"
            else:
                # Sin FV en ventana → pre-enfriar nocturno para inercia diurna
                tw = f"{ws:02d}h–{we:02d}h"
                action = f"Pre-enfriar 1-2°C {tw}"
                reason = f"La ventana {tw} no incluye generación FV. El pre-enfriado nocturno acumula inercia térmica para 2-3 horas sin compresor activo. Durante el día, la FV pico de {pv_peak_kw:.1f} kW cubre el consumo y la inercia evita arranques adicionales."
                saving = 0.0
                tag = "Sin FV en ventana — inercia térmica"
                urgency = "high"

        elif asset_type == "compressor" and is_flex:
            if solar_in_window and not has_real_pvp:
                best_h = window_pv_peak_h if window_pv_peak_h and window_pv_peak_h in window_hours else solar_in_window[0]
                tw = f"{best_h:02d}h–{best_h+2:02d}h"
                action = f"Arrancar compresor {tw}"
                reason = f"Arrancar a las {best_h:02d}h coincide con {window_pv_max:.1f} kW de FV pico en la ventana, absorbiendo el pico de arranque de {power_kw * 6:.1f} kW (6× nominal) en autoconsumo. Operar fuera de esta franja importaría todo el pico de la red."
                saving = round(power_kw * 2 * 0.12 * min(1.0, window_pv_max / power_kw if power_kw > 0 else 0) / 1000, 2)
                tag = f"Evita pico importación ~{saving:.2f} €"
                urgency = "medium"
            else:
                tw = f"{ws:02d}h–{we:02d}h"
                action = f"Operar compresor {tw}"
                reason = f"La ventana {tw} no incluye FV. El arranque del compresor de {power_kw} kW se importa íntegramente de la red. Programar mantenimiento (purga, filtros) en esta franja para concentrar el consumo en horas controladas."
                saving = 0.0
                tag = "Sin FV — decisión operativa"
                urgency = "medium"

        elif asset_type == "pump" and is_flex:
            if solar_in_window and not has_real_pvp:
                best_h = solar_in_window[0] if solar_in_window else ws
                tw = f"{best_h:02d}h–{best_h+3:02d}h"
                action = f"Llenar depósito {tw}"
                autonomia = math.ceil(cap_kwh / power_kw) if cap_kwh > 0 and power_kw > 0 else 4
                reason = f"Llenar entre {tw} aprovecha {window_pv_max:.1f} kW de FV disponible. El depósito acumula {autonomia} horas de autonomía hidráulica, evitando bombear en horas sin FV cuando todo se importaría."
                saving = round(power_kw * 3 * 0.12 * min(1.0, window_pv_total / (power_kw * 3) if power_kw > 0 else 0) / 1000, 2)
                tag = f"Autoconsumo ~{saving:.2f} €"
                urgency = "medium"
            else:
                tw = f"{ws:02d}h–{we:02d}h"
                action = f"Llenar depósito {tw}"
                reason = f"La ventana {tw} no incluye FV. El llenado del depósito se importa de la red. Se programa por necesidad operativa; el depósito actúa como batería hidráulica para horas sin bombeo."
                saving = 0.0
                tag = "Sin FV — decisión operativa"
                urgency = "medium"

        elif asset_type == "lighting" and is_flex:
            if solar_in_window and not has_real_pvp:
                tw = f"{solar_in_window[0]:02d}h–{solar_in_window[-1]+1:02d}h"
                action = f"Programar encendido {tw}"
                reason = f"La ventana incluye {len(solar_in_window)} horas de FV activa. Encender durante estas horas aprovecha la generación solar. En horas sin FV, reducir al 70% si el sistema tiene dimmer."
                saving = round(power_kw * len(solar_in_window) * 0.12 * 0.5 / 1000, 2)
                tag = f"Ahorro potencial ~{saving:.2f} €"
                urgency = "low"
            else:
                tw = f"{ws:02d}h–{we:02d}h"
                action = f"Apagar 30% zonas no productivas {tw}"
                reason = f"La ventana {tw} no incluye FV. Apagar 30% de zonas no productivas reduce la importación neta. Solo aplicar si el ahorro supera 0.05 €."
                saving = round(power_kw * 0.3 * len(window_hours) * 0.12 / 1000, 2)
                tag = f"Reducción importación ~{saving:.2f} €"
                urgency = "low"

        elif not is_flex:
            tw = f"{ws:02d}h–{we:02d}h"
            action = f"Monitorizar consumo {tw}"
            pct_total = round(power_kw / (avg_consumption_kw if avg_consumption_kw > 0 else 1) * 100, 1)
            if not has_real_pvp:
                reason = f"Activo no flexible de {power_kw} kW opera en horario fijo. En horas sin FV, importa {power_kw} kW de la red. Representa el {pct_total}% del consumo total de la planta."
                saving = 0.0
                tag = "Alerta — sin ahorro directo"
            else:
                window_pvp = window_df["price_pvpc_eur_mwh"].dropna()
                avg_pvp = float(window_pvp.mean()) if not window_pvp.empty else 0.0
                coste = round(power_kw * len(window_hours) * avg_pvp / 1000, 2)
                reason = f"Activo no flexible de {power_kw} kW. Coste estimado en la ventana: {coste:.2f} €. Representa el {pct_total}% de la demanda total."
                saving = 0.0
                tag = f"Alerta pico — coste ~{coste:.2f} €"
            urgency = "low"

        else:
            # Otros activos flexibles
            tw = f"{ws:02d}h–{we:02d}h"
            action = f"Optimizar horario {tw}"
            if solar_in_window and not has_real_pvp:
                reason = f"La ventana incluye {len(solar_in_window)} horas de FV. Programar operación durante estas horas para maximizar autoconsumo."
                saving = round(power_kw * len(solar_in_window) * 0.12 * 0.3 / 1000, 2)
                tag = f"Autoconsumo ~{saving:.2f} €"
            else:
                reason = f"La ventana {tw} no incluye FV. La operación se importa de la red. Programar por necesidad operativa."
                saving = 0.0
                tag = "Sin FV — decisión operativa"
            urgency = "medium"

        asset_decisions.append({
            "asset_id": asset_id,
            "asset_name": asset_name,
            "asset_type": asset_type,
            "priority": priority,
            "time_window": tw,
            "action": action,
            "reason": reason,
            "saving_tag": tag,
            "saving_eur": saving,
            "urgency": urgency,
            "flex_window_label": f"{ws:02d}h–{we:02d}h"
        })

    # ────────────────────────────────────────────────────────────
    # CONSTRUIR EL PROMPT FINAL
    # ────────────────────────────────────────────────────────────

    target_date_str = str(df_today["date"].iloc[0]) if "date" in df_today.columns else "desconocida"

    decisions_json = json.dumps(asset_decisions, indent=2, ensure_ascii=False)
    hourly_json = json.dumps(hourly_data, indent=2, ensure_ascii=False)

    lines = [
        "## CONTEXTO DEL DÍA",
        "",
        f"Fecha: {target_date_str}",
        f"PVP disponible: {'SÍ — datos reales' if has_real_pvp else 'NO — sin datos de mercado'}",
        f"FV pico: {pv_peak_kw:.1f} kW a las {pv_peak_h:02d}h" if pv_peak_h else "FV pico: 0 kW",
        f"FV total: {pv_total_kwh:.1f} kWh",
        f"Consumo basal medio: {avg_consumption_kw:.1f} kW",
        f"Horas donde FV cubre consumo: {fv_cover_hours}",
        f"Exceso FV acumulado: {total_excess_fv:.1f} kWh",
        "",
        "## DATOS HORA A HORA",
        "```json",
        hourly_json,
        "```",
        "",
        "## DECISIONES YA CALCULADAS POR ACTIVO",
        "El siguiente JSON contiene el razonamiento completo para cada activo.",
        "TU TAREA: devolver EXACTAMENTE estos datos en el formato JSON requerido.",
        "NO modifiques los time_window, NO inventes precios, NO cambies las razones.",
        "Solo asegúrate de que el formato sea válido JSON con la lista 'decisions'.",
        "",
        "```json",
        decisions_json,
        "```",
        "",
        "## FORMATO DE SALIDA REQUERIDO",
        "",
        "Devuelve SOLO un JSON con esta estructura:",
        "",
        "{",
        '  "decisions": [',
        "    {",
        '      "asset_id": "string",',
        '      "asset_name": "string",',
        '      "asset_type": "string",',
        '      "priority": int,',
        '      "time_window": "HHh–HHh",',
        '      "action": "string",',
        '      "reason": "string",',
        '      "saving_tag": "string",',
        '      "saving_eur": float,',
        '      "urgency": "critical|high|medium|low",',
        '      "flex_window_label": "HHh–HHh"',
        "    }",
        "  ]",
        "}",
        "",
        "REGLAS:",
        "• Usa EXACTAMENTE los time_window, action, reason, saving_tag, saving_eur, urgency del JSON anterior.",
        "• NO añadas texto explicativo fuera del JSON.",
        "• NO modifiques las razones. Ya están calculadas correctamente.",
        "• Si has_pvp=false en el contexto: NO uses €/MWh en ningún campo.",
    ]

    return "\n".join(lines)


def _call_llm(system_prompt: str, user_prompt: str) -> dict:
    logger.info("[AGENTE] openai version: %s", openai.__version__)
    logger.info("[AGENTE] httpx version: %s", httpx.__version__)
    for k, v in os.environ.items():
        if "proxy" in k.lower():
            logger.info("[AGENTE] proxy env: %s=%s", k, v)
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