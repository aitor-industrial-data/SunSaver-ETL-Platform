# 04 · Informe operativo y motor de decisiones

[← README](../README.md)

---

## Qué es el informe

El informe es el output final del pipeline: un archivo HTML autónomo publicado en S3 cada noche, accesible en una URL fija, diseñado para ser leído por el responsable de planta antes de que empiece el turno.

**[→ Ver informe live](https://sunsaver-bronze.s3.eu-south-2.amazonaws.com/reports/latest.html)**

No es un dashboard de monitorización en tiempo real. Es un **plan de acción para el día siguiente**: qué hacer, cuándo hacerlo y por qué, expresado en lenguaje operativo sin jerga técnica.

---

## Ciclo de vida del informe

```
gold.fact_energy_forecast   ─┐
gold.dim_assets              ├─►  gold_fact_energy_decisions.py
gold.dim_client             ─┘         (motor de reglas → dict)
                                              │
                                              ▼
                                    report_generator.py
                                    (renderiza HTML con Chart.js)
                                              │
                              ┌───────────────┴───────────────┐
                              ▼                               ▼
                    S3: reports/{date}/             S3: reports/latest.html
                    {client_id}_energy_report.html  (URL fija, ACL pública)
                    (histórico por fecha)
```

El informe histórico fechado permite auditar qué se recomendó cada día. La URL `latest.html` es siempre el más reciente.

---

## Motor de decisiones (`gold_fact_energy_decisions.py`)

El motor no persiste en base de datos. Lee de Gold, aplica reglas, devuelve un dict estructurado que el generador de informe consume directamente.

### Datos de entrada

```python
_load_client(conn, client_id)     # Metadatos: timezone, potencia, batería
_load_assets(conn, client_id)     # Activos ordenados por prioridad
_load_forecast(conn, client_id, target_date)  # Previsión D+1 + 5 días
```

`target_date` = `date.today() + 1` — el ETL corre a las 21h y genera el plan del día siguiente completo (00–23h).

Toda la lógica interna trabaja en UTC. La conversión a hora local del cliente (`ZoneInfo(client.timezone)`) ocurre solo al serializar los datos para el informe. Así las decisiones son correctas aunque un cliente esté en zona horaria diferente.

### Clasificación horaria

Cada hora del día se clasifica en uno de cuatro estados:

```python
def _classify_hour(pvp, pv_kw):
    if pv_kw >= 1.0:    return "solar"   # Generación FV activa
    if pvp < 80:        return "low"     # Precio barato → cargar
    if pvp > 150:       return "high"    # Precio caro → reducir
    return "mid"                         # Precio normal
```

Umbrales actuales: `PVP_LOW = 80 €/MWh`, `PVP_HIGH = 150 €/MWh`, `PV_ACTIVE = 1 kW`.

> ⚠️ Estos umbrales están centralizados como constantes en el módulo. Cuando se implementen reglas más elaboradas (precio dinámico por perfil de cliente, umbrales adaptativos por histórico...) solo hay que modificar este punto.

### Reglas por tipo de activo

El motor itera sobre los activos del cliente y aplica una regla según `asset_type` e `is_flexible`:

**`forklift_battery` (carretillas)** — Si es flexible y hay horas baratas dentro de su ventana horaria: *"Programar carga nocturna"*. La razón incluye precio mínimo exacto, ventana recomendada y horas a evitar (precio alto).

**`cold_storage` (cámara frigorífica)** — Si es flexible: *"Pre-enfriar durante generación solar"* aprovechando el excedente FV para diferir consumo de red. Si hay horas de precio alto sin solar: *"Mantener temperatura — no abrir puertas"*.

**`compressor`** — Si hay horas solares: *"Sincronizar ciclos con generación FV"*. Si hay horas de precio alto: *"Diferir arranques hasta ventana económica"* con urgencia `high`.

**`pump`** — Si hay horas baratas o solares: *"Activar bombeo en ventana económica"*. Las bombas suelen ser el activo más fácil de desplazar.

**`autoclave`** — Alta potencia, baja flexibilidad. Si hay horas de precio alto: *"Completar ciclo antes de hora pico"* con urgencia `critical`.

**`lighting`** — Recordatorio de apagar iluminación exterior/no productiva en horas de precio alto.

**Activos no flexibles** — Si coinciden con horas de precio alto: *"Monitorizar consumo"* con urgencia `low`.

Las decisiones se ordenan por urgencia (`critical > high > medium > low`) y luego por prioridad del activo definida en el Excel.

### Estructura de una decisión

```python
{
    "asset_id":    "AST-001",
    "asset_name":  "Carretilla elevadora nave A",
    "asset_type":  "forklift_battery",
    "priority":    1,
    "time_window": "01h–05h",        # hora local del cliente
    "action":      "Programar carga nocturna",
    "reason":      "PVP en mínimos (62 €/MWh). Cargar al 100% entre 01h–05h antes del turno. Evitar carga en horas pico (19h, 20h, 21h, >150 €/MWh).",
    "saving_tag":  "Ahorro en carga",
    "urgency":     "high",
}
```

### KPIs del día

Junto a las decisiones el motor calcula los KPIs del cabecero del informe:

| KPI | Cálculo |
|-----|---------|
| `pv_peak_kw` | `max(pv_power_gen_kw)` en el día |
| `pv_peak_hour` | Hora local de máxima generación |
| `pvp_min` / `pvp_max` | Mínimo y máximo PVPC con su hora |
| `hours_solar` | Horas con FV > 1 kW |
| `hours_cheap` | Horas con PVPC < 80 €/MWh |
| `hours_expensive` | Horas con PVPC > 150 €/MWh |
| `forecast_reliability` | `"alta"` si hay PVP confirmado, `"baja"` si no |

### Outlook semanal (5 días)

Para los días D+2 a D+6 (sin precio PVPC disponible) el motor genera un resumen orientativo por día:

```python
{
    "date":        "2026-05-24",
    "pv_peak_kw":  11.3,
    "clouds_pct":  45.0,
    "rain_prob":   0.28,
    "temp_max":    22.1,
    "temp_min":    14.3,
    "hours_pv":    7,
    "weather_id":  802,
    "reliability": "baja",   # Sin PVP — solo orientativo
}
```

El tono del resumen textual semanal se genera automáticamente según la nubosidad media y la probabilidad de lluvia acumulada:

- Nubosidad < 40% y FV media > 7 kW → *"semana con buena generación FV prevista"*
- Nubosidad > 65% o ≥3 días con lluvia > 50% → *"semana con generación FV limitada"*
- Resto → *"semana con generación moderada e inestable"*

---

## Generador de informe (`report_generator.py`)

Renderiza el dict del motor de decisiones en HTML puro (sin framework frontend) usando Chart.js para los gráficos.

### Secciones del informe

**Cabecero** — Cliente, fecha en español (Lun 23 may 2026), timestamp de generación, badge `INFORME ACTIVO` animado, indicador de fiabilidad de previsión con color (verde = PVP confirmado, rojo = sin PVP).

**KPI strip** — 6 tarjetas horizontales: pico FV con hora, PVP mínimo con hora, PVP máximo con hora, horas solar activa, horas precio bajo, horas precio alto.

**Plan de acción** — Lista de decisiones ordenadas por urgencia. Cada decisión muestra: emoji del tipo de activo, ventana horaria en `IBM Plex Mono`, badge de urgencia con color, título de la acción, razón detallada y tag de ahorro.

**Gráficos duales** — Curva de generación FV horaria (área azul) y barras de precio PVPC hora a hora (color semáforo: verde < 80, ámbar 80–150, rojo > 150). Chart.js 4.4.1 cargado desde CDN.

**Outlook 5 días** — Tarjetas por día con emoji meteorológico (mapeado desde `weather_id` OWM), pico FV previsto, temperatura min/max y barra de fiabilidad.

**Footer** — ID de cliente en monospace, timestamp de generación, marca SunSaver.

### Publicación en S3

En producción (`ENVIRONMENT=PRD`), el informe se sube en dos objetos:

```
s3://sunsaver-bronze/reports/2026-05-23/CLT-0001_energy_report.html    # histórico
s3://sunsaver-bronze/reports/latest.html                               # URL fija
```

Solo `latest.html` tiene ACL pública. El histórico fechado es privado y sirve para auditoría. La URL fija nunca cambia, lo que permite compartirla como enlace permanente (en un CV, en una reunión, en un email al cliente).

---

## Extensibilidad del motor de decisiones

El motor está diseñado para añadir reglas sin tocar la arquitectura. Próximas extensiones previstas:

- **Precio dinámico por perfil** — umbrales distintos por tipo de industria o contrato
- **Reglas de batería** — ciclos de carga/descarga optimizados si el cliente tiene almacenamiento
- **Alertas de mantenimiento preventivo** — cruzar histórico de PR con umbrales de degradación
- **Recomendaciones de curtailment** — cuando la generación supera el consumo y no hay red de salida
- **Score de oportunidad diario** — métrica agregada que resuma en un número el potencial de ahorro del día

---

[← Modelo de datos](03_modelo_datos.md) · [CI/CD →](05_ci_cd_despliegue.md) · [↑ README](../README.md)