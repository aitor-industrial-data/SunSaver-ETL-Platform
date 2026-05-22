# 06 · Guía de operaciones

[← README](../README.md)

---

## Monitorización de una ejecución

### CloudWatch Logs

Cada ejecución de Fargate escribe en el grupo `/ecs/sunsaver-etl`. El formato de cada línea es:

```
2026-05-23 21:05:12 | INFO     | run                           | PIPELINE SUNSAVER — inicio (UTC): 2026-05-23 19:05:12
2026-05-23 21:05:13 | INFO     | run                           | ── STAGE 1 ────────────────────────────────────────────
2026-05-23 21:05:13 | INFO     | run                           |   ▶  extract_clients ...
2026-05-23 21:05:14 | INFO     | run                           |   ✔  extract_clients completado (1.2s) | Filas: 2
```

Para seguir una ejecución en tiempo real desde la CLI:

```bash
aws logs tail /ecs/sunsaver-etl \
  --follow \
  --region eu-south-2
```

Para filtrar solo errores:

```bash
aws logs filter-log-events \
  --log-group-name /ecs/sunsaver-etl \
  --filter-pattern "ERROR" \
  --region eu-south-2
```

### Tabla de auditoría

La forma más rápida de ver el estado de las últimas ejecuciones es consultar `etl.etl_metadata`:

```sql
SELECT
    _executed_at,
    status,
    duration_seconds,
    rows_affected,
    error_message,
    env,
    _executed_by
FROM etl.etl_metadata
ORDER BY _executed_at DESC
LIMIT 10;
```

Resultados esperados en una ejecución normal:

| Campo | Valor típico |
|-------|-------------|
| `status` | `SUCCESS` |
| `duration_seconds` | 150–200 s |
| `rows_affected` | 800–1200 (depende del número de clientes y ventana de forecast) |
| `error_message` | `null` |
| `env` | `PRD` |

---

## Significado de cada status de auditoría

| Status | Significado | Acción |
|--------|-------------|--------|
| `SUCCESS` | Todos los steps completados sin errores | Ninguna |
| `PARTIAL SUCCESS` | Al menos un step devolvió `False` pero el pipeline continuó | Revisar CloudWatch para identificar el step fallido |
| `FAILED AT STAGE N` | Todos los steps del stage N fallaron; pipeline abortado | Revisar CloudWatch, relanzar desde `--stage N` cuando se resuelva |
| `CRITICAL ERROR` | Excepción no controlada en el orquestador | Revisar CloudWatch con urgencia; puede indicar fallo de infraestructura |

---

## Relanzar el pipeline desde un stage concreto

`run.py` acepta `--stage N` para arrancar desde cualquier stage sin repetir los anteriores. Útil cuando Bronze ya se ejecutó correctamente pero Silver falló.

```bash
# Ejemplo: Bronze OK, fallo en Silver (stage 2). Relanzar desde stage 2.
python src/run.py --stage 2

# Stages disponibles:
# 1 → Bronze ingest (clientes, activos, precios, contexto)
# 2 → Silver transform (clientes, activos, precios, contexto)
# 3 → Bronze weather (OpenWeatherMap)
# 4 → Silver weather
# 5 → Silver calculation (motor PV)
# 6 → Gold dimensions
# 7 → Gold facts (historical + forecast)
# 8 → Output (decisiones + informe HTML)
```

En Fargate, usar `--overrides` en el comando `aws ecs run-task` (ver `05_ci_cd_despliegue.md`).

---

## Cómo añadir un cliente nuevo

1. Abrir `clients_source.xlsx` desde S3 (`s3://sunsaver-bronze/inputs/clients_source.xlsx`).

