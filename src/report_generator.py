"""
report_generator.py
────────────────────
Genera el informe diario de estrategia energética en HTML (y opcionalmente PDF).

Entrada:
    · Dict estructurado de gold_fact_energy_decisions.build_energy_decisions()

Salida:
    · HTML  → data_storage/reports/YYYY-MM-DD/{client_id}_energy_report.html
    · PDF   → misma carpeta (requiere playwright: pip install playwright &&
                             playwright install chromium)

Autoejectable: genera informe para CLT-0001 si se lanza directamente.
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

WX_ICON = {
    800: "☀️", 801: "🌤", 802: "⛅", 803: "🌥", 804: "☁️",
    500: "🌧", 501: "🌧", 502: "⛈️", 300: "🌦", 600: "🌨",
}


# ── HELPERS HTML ──────────────────────────────────────────────────────────────

def _wx_icon(weather_id: int | None) -> str:
    if weather_id is None:
        return "🌡"
    base = (weather_id // 100) * 100
    return WX_ICON.get(weather_id, WX_ICON.get(base, "🌡"))


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
    client  = data["client"]
    today   = data["today"]
    kpis    = today["kpis"]
    gen_at  = data["generated_at"]
    date_es = _fmt_date_es(today["date"])

    rel_color = _reliability_color(kpis["forecast_reliability"])

    return f"""
    <div class="header">
      <div class="header-left">
        <div class="badge">
          <span class="dot"></span>INFORME ACTIVO
        </div>
        <h1>Estrategia Energética · Plan de Acción</h1>
        <p class="subtitle">{client['name']} &nbsp;·&nbsp; Turno completo</p>
      </div>
      <div class="header-right">
        <div class="client-id">{client['client_id']}</div>
        <div class="report-date">{date_es}</div>
        <div class="generated">Generado: {gen_at}</div>
        <div class="reliability" style="color:{rel_color}">
          ● Fiabilidad previsión: <strong>{kpis['forecast_reliability'].upper()}</strong>
          {'· PVP confirmado OMIE' if kpis['has_pvp'] else '· Sin PVP (D+1 pendiente)'}
        </div>
      </div>
    </div>
    """


def _render_kpis(kpis: dict) -> str:
    pvp_min_str = f"{kpis['pvp_min']:.0f} €/MWh · {kpis['pvp_min_hour']}h" if kpis['pvp_min'] else "—"
    pvp_max_str = f"{kpis['pvp_max']:.0f} €/MWh · {kpis['pvp_max_hour']}h" if kpis['pvp_max'] else "—"

    cards = [
        ("FV pico hoy",        f"{kpis['pv_peak_kw']} kW",  f"Hora {kpis['pv_peak_hour']}h", "#58a6ff"),
        ("PVP mínimo",         pvp_min_str,                  "Ventana económica",              "#3fb950"),
        ("PVP máximo",         pvp_max_str,                  "Evitar consumo",                 "#f85149"),
        ("Horas solar activa", f"{kpis['hours_solar']}h",    "FV > 1 kW",                      "#58a6ff"),
        ("Horas precio bajo",  f"{kpis['hours_cheap']}h",    f"< 80 €/MWh",                    "#3fb950"),
        ("Horas precio alto",  f"{kpis['hours_expensive']}h",f"> 150 €/MWh",                   "#f85149"),
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

    # Datos para Chart.js
    labels    = json.dumps([f"{r['hour']}h" for r in pv_hours])
    pv_data   = json.dumps([round(r["pv_power_gen_kw"], 2) for r in pv_hours])
    pvp_data  = json.dumps([round(r["price_pvpc_eur_mwh"], 1) if r["price_pvpc_eur_mwh"] else None
                            for r in pvp_hours])
    pvp_colors = json.dumps([_pvp_bar_color(r["price_pvpc_eur_mwh"]) for r in pvp_hours])

    return f"""
    <div class="section">
      <div class="section-title">Precio de mercado (PVP) · Generación fotovoltaica</div>
      <div class="chart-row">
        <div class="chart-box">
          <div class="chart-label">PVP €/MWh — hoy</div>
          <div style="position:relative;height:180px;">
            <canvas id="chartPVP" role="img" aria-label="Precio PVP horario">Precios OMIE del día.</canvas>
          </div>
        </div>
        <div class="chart-box">
          <div class="chart-label">Generación FV prevista (kW)</div>
          <div style="position:relative;height:180px;">
            <canvas id="chartPV" role="img" aria-label="Generación fotovoltaica">Curva FV del día.</canvas>
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
        data: {{
          labels: labels,
          datasets: [{{ data: pvpData, backgroundColor: pvpColors,
                        borderRadius: 3, borderSkipped: false }}]
        }},
        options: {{
          responsive: true, maintainAspectRatio: false,
          plugins: {{ legend: {{ display: false }},
            tooltip: {{ callbacks: {{ label: ctx => ctx.parsed.y != null ? ctx.parsed.y.toFixed(0) + ' €/MWh' : 'Sin dato' }} }} }},
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
        data: {{
          labels: labels,
          datasets: [{{ data: pvData, borderColor:'#58a6ff',
                        backgroundColor:'rgba(88,166,255,0.15)',
                        fill:true, tension:0.4, pointRadius:3,
                        pointBackgroundColor:'#58a6ff' }}]
        }},
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
    </script>
    """


def _render_decisions(decisions: list[dict]) -> str:
    if not decisions:
        return "<p style='color:#8b949e;padding:16px'>Sin decisiones generadas para hoy.</p>"

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
    days = outlook.get("days", [])
    summary = outlook.get("summary_text", "")

    months = ["ene","feb","mar","abr","may","jun",
              "jul","ago","sep","oct","nov","dic"]
    day_names = ["Lun","Mar","Mié","Jue","Vie","Sáb","Dom"]

    day_cards = ""
    for d in days:
        dt    = datetime.strptime(d["date"], "%Y-%m-%d")
        label = f"{day_names[dt.weekday()]} {dt.day} {months[dt.month-1]}"
        rel_w = max(10, min(100, int((1 - d["rain_prob"]) * 100)))
        pv_w  = max(5,  min(100, int(d["pv_peak_kw"] / 15 * 100)))
        rel_c = _reliability_color(d["reliability"])

        day_cards += f"""
        <div class="outlook-day">
          <div class="outlook-label">{label}</div>
          <div class="outlook-icon">{_wx_icon(None)}</div>
          <div class="outlook-pv" style="color:#58a6ff">FV ~{d['pv_peak_kw']} kW</div>
          <div class="outlook-temp">{d['temp_min']:.0f}°–{d['temp_max']:.0f}°C
            · {d['clouds_pct']:.0f}% nub.</div>
          <div class="mini-bar-wrap">
            <div class="mini-bar" style="width:{pv_w}%;background:#58a6ff" title="FV relativa"></div>
          </div>
          
        </div>"""

    warning = """
    <div class="outlook-warning">
      ⚠ Datos climatológicos estimados — sin PVP corporativo. Margen de confianza adaptativo según horizonte temporal. Actualización cada mañana.
    </div>"""

    return f"""
    <div class="section">
      <div class="section-title">Outlook 5 días · Orientativo</div>
      <div class="outlook-summary">{summary}</div>
      <div class="outlook-grid">{day_cards}</div>
      {warning}
    </div>"""


def _render_footer(client_id: str, generated_at: str) -> str:
    return f"""
    <div class="footer">
      <span>Sistema de gestión energética · ETL SunSaver · gold.fact_energy_forecast
            · {client_id}</span>
      <span class="footer-logo">ENERGY·OS v2.1</span>
    </div>"""


# ── CSS ───────────────────────────────────────────────────────────────────────

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: #0d1117; color: #e6edf3;
  font-family: 'IBM Plex Sans', 'Segoe UI', sans-serif;
  font-size: 14px; line-height: 1.5;
}
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600;700&display=swap');

/* HEADER */
.header {
  background: #161b22; border-bottom: 1px solid rgba(255,255,255,0.1);
  padding: 20px 28px 16px;
  display: flex; justify-content: space-between; align-items: flex-start;
  flex-wrap: wrap; gap: 12px;
}
.badge {
  display: inline-flex; align-items: center; gap: 6px;
  background: rgba(63,185,80,0.15); border: 1px solid #3fb950;
  color: #3fb950; font-size: 10px; font-weight: 700; letter-spacing: 1.5px;
  padding: 3px 10px; border-radius: 3px; margin-bottom: 8px;
}
.dot {
  width: 6px; height: 6px; border-radius: 50%; background: #3fb950;
  animation: pulse 2s infinite;
}
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
h1 { font-size: 22px; font-weight: 700; }
.subtitle { font-size: 13px; color: #8b949e; margin-top: 4px; }
.header-right { text-align: right; }
.client-id { font-size: 13px; font-weight: 700; color: #58a6ff;
             font-family: 'IBM Plex Mono', monospace; }
.report-date { font-size: 12px; color: #8b949e; margin-top: 2px;
               font-family: 'IBM Plex Mono', monospace; }
.generated { font-size: 10px; color: #8b949e; margin-top: 4px; }
.reliability { font-size: 11px; margin-top: 6px; }

/* KPIs */
.kpi-strip {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(140px,1fr));
  gap: 1px; background: rgba(255,255,255,0.08);
  border-bottom: 1px solid rgba(255,255,255,0.08);
}
.kpi-card { background: #161b22; padding: 14px 16px; }
.kpi-label { font-size: 10px; color: #8b949e; text-transform: uppercase;
             letter-spacing: 1px; font-weight: 600; }
.kpi-value { font-size: 18px; font-weight: 700;
             font-family: 'IBM Plex Mono', monospace; margin-top: 4px; }
.kpi-sub { font-size: 11px; color: #8b949e; margin-top: 2px; }

/* SECTIONS */
.section { padding: 20px 28px; border-bottom: 1px solid rgba(255,255,255,0.08); }
.section-title {
  font-size: 10px; font-weight: 700; letter-spacing: 1.5px;
  text-transform: uppercase; color: #8b949e; margin-bottom: 16px;
  display: flex; align-items: center; gap: 8px;
}
.section-title::after {
  content:''; flex:1; height:1px; background: rgba(255,255,255,0.08);
}

/* CHARTS */
.chart-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.chart-box {
  background: #161b22; border: 1px solid rgba(255,255,255,0.08);
  border-radius: 6px; padding: 16px;
}
.chart-label { font-size: 11px; font-weight: 600; color: #8b949e; margin-bottom: 12px; }

/* DECISIONS */
.decisions-grid { display: flex; flex-direction: column; gap: 10px; }
.decision-card {
  background: #161b22; border: 1px solid rgba(255,255,255,0.08);
  border-radius: 6px; padding: 14px 16px;
  display: grid; grid-template-columns: 48px 1fr auto;
  align-items: start; gap: 14px;
}
.decision-icon {
  width: 44px; height: 44px; border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
}
.decision-meta { display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }
.decision-time {
  font-family: 'IBM Plex Mono', monospace; font-size: 12px;
  font-weight: 700; color: #58a6ff;
}
.decision-tag {
  font-size: 9px; font-weight: 700; letter-spacing: 1px;
  padding: 2px 8px; border-radius: 3px; text-transform: uppercase;
}
.decision-title { font-size: 13px; font-weight: 700; margin-bottom: 5px; }
.decision-reason { font-size: 12px; color: #8b949e; line-height: 1.55; }
.decision-saving {
  font-size: 11px; font-weight: 700; text-align: right; white-space: nowrap;
  font-family: 'IBM Plex Mono', monospace;
}

/* OUTLOOK */
.outlook-summary {
  font-size: 12px; color: #8b949e; line-height: 1.6;
  background: #161b22; border: 1px solid rgba(255,255,255,0.08);
  border-radius: 6px; padding: 12px 16px; margin-bottom: 14px;
}
.outlook-grid { display: grid; grid-template-columns: repeat(5,1fr); gap: 8px; }
.outlook-day {
  background: #161b22; border: 1px solid rgba(255,255,255,0.08);
  border-radius: 6px; padding: 12px 10px; text-align: center;
}
.outlook-label { font-size: 10px; color: #8b949e; text-transform: uppercase; }
.outlook-icon  { font-size: 22px; margin: 6px 0; }
.outlook-pv    { font-size: 11px; font-weight: 600; }
.outlook-temp  { font-size: 10px; color: #8b949e; margin-top: 4px; }
.mini-bar-wrap {
  height: 3px; background: rgba(255,255,255,0.08);
  border-radius: 2px; margin: 8px 0 4px; overflow: hidden;
}
.mini-bar { height: 100%; border-radius: 2px; }
.reliability-label { font-size: 9px; font-weight: 600; text-transform: uppercase; }
.outlook-warning {
  margin-top: 12px; font-size: 11px; color: #8b949e; line-height: 1.6;
  background: #161b22; border: 1px solid rgba(255,255,255,0.08);
  border-radius: 6px; padding: 10px 14px;
}

/* FOOTER */
.footer {
  padding: 12px 28px; background: #161b22;
  border-top: 1px solid rgba(255,255,255,0.08);
  display: flex; justify-content: space-between; align-items: center;
  font-size: 10px; color: #8b949e; flex-wrap: wrap; gap: 8px;
}
.footer-logo {
  font-family: 'IBM Plex Mono', monospace; font-size: 11px;
  font-weight: 700; color: #58a6ff; letter-spacing: 1px;
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
        + '<div class="section"><div class="section-title">Decisiones operativas · Plan de acción hoy</div>'
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


def save_html(html: str, client_id: str, report_date: str) -> Path:
    out = _output_dir(client_id, report_date) / f"{client_id}_energy_report.html"
    out.write_text(html, encoding="utf-8")
    logger.info("[HTML] Informe guardado: %s", out)
    return out


def save_pdf(html_path: Path) -> Path | None:
    """
    Convierte el HTML a PDF usando Playwright (headless Chromium).
    Requiere:  pip install playwright && playwright install chromium
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("[PDF] Playwright no instalado — omitiendo PDF. "
                       "Instalar con: pip install playwright && playwright install chromium")
        return None

    pdf_path = html_path.with_suffix(".pdf")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page    = browser.new_page()
            page.goto(html_path.resolve().as_uri(), wait_until="networkidle")
            page.pdf(
                path=str(pdf_path),
                format="A4",
                landscape=True,
                print_background=True,
                margin={"top": "10mm", "bottom": "10mm",
                        "left": "10mm", "right": "10mm"},
            )
            browser.close()
        logger.info("[PDF] PDF guardado: %s", pdf_path)
        return pdf_path
    except Exception as exc:
        logger.error("[PDF] Error generando PDF: %s", exc)
        return None


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def generate_report(client_id: str, export_pdf: bool = False) -> dict[str, Path | None]:
    """
    Entry point del módulo.
    Devuelve dict con paths: {"html": Path, "pdf": Path | None}
    """
    logger.info("[INIT] ── generate_report — cliente: %s ──────────────────", client_id)

    data = build_energy_decisions(client_id)
    if not data:
        logger.error("[ERROR] Sin datos de decisiones — abortando")
        return {"html": None, "pdf": None}

    report_date = data["today"]["date"]
    html        = render_html(data)
    html_path   = save_html(html, client_id, report_date)

    pdf_path = save_pdf(html_path) if export_pdf else None

    logger.info("[DONE] generate_report — fecha: %s | PDF: %s",
                report_date, "sí" if pdf_path else "no")

    return {"html": html_path, "pdf": pdf_path}


if __name__ == "__main__":
    import sys
    client  = sys.argv[1] if len(sys.argv) > 1 else "CLT-0001"
    pdf     = "--pdf" in sys.argv
    result  = generate_report(client, export_pdf=pdf)
    print(f"\n✓ HTML → {result['html']}")
    if result["pdf"]:
        print(f"✓ PDF  → {result['pdf']}")