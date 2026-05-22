"""
report_generator.py
────────────────────
Genera el informe diario de estrategia energética en HTML (y opcionalmente PDF).

Entrada:
    · Dict estructurado de gold_fact_energy_decisions.build_energy_decisions()

Salida:
    · HTML  → data_storage/reports/YYYY-MM-DD/{client_id}_energy_report.html

Autoejectable: genera informe para CLT-0001 si se lanza directamente.
    python report_generator.py              → solo HTML
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from gold_fact_energy_decisions import build_energy_decisions
from logger_config import setup_logging

load_dotenv()
logger = setup_logging()

# ── RUTAS DE SALIDA ───────────────────────────────────────────────────────────
REPORTS_DIR = Path("data_storage") / "reports"

# ── CONFIG S3 (solo aplica si ENVIRONMENT != DEV) ─────────────────────────────
ENVIRONMENT = os.getenv("ENVIRONMENT", "DEV").upper()
S3_BUCKET   = os.getenv("S3_BUCKET")
S3_PREFIX   = os.getenv("S3_PREFIX", "reports")
AWS_REGION  = os.getenv("AWS_REGION", "eu-south-2")

# ── ICONOS POR TIPO DE ACTIVO ─────────────────────────────────────────────────
ASSET_ICONS = {
    "forklift_battery": "🔋",
    "cold_storage":     "❄️",
    "compressor":       "💨",
    "pump":             "💧",
    "autoclave":        "🏭",
    "lighting":         "💡",
    "other":            "⚙️",
}

URGENCY_COLOR = {
    "critical": ("#1a4a2e", "#3fb950", "#d4f7e0"),
    "high":     ("#0d2d4a", "#58a6ff", "#d0e8ff"),
    "medium":   ("#4a3800", "#d29922", "#fef3cd"),
    "low":      ("#2c2c2c", "#8b949e", "#f0f0f0"),
}

URGENCY_LABEL = {
    "critical": "PRIORITARIO",
    "high":     "IMPORTANTE",
    "medium":   "PROGRAMAR",
    "low":      "OPCIONAL",
}

# Mapa completo OpenWeatherMap
WX_ICON = {
    # Tormenta (2xx)
    200: "⛈️", 201: "⛈️", 202: "⛈️", 210: "⛈️", 211: "⛈️",
    212: "⛈️", 221: "⛈️", 230: "⛈️", 231: "⛈️", 232: "⛈️",
    # Llovizna (3xx)
    300: "🌦️", 301: "🌦️", 302: "🌦", 310: "🌦️", 311: "🌦",
    312: "🌦", 313: "🌦️", 314: "🌦", 321: "🌦️",
    # Lluvia (5xx)
    500: "🌦️", 501: "🌨", 502: "🌨", 503: "🌨", 504: "🌨",
    511: "🌨", 520: "🌦️", 521: "🌧", 522: "🌧", 531: "🌧",
    # Nieve (6xx)
    600: "❄️", 601: "❄️", 602: "❄️", 611: "❄️", 612: "❄️",
    613: "❄️", 615: "❄️", 616: "❄️", 620: "❄️", 621: "❄️", 622: "❄️",
    # Atmósfera (7xx)
    701: "🌫", 711: "🌫", 721: "🌫", 731: "🌫", 741: "🌫",
    751: "🌫", 761: "🌫", 762: "🌫", 771: "🌫", 781: "🌪",
    # Despejado / nubes (8xx)
    800: "☀️", 801: "🌤️", 802: "⛅", 803: "☁️", 804: "☁️",
}


# ── HELPERS HTML ──────────────────────────────────────────────────────────────

def _wx_icon(weather_id: int | None) -> str:
    """Devuelve emoji para el weather_id de OpenWeatherMap.
    Busca primero exacto, luego por centena, luego fallback."""
    if weather_id is None:
        return "🌡"
    icon = WX_ICON.get(int(weather_id))
    if icon:
        return icon
    # fallback por centena
    base = (int(weather_id) // 100) * 100
    return WX_ICON.get(base, "🌡")


def _pvp_bar_color(pvp: float | None) -> str:
    if pvp is None:
        return "#8b949e"
    if pvp < 80:
        return "#3fb950"
    if pvp < 150:
        return "#d29922"
    return "#f85149"


def _reliability_color(r: str) -> str:
    return {"alta": "#3fb950", "media": "#d29922", "baja": "#f85149"}.get(r, "#8b949e")


def _fmt_date_es(date_str: str) -> str:
    days   = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
    months = ["ene", "feb", "mar", "abr", "may", "jun",
              "jul", "ago", "sep", "oct", "nov", "dic"]
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{days[d.weekday()]} {d.day} {months[d.month - 1]} {d.year}"


# ── SECCIONES HTML ────────────────────────────────────────────────────────────

def _render_header(data: dict) -> str:
    client    = data["client"]
    today     = data["today"]
    kpis      = today["kpis"]
    gen_at    = data["generated_at"]
    date_es   = _fmt_date_es(today["date"])
    rel_color = _reliability_color(kpis["forecast_reliability"])

    return f"""
    <div class="header">
      <div class="header-left">
        <div class="badge"><span class="dot"></span>INFORME ACTIVO</div>
        <h1>Estrategia Energética · Plan de Acción</h1>
        <p class="subtitle">{client['name']} &nbsp;·&nbsp; "Sostenidos por cinta aislante, impulsados por la fe. Mientras salga zumo, nadie toca nada."</p>
      </div>
      <div class="header-right">
        <div class="client-id">{client['client_id']}</div>
        <div class="report-date">{date_es}</div>
        <div class="generated">Generado: {gen_at}</div>
        <div class="reliability" style="color:{rel_color}">
          ● Fiabilidad: <strong>{kpis['forecast_reliability'].upper()}</strong>
          {'· PVP confirmado OMIE' if kpis['has_pvp'] else '· Sin PVP (D+1 pendiente)'}
        </div>
      </div>
    </div>"""


def _render_kpis(kpis: dict) -> str:
    pvp_min_str = (f"{kpis['pvp_min']:.0f} €/MWh · {kpis['pvp_min_hour']:02d}h"
                   if kpis["pvp_min"] is not None else "—")
    pvp_max_str = (f"{kpis['pvp_max']:.0f} €/MWh · {kpis['pvp_max_hour']:02d}h"
                   if kpis["pvp_max"] is not None else "—")

    cards = [
        ("FV pico mañana",     f"{kpis['pv_peak_kw']} kW",   f"{kpis['pv_peak_hour']:02d}h hora local", "#58a6ff"),
        ("PVP mínimo",         pvp_min_str,                    "Ventana económica",                       "#3fb950"),
        ("PVP máximo",         pvp_max_str,                    "Evitar consumo",                          "#f85149"),
        ("Horas solar activa", f"{kpis['hours_solar']}h",      "FV > 1 kW",                               "#58a6ff"),
        ("Horas precio bajo",  f"{kpis['hours_cheap']}h",      "< 80 €/MWh",                              "#3fb950"),
        ("Horas precio alto",  f"{kpis['hours_expensive']}h",  "> 150 €/MWh",                             "#f85149"),
    ]

    items = ""
    for label, value, sub, color in cards:
        items += f"""
        <div class="kpi-card">
          <div class="kpi-label">{label}</div>
          <div class="kpi-value" style="color:{color}">{value}</div>
          <div class="kpi-sub">{sub}</div>
        </div>"""

    return f'<div class="kpi-strip">{items}</div>'


def _render_charts(today: dict) -> str:
    pvp_hours = today["pvp_hours"]
    pv_hours  = today["pv_hours"]

    # Asegurar cobertura completa 00–23h; rellenar huecos con null
    pvp_map = {r["hour"]: r for r in pvp_hours}
    pv_map  = {r["hour"]: r for r in pv_hours}

    labels     = json.dumps([f"{h:02d}h" for h in range(24)])
    pv_data    = json.dumps([round(pv_map[h]["pv_power_gen_kw"], 2) if h in pv_map else 0
                             for h in range(24)])
    pvp_data   = json.dumps([round(pvp_map[h]["price_pvpc_eur_mwh"], 1)
                              if h in pvp_map and pvp_map[h]["price_pvpc_eur_mwh"] is not None
                              else None
                              for h in range(24)])
    pvp_colors = json.dumps([_pvp_bar_color(pvp_map[h]["price_pvpc_eur_mwh"]
                              if h in pvp_map else None)
                             for h in range(24)])

    return f"""
    <div class="section">
      <div class="section-title">Precio de mercado (PVP) · Generación fotovoltaica</div>
      <div class="chart-row">
        <div class="chart-box">
          <div class="chart-label">PVP €/MWh — 00h a 23h hora local</div>
          <div style="position:relative;height:180px;">
            <canvas id="chartPVP"></canvas>
          </div>
        </div>
        <div class="chart-box">
          <div class="chart-label">Generación FV prevista (kW) — 00h a 23h hora local</div>
          <div style="position:relative;height:180px;">
            <canvas id="chartPV"></canvas>
          </div>
        </div>
      </div>
    </div>
    <script>
    (function(){{
      var labels    = {labels};
      var pvpData   = {pvp_data};
      var pvpColors = {pvp_colors};
      var pvData    = {pv_data};

      new Chart(document.getElementById('chartPVP'), {{
        type: 'bar',
        data: {{ labels, datasets: [{{ data: pvpData, backgroundColor: pvpColors,
                                       borderRadius: 3, borderSkipped: false }}] }},
        options: {{
          responsive: true, maintainAspectRatio: false,
          plugins: {{ legend: {{ display: false }},
            tooltip: {{ callbacks: {{ label: ctx => ctx.parsed.y != null
              ? ctx.parsed.y.toFixed(0) + ' €/MWh' : 'Sin dato' }} }} }},
          scales: {{
            x: {{ ticks: {{ color:'#8b949e', font:{{ size:9 }}, autoSkip:false, maxRotation:0 }},
                  grid: {{ color:'rgba(255,255,255,0.05)' }} }},
            y: {{ ticks: {{ color:'#8b949e', font:{{ size:9 }}, callback: v => v+'€' }},
                  grid: {{ color:'rgba(255,255,255,0.05)' }} }}
          }}
        }}
      }});

      new Chart(document.getElementById('chartPV'), {{
        type: 'line',
        data: {{ labels, datasets: [{{ data: pvData, borderColor:'#58a6ff',
          backgroundColor:'rgba(88,166,255,0.15)', fill:true, tension:0.4,
          pointRadius:3, pointBackgroundColor:'#58a6ff' }}] }},
        options: {{
          responsive: true, maintainAspectRatio: false,
          plugins: {{ legend: {{ display: false }},
            tooltip: {{ callbacks: {{ label: ctx => ctx.parsed.y.toFixed(1) + ' kW' }} }} }},
          scales: {{
            x: {{ ticks: {{ color:'#8b949e', font:{{ size:9 }}, autoSkip:false, maxRotation:0 }},
                  grid: {{ color:'rgba(255,255,255,0.05)' }} }},
            y: {{ ticks: {{ color:'#8b949e', font:{{ size:9 }}, callback: v => v+'kW' }},
                  grid: {{ color:'rgba(255,255,255,0.05)' }} }}
          }}
        }}
      }});
    }})();
    </script>"""


def _render_decisions(decisions: list[dict]) -> str:
    if not decisions:
        return "<p style='color:#8b949e;padding:16px'>Sin decisiones generadas.</p>"

    cards = ""
    for d in decisions:
        dark, accent, light = URGENCY_COLOR.get(d["urgency"], URGENCY_COLOR["low"])
        icon  = ASSET_ICONS.get(d["asset_type"], "⚙️")
        label = URGENCY_LABEL.get(d["urgency"], d["urgency"].upper())

        cards += f"""
        <div class="decision-card" style="border-left:3px solid {accent}">
          <div class="decision-icon" style="background:{light};font-size:22px">{icon}</div>
          <div class="decision-body">
            <div class="decision-meta">
              <span class="decision-time">{d['time_window']}</span>
              <span class="decision-tag" style="background:{light};color:{accent}">{label}</span>
            </div>
            <div class="decision-title">{d['action']} — {d['asset_name']}</div>
            <div class="decision-reason">{d['reason']}</div>
          </div>
          <div class="decision-saving" style="color:{accent}">{d['saving_tag']}</div>
        </div>"""

    return f'<div class="decisions-grid">{cards}</div>'


def _render_outlook(outlook: dict) -> str:
    days    = outlook.get("days", [])
    summary = outlook.get("summary_text", "")

    months    = ["ene","feb","mar","abr","may","jun",
                 "jul","ago","sep","oct","nov","dic"]
    day_names = ["Lun","Mar","Mié","Jue","Vie","Sáb","Dom"]

    day_cards = ""
    for d in days:
        dt      = datetime.strptime(d["date"], "%Y-%m-%d")
        label   = f"{day_names[dt.weekday()]} {dt.day} {months[dt.month-1]}"
        pv_w    = max(5, min(100, int(d["pv_peak_kw"] / 15 * 100)))
        rel_c   = _reliability_color(d["reliability"])
        wx      = _wx_icon(d.get("weather_id"))       

        day_cards += f"""
        <div class="outlook-day">
          <div class="outlook-label">{label}</div>
          <div class="outlook-icon">{wx}</div>
          <div class="outlook-pv" style="color:#58a6ff">FV ~{d['pv_peak_kw']} kW</div>
          <div class="outlook-temp">{d['temp_min']:.0f}°–{d['temp_max']:.0f}°C
            · {d['clouds_pct']:.0f}% nub.</div>
          <div class="mini-bar-wrap">
            <div class="mini-bar" style="width:{pv_w}%;background:#58a6ff"></div>
          </div>
          
        </div>"""

    return f"""
    <div class="section">
      <div class="section-title">Outlook 5 días · Orientativo</div>
      <div class="outlook-summary">{summary}</div>
      <div class="outlook-grid">{day_cards}</div>
      <div class="outlook-warning">
        ⚠ Previsión climática orientativa — Sujeto a variabilidad atmosférica.
      </div>
    </div>"""


def _render_footer(client_id: str, generated_at: str) -> str:
    return f"""
    <div class="footer">
      <span>Sistema de gestión energética · ETL SunSaver · gold.fact_energy_forecast · {client_id}</span>
      <span class="footer-logo">ENERGY·OS v2.1</span>
    </div>"""


# ── CSS ───────────────────────────────────────────────────────────────────────

CSS = """
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600;700&display=swap');
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background:#0d1117; color:#e6edf3;
       font-family:'IBM Plex Sans','Segoe UI',sans-serif; font-size:14px; line-height:1.5; }