2. En la hoja **Clients Data**, añadir una fila con los campos obligatorios:

   | Campo | Obligatorio | Notas |
   |-------|-------------|-------|
   | `client_id` | ✓ | Formato `CLT-XXXX`. Único. |
   | `name` | ✓ | Nombre de la instalación |
   | `latitude` | ✓ | Coordenada GPS decimal (ej: 42.803852) |
   | `longitude` | ✓ | Coordenada GPS decimal (ej: -1.701961) |
   | `pv_peak_power_kw` | ✓ | Potencia pico instalada en kW STC |
   | `nominal_load_kw` | ✓ | Carga nominal industrial. Si se deja vacío → `pv_peak_power_kw × 1.3` |
   | `loss_pct` | — | Default: 14. Rango válido: 0–90 |
   | `angle` | — | Default: 30. Inclinación 0–90° |
   | `aspect` | — | Default: 180 (sur). Rango: 1–360° |
   | `timezone` | — | Default: UTC. Usar `Europe/Madrid` para España |
   | `battery_capacity_kwh` | — | Default: 0 (sin batería) |

3. En la hoja **assets**, añadir los activos industriales del cliente con su `client_id`. Tipos válidos: `forklift_battery`, `compressor`, `cold_storage`, `pump`, `autoclave`, `lighting`, `other`.

4. Subir el Excel actualizado a S3:
   ```bash
   aws s3 cp clients_source.xlsx \
     s3://sunsaver-bronze/inputs/clients_source.xlsx \
     --region eu-south-2
   ```

5. En la próxima ejecución del pipeline (o manual), el cliente aparece automáticamente en todos los cálculos e informes.

> **Nota**: las coordenadas GPS determinan todo — posición solar, llamada a OWM, y cálculo de temperatura de célula. Verificar su exactitud antes de subir.

---

## Cómo interpretar los manifests de S3

Cada fuente Bronze tiene su manifest en `s3://sunsaver-bronze/bronze/manifests/`. El manifest es un array JSON de tareas:

```json
[
  {
    "source":     "clients_source.xlsx",
    "path":       "bronze/clients/clients_20260523_190512.json",
    "status":     "success",
    "created_at": "2026-05-23 19:05:12",
    "updated_at": "2026-05-23 19:05:18"
  },
  {
    "source":     "clients_source.xlsx",
    "path":       "bronze/clients/clients_20260522_190510.json",
    "status":     "success",
    "created_at": "2026-05-22 19:05:10",
    "updated_at": "2026-05-22 19:05:15"
  }
]
```

| Status | Significado |
|--------|-------------|
| `pending` | Archivo Bronze ingestado, pendiente de procesar en Silver |
| `success` | Procesado correctamente en Silver |
| `error` | Falló en Silver. El campo `error` contiene el motivo. Se reintentará en la próxima ejecución. |

Para listar los manifests disponibles:

```bash
aws s3 ls s3://sunsaver-bronze/bronze/manifests/ --region eu-south-2
```

Para inspeccionar un manifest:

```bash
aws s3 cp s3://sunsaver-bronze/bronze/manifests/_process_manifest_ree.json - \
  --region eu-south-2 | python -m json.tool
```

---

## Qué hacer cuando falla cada fuente

### ESIOS precios (`bronze_ingest_prices_ree.py`)

**Síntoma**: `[EXTRACT] REE sin valores PVPC para YYYY-MM-DD` en CloudWatch.

**Causa más frecuente**: se ejecutó antes de las 20:30 CET, hora en que ESIOS publica los precios D+1. El pipeline está programado a las 21:05 CET para evitar esto, pero un retraso en la publicación de ESIOS puede ocurrir.

**Efecto**: `fact_energy_forecast` no tendrá `price_pvpc_eur_mwh` para D+1. El informe se genera igualmente con ese campo en `null` y fiabilidad marcada como `baja`.

**Acción**: relanzar desde stage 1 o 2 cuando ESIOS publique. Los precios se upsertarán y el informe se regenerará.

---

### ESIOS contexto (`bronze_ingest_context.py`)

**Síntoma**: `[EXTRACT] Sin datos D-1 — PARTIAL SUCCESS`.

**Causa frecuente**: uno o varios indicadores ESIOS (1293, 1295, 10355, 685) no devuelven valores. La API de ESIOS puede tener latencia en la consolidación de D−1.