.header { background:#161b22; border-bottom:1px solid rgba(255,255,255,0.1);
          padding:20px 28px 16px; display:flex; justify-content:space-between;
          align-items:flex-start; flex-wrap:wrap; gap:12px; }
.badge  { display:inline-flex; align-items:center; gap:6px;
          background:rgba(63,185,80,0.15); border:1px solid #3fb950;
          color:#3fb950; font-size:10px; font-weight:700; letter-spacing:1.5px;
          padding:3px 10px; border-radius:3px; margin-bottom:8px; }
.dot    { width:6px; height:6px; border-radius:50%; background:#3fb950;
          animation:pulse 2s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
h1 { font-size:22px; font-weight:700; }
.subtitle  { font-size:13px; color:#8b949e; margin-top:4px; }
.header-right { text-align:right; }
.client-id   { font-size:13px; font-weight:700; color:#58a6ff;
               font-family:'IBM Plex Mono',monospace; }
.report-date { font-size:12px; color:#8b949e; margin-top:2px;
               font-family:'IBM Plex Mono',monospace; }
.generated   { font-size:10px; color:#8b949e; margin-top:4px; }
.reliability { font-size:11px; margin-top:6px; }
.kpi-strip   { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
               gap:1px; background:rgba(255,255,255,0.08);
               border-bottom:1px solid rgba(255,255,255,0.08); }
.kpi-card  { background:#161b22; padding:14px 16px; }
.kpi-label { font-size:10px; color:#8b949e; text-transform:uppercase;
             letter-spacing:1px; font-weight:600; }
.kpi-value { font-size:18px; font-weight:700;
             font-family:'IBM Plex Mono',monospace; margin-top:4px; }
.kpi-sub   { font-size:11px; color:#8b949e; margin-top:2px; }
.section   { padding:20px 28px; border-bottom:1px solid rgba(255,255,255,0.08); }
.section-title { font-size:10px; font-weight:700; letter-spacing:1.5px;
                 text-transform:uppercase; color:#8b949e; margin-bottom:16px;
                 display:flex; align-items:center; gap:8px; }
.section-title::after { content:''; flex:1; height:1px;
                        background:rgba(255,255,255,0.08); }
.chart-row { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
.chart-box { background:#161b22; border:1px solid rgba(255,255,255,0.08);
             border-radius:6px; padding:16px; }
.chart-label { font-size:11px; font-weight:600; color:#8b949e; margin-bottom:12px; }
.decisions-grid { display:flex; flex-direction:column; gap:10px; }
.decision-card  { background:#161b22; border:1px solid rgba(255,255,255,0.08);
                  border-radius:6px; padding:14px 16px;
                  display:grid; grid-template-columns:48px 1fr auto;
                  align-items:start; gap:14px; }
.decision-icon  { width:44px; height:44px; border-radius:8px;
                  display:flex; align-items:center; justify-content:center; }
.decision-meta  { display:flex; align-items:center; gap:10px; margin-bottom:4px; }
.decision-time  { font-family:'IBM Plex Mono',monospace; font-size:12px;
                  font-weight:700; color:#58a6ff; }
.decision-tag   { font-size:9px; font-weight:700; letter-spacing:1px;
                  padding:2px 8px; border-radius:3px; text-transform:uppercase; }
.decision-title  { font-size:13px; font-weight:700; margin-bottom:5px; }
.decision-reason { font-size:12px; color:#8b949e; line-height:1.55; }
.decision-saving { font-size:11px; font-weight:700; text-align:right;
                   white-space:nowrap; font-family:'IBM Plex Mono',monospace; }
.outlook-summary { font-size:12px; color:#8b949e; line-height:1.6;
                   background:#161b22; border:1px solid rgba(255,255,255,0.08);
                   border-radius:6px; padding:12px 16px; margin-bottom:14px; }
.outlook-grid  { display:grid; grid-template-columns:repeat(5,1fr); gap:8px; }
.outlook-day   { background:#161b22; border:1px solid rgba(255,255,255,0.08);
                 border-radius:6px; padding:12px 10px; text-align:center; }
.outlook-label { font-size:10px; color:#8b949e; text-transform:uppercase; }
.outlook-icon  { font-size:22px; margin:6px 0; }
.outlook-pv    { font-size:11px; font-weight:600; }
.outlook-temp  { font-size:10px; color:#8b949e; margin-top:4px; }
.mini-bar-wrap { height:3px; background:rgba(255,255,255,0.08);
                 border-radius:2px; margin:8px 0 4px; overflow:hidden; }
.mini-bar      { height:100%; border-radius:2px; }
.reliability-label { font-size:9px; font-weight:600; text-transform:uppercase; }
.outlook-warning   { margin-top:12px; font-size:11px; color:#8b949e; line-height:1.6;
                     background:#161b22; border:1px solid rgba(255,255,255,0.08);
                     border-radius:6px; padding:10px 14px; }
.footer      { padding:12px 28px; background:#161b22;
               border-top:1px solid rgba(255,255,255,0.08);
               display:flex; justify-content:space-between; align-items:center;
               font-size:10px; color:#8b949e; flex-wrap:wrap; gap:8px; }
.footer-logo { font-family:'IBM Plex Mono',monospace; font-size:11px;
               font-weight:700; color:#58a6ff; letter-spacing:1px; }

/* === MEDIA QUERIES MOBILE === */
@media (max-width: 768px) {
  .header { flex-direction: column; align-items: flex-start; gap: 16px; }
  .header-right { text-align: left; width: 100%; }
  .chart-row { grid-template-columns: 1fr; }
  .decision-card { grid-template-columns: 48px 1fr; grid-template-rows: auto auto; }
  .decision-saving { grid-column: 1 / -1; text-align: left; margin-top: 8px; padding-top: 8px; border-top: 1px solid rgba(255,255,255,0.08); }
}
@media (max-width: 480px) {
  body { font-size: 13px; }
  .header { padding: 16px 16px 12px; }
  .section { padding: 16px; }
  .footer { padding: 12px 16px; }
  h1 { font-size: 18px; }
  .subtitle { font-size: 12px; }
  .kpi-strip { grid-template-columns: 1fr 1fr; }
  .kpi-card { padding: 12px; }
  .kpi-value { font-size: 16px; }
  .decision-card { grid-template-columns: 1fr; gap: 10px; }
  .decision-icon { width: 40px; height: 40px; font-size: 18px; }
  .decision-meta { flex-wrap: wrap; gap: 6px; }
  .decision-saving { grid-column: 1; text-align: left; }
  .outlook-grid { display: flex; overflow-x: auto; scroll-snap-type: x mandatory; -webkit-overflow-scrolling: touch; gap: 8px; padding-bottom: 8px; scrollbar-width: thin; scrollbar-color: rgba(255,255,255,0.1) transparent; }
  .outlook-grid::-webkit-scrollbar { height: 4px; }
  .outlook-grid::-webkit-scrollbar-track { background: transparent; }
  .outlook-grid::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.2); border-radius: 2px; }
  .outlook-day { flex: 0 0 140px; scroll-snap-align: start; padding: 14px 12px; }
  .outlook-icon { font-size: 26px; }
  .outlook-pv { font-size: 12px; }
  .outlook-temp { font-size: 11px; }
}
@media (max-width: 360px) {
  .kpi-strip { grid-template-columns: 1fr; }
  .badge { font-size: 9px; padding: 2px 8px; }
  .client-id { font-size: 12px; }
  .outlook-day { flex: 0 0 120px; }
}
"""

# ── RENDER COMPLETO ───────────────────────────────────────────────────────────

def render_html(data: dict) -> str:
    today     = data["today"]
    outlook   = data["outlook"]
    client_id = data["client"]["client_id"]

    body = (
        _render_header(data)
        + _render_kpis(today["kpis"])
        + '<div class="section"><div class="section-title">'
          'Decisiones operativas · Plan de acción</div>'
        + _render_decisions(today["decisions"])
        + "</div>"
        + _render_charts(today)
        + _render_outlook(outlook)
        + _render_footer(client_id, data["generated_at"])
    )

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Informe Energético · {client_id} · {today['date']}</title>
  <style>{CSS}</style>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
</head>
<body>{body}</body>
</html>"""


# ── GUARDAR ARCHIVOS ──────────────────────────────────────────────────────────

def _output_dir(client_id: str, report_date: str) -> Path:
    d = REPORTS_DIR / report_date / client_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_html(html: str, client_id: str, report_date: str) -> Path | None:
    if ENVIRONMENT == "DEV":
        out = _output_dir(client_id, report_date) / f"{client_id}_energy_report.html"
        out.write_text(html, encoding="utf-8")
        logger.info("[HTML] Informe guardado local: %s", out)
        return out
    else:
        _upload_s3(html, client_id, report_date)
        return None


def _upload_s3(html: str, client_id: str, report_date: str) -> None:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    if not S3_BUCKET:
        logger.error("[HTML] S3_BUCKET no definido en .env — abortando upload")
        return

    dated_key  = f"{S3_PREFIX}/{report_date}/{client_id}_energy_report.html"
    latest_key = f"{S3_PREFIX}/latest.html"
    content    = html.encode("utf-8")
    extra      = {"ContentType": "text/html; charset=utf-8"}

    try:
        s3 = boto3.client("s3", region_name=AWS_REGION)
        s3.put_object(Bucket=S3_BUCKET, Key=dated_key,  Body=content, **extra)
        logger.info("[HTML] ✓ Histórico : s3://%s/%s", S3_BUCKET, dated_key)
        s3.put_object(Bucket=S3_BUCKET, Key=latest_key, Body=content, **extra)
        logger.info("[HTML] ✓ Latest    : s3://%s/%s", S3_BUCKET, latest_key)
        base_url = f"http://{S3_BUCKET}.s3-website.{AWS_REGION}.amazonaws.com"
        logger.info("[HTML] URL fija CV → %s/%s", base_url, latest_key)
    except (BotoCoreError, ClientError) as exc:
        logger.error("[HTML] Error en upload S3: %s", exc)


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def generate_report(client_id: str) -> dict[str, Path | None]:
    """
    Entry point del módulo.
    Devuelve dict con paths: {"html": Path}
    """
    logger.info("[INIT] ── generate_report — cliente: %s ──────────────────", client_id)

    data = build_energy_decisions(client_id)
    if not data:
        logger.error("[ERROR] Sin datos de decisiones — abortando")
        return {"html": None}

    report_date = data["today"]["date"]
    html        = render_html(data)
    html_path   = save_html(html, client_id, report_date)

    logger.info("[DONE] generate_report — fecha: %s", report_date)
    return {"html": html_path}


if __name__ == "__main__":
    import sys
    client = sys.argv[1] if len(sys.argv) > 1 else "CLT-0001"
    result = generate_report(client)
    print(f"\n✓ HTML → {result['html']}")