**Efecto**: `fact_energy_historical` tendrá `null` en los campos `national_*` para ese día. El histórico seguirá siendo consistente; solo faltan los indicadores del sistema peninsular.

**Acción**: relanzar desde stage 1 al día siguiente. El upsert en `fact_energy_historical` completará los campos que faltaban.

---

### OpenWeatherMap (`bronze_ingest_weather_owm.py`)

**Síntoma**: `[EXTRACT] Error weather (lat=XX, lon=YY): ...` para uno o varios clientes.

**Causas posibles**: timeout de red, rate limit de la API key, coordenadas GPS inválidas.

**Efecto**: el cliente afectado no tendrá datos Silver de weather → no se calculará su generación FV → no aparecerá en el informe de ese cliente.

**Acción**: el manifest queda en `error` y se reintenta automáticamente en la próxima ejecución. Si el error persiste, verificar `WEATHER_API_KEY` en SSM y las coordenadas del cliente en el Excel.

---

### RDS PostgreSQL

**Síntoma**: `[DB] Faltan variables de conexión` o `SSL connection has been closed unexpectedly`.

**Causas posibles**: SSM no inyectó las variables (problema de permisos IAM del task role), RDS en mantenimiento, security group bloqueando el acceso desde la subnet de Fargate.

**Acción**:
1. Verificar que el task role (`sunsaver-etl-task-role`) tiene permisos `ssm:GetParameters` sobre los ARNs de los parámetros.
2. Verificar que el security group de Fargate tiene salida al security group de RDS en el puerto 5432.
3. Consultar el panel de RDS en la consola AWS para ver si hay mantenimiento programado.

---

### S3 (`config_paths.py`)

**Síntoma**: `[S3] Error subiendo` o `[S3] Error descargando`.

**Causa más frecuente**: el task role no tiene los permisos S3 necesarios (`s3:PutObject`, `s3:GetObject`, `s3:ListBucket`) sobre el bucket `sunsaver-bronze`.

**Acción**: revisar la política IAM del rol `sunsaver-etl-task-role` en la consola de AWS.

---

## Consultas útiles para analistas

```sql
-- Generación FV total por cliente en los últimos 7 días
SELECT
    client_id,
    DATE(forecast_time_utc) AS fecha,
    ROUND(SUM(pv_gen_kw)::numeric, 1) AS kwh_total,
    ROUND(MAX(pv_gen_kw)::numeric, 2) AS pico_kw
FROM gold.fact_energy_historical
WHERE forecast_time_utc >= NOW() - INTERVAL '7 days'
GROUP BY client_id, DATE(forecast_time_utc)
ORDER BY client_id, fecha DESC;

-- Horas con generación FV activa vs precio alto (oportunidades de ahorro)
SELECT
    f.client_id,
    d.datetime_local,
    d.tariff_label,
    ROUND(f.pv_gen_kw::numeric, 2) AS pv_gen_kw,
    ROUND(f.national_price_pvpc_eur_mwh::numeric, 2) AS pvpc_eur_mwh
FROM gold.fact_energy_historical f
JOIN gold.dim_datetime d ON d.unix_time = f.unix_time
WHERE f.pv_gen_kw > 1
  AND f.national_price_pvpc_eur_mwh > 150
ORDER BY f.unix_time DESC
LIMIT 50;

-- Performance Ratio medio por cliente (indicador de degradación)
SELECT
    client_id,
    DATE_TRUNC('week', forecast_time_utc) AS semana,
    ROUND(AVG(pv_performance_ratio)::numeric, 3) AS pr_medio,
    COUNT(*) AS horas_solar
FROM gold.fact_energy_historical
WHERE pv_gen_kw > 0.5
GROUP BY client_id, DATE_TRUNC('week', forecast_time_utc)
ORDER BY client_id, semana DESC;

-- Últimas ejecuciones del pipeline
SELECT
    _executed_at,
    status,
    duration_seconds,
    rows_affected,
    error_message
FROM etl.etl_metadata
ORDER BY _executed_at DESC
LIMIT 20;
```

---

[← CI/CD](05_ci_cd_despliegue.md) · [↑ README](../README.md)