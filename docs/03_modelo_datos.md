# 03 · Modelo de datos

[← README](../README.md)

---

## Visión general

La base de datos RDS PostgreSQL organiza los datos en tres schemas. `silver` contiene los datos curados listos para el cálculo. `gold` es el star schema que consumen los analistas y el motor de decisiones. `etl` registra la auditoría de cada ejecución del pipeline.

```
silver.clean_clients        →   gold.dim_client
silver.clean_assets         →   gold.dim_assets
silver.clean_weather        →   gold.dim_weather
silver.clean_calculations   ┐
silver.clean_weather        ├─► gold.dim_datetime
silver.clean_prices         │
silver.clean_context        ┘
silver.clean_calculations   ┐
silver.clean_weather        ├─► gold.fact_energy_historical
silver.clean_prices         │
silver.clean_context        ┘
silver.clean_calculations   ┐
silver.clean_weather        ├─► gold.fact_energy_forecast
silver.clean_prices         ┘

run.py (orquestador)        →   etl.etl_metadata
```

---

## Schema Silver — Datos curados

### `silver.clean_clients`

Parámetros físicos y de negocio de cada instalación. Se reconstruye completa en cada ejecución (DROP + CREATE + INSERT). Deduplicación por `client_id` manteniendo el registro más reciente.

| Campo | Tipo SQL | Descripción |
|-------|----------|-------------|
| `client_id` | TEXT PK | Identificador único (ej: CLT-0001) |
| `name` | TEXT | Nombre de la instalación (normalizado UPPER) |
| `description` | TEXT | Descripción libre |
| `latitude` | REAL | Coordenada GPS — 6 decimales |
| `longitude` | REAL | Coordenada GPS — 6 decimales |
| `nominal_load_kw` | REAL | Carga nominal industrial del cliente |
| `pv_peak_power_kw` | REAL | Potencia pico instalada en STC |
| `panel_area_m2` | REAL | Superficie total de panel |
| `efficiency` | REAL | Eficiencia del panel (0–1) |
| `panel_type` | TEXT | Tipo de panel |
| `loss_pct` | REAL | Pérdidas de sistema % (inversor, cableado…) |
| `angle` | REAL | Inclinación del panel (0–90°) |
| `aspect` | REAL | Orientación (1–360°, 180 = sur) |
| `mounting` | TEXT | Tipo de montaje |
| `battery_capacity_kwh` | REAL | Capacidad de batería (0 si no tiene) |
| `soc_min_pct` | REAL | Estado de carga mínimo permitido % |
| `installation_cost_eur` | REAL | Coste de instalación |
| `timezone` | TEXT | Zona horaria local (ej: Europe/Madrid) |
| `_source_file` | TEXT | Nombre del archivo Bronze de origen |
| `_ingested_at_utc` | TIMESTAMPTZ | Timestamp de ingesta Bronze |

**Validaciones**: coordenadas en rango físico, `angle` 0–90 (default 30), `aspect` 1–360 (default 180), `loss_pct` 0–90 (default 14), `pv_peak_power_kw > 0`. Valores fuera de rango se corrigen al default, no se descartan.

---

### `silver.clean_assets`

Activos industriales desplazables o monitorizados por cliente.

| Campo | Tipo SQL | Descripción |
|-------|----------|-------------|
| `asset_id` | TEXT PK | Identificador único del activo |
| `client_id` | TEXT | FK → clean_clients |
| `asset_name` | TEXT | Nombre descriptivo |
| `asset_type` | TEXT | Tipo: `forklift_battery`, `compressor`, `cold_storage`, `pump`, `autoclave`, `lighting`, `other` |
| `power_kw` | REAL | Potencia nominal del activo |
| `capacity_kwh` | REAL | Capacidad (baterías y almacenamiento frío) |
| `is_flexible` | INTEGER | 1 si el activo puede desplazarse en el tiempo, 0 si no |
| `flex_window_start` | INTEGER | Inicio ventana de flexibilidad (hora local, 0–23) |
| `flex_window_end` | INTEGER | Fin ventana de flexibilidad (hora local, 0–23) |
| `priority` | INTEGER | Prioridad de despacho (1 = más prioritario, default 99) |
| `notes` | TEXT | Observaciones del operario |
| `_source_file` | TEXT | Archivo Bronze de origen |
| `_ingested_at_utc` | TIMESTAMPTZ | Timestamp de ingesta |

**Validaciones**: `asset_type` normalizado a minúsculas; valores no reconocidos → `other`. `flex_window_start` debe ser menor que `flex_window_end`; si no, se resetea a 0–23.

---

### `silver.clean_weather`

Forecast meteorológico por cliente y hora. OWM devuelve intervalos de 3h que se interpolan a 1h con resample + interpolación lineal para numéricos y forward-fill para categóricos.

| Campo | Tipo SQL | Descripción |
|-------|----------|-------------|
| `client_id` | TEXT PK | FK → clean_clients |
| `unix_time` | BIGINT PK | Timestamp UNIX UTC |
| `forecast_time_utc` | TIMESTAMPTZ | Timestamp UTC |
| `temp_celsius` | DOUBLE PRECISION | Temperatura ambiente |
| `humidity_pct` | DOUBLE PRECISION | Humedad relativa % |
| `clouds_pct` | DOUBLE PRECISION | Cobertura nubosa % (0–100) |
| `rain_prob_norm` | DOUBLE PRECISION | Probabilidad de lluvia (0–1, `pop` de OWM) |
| `wind_speed_mps` | DOUBLE PRECISION | Velocidad de viento m/s |
| `weather_id` | INTEGER | Código OWM (800=claro, 2xx=tormenta…) |
| `weather_main` | TEXT | Categoría principal OWM |
| `weather_description` | TEXT | Descripción detallada OWM |
| `is_daylight` | INTEGER | 1 si hay luz solar (`pod=d` de OWM), 0 si no |
| `_source_file` | TEXT | Archivo Bronze de origen |
| `_ingested_at_utc` | TIMESTAMPTZ | Timestamp de ingesta |

---

### `silver.clean_prices`

Precios PVPC hora a hora extraídos de ESIOS/REE. Se hace upsert en cada ejecución. Incluye ventana de ayer, hoy y mañana (cuando está disponible tras las 20:30 CET).

| Campo | Tipo SQL | Descripción |
|-------|----------|-------------|
| `unix_time` | BIGINT | Timestamp UNIX UTC |
| `datetime_utc` | TIMESTAMPTZ PK | Timestamp UTC |
| `price_type` | TEXT PK | Tipo de precio (ej: `PVPC`) |
| `price_euro_mwh` | DOUBLE PRECISION | Precio en €/MWh. Interpolación lineal para huecos. Outliers filtrados: < −100 o > 2000 €/MWh |
| `_source_file` | TEXT | Archivo Bronze de origen |
| `_ingested_at_utc` | TIMESTAMPTZ | Timestamp de ingesta |

---

### `silver.clean_context`

Indicadores del sistema eléctrico peninsular de D−1 extraídos de ESIOS. Formato largo (una fila = un indicador × un timestamp). Se hace upsert preservando histórico.

| Campo | Tipo SQL | Descripción |
|-------|----------|-------------|
| `unix_time` | BIGINT PK | Timestamp UNIX UTC |
| `datetime_utc` | TIMESTAMPTZ PK | Timestamp UTC |
| `indicator_name` | VARCHAR(100) PK | Nombre del indicador: `demand_real`, `pv_gen`, `co2`, `upward_imb` |
| `indicator_id` | INTEGER | ID numérico ESIOS del indicador |
| `value` | DOUBLE PRECISION | Valor medido. Validado por rangos: demanda 5000–600000 MW, pv_gen 0–500000 MW, co2 0–100000 tCO2/h, upward_imb ±50000000 |
| `_source_file` | TEXT | Archivo Bronze de origen |
| `_ingested_at_utc` | TIMESTAMPTZ | Timestamp de ingesta |

**Indicadores ESIOS ingestados**:

| `indicator_name` | ID ESIOS | Unidad | Descripción |
|-----------------|----------|--------|-------------|
| `demand_real` | 1293 | MWh | Demanda real peninsular |
| `pv_gen` | 1295 | MWh | Generación fotovoltaica tiempo real |
| `co2` | 10355 | tCO2/MWh | CO2 asociado a la generación |
| `upward_imb` | 685 | €/MWh | Desvío a subir en el mercado |

---

### `silver.clean_calculations`

Resultados del motor PV por cliente y hora. Upsert idempotente en cada ejecución (`ON CONFLICT DO UPDATE`): relanzar el pipeline para el mismo periodo es seguro.

| Campo | Tipo SQL | Descripción |
|-------|----------|-------------|
| `client_id` | TEXT PK | FK → clean_clients |
| `unix_time` | BIGINT PK | Timestamp UNIX UTC |
| `forecast_time_utc` | TIMESTAMPTZ | Timestamp UTC |
| `pv_power_gen_kw` | DOUBLE PRECISION | Generación FV calculada por el motor físico |
| `pv_performance_ratio` | DOUBLE PRECISION | PR efectivo (incluye derating térmico) |
| `poa_wm2` | DOUBLE PRECISION | Irradiancia sobre el plano del panel W/m² |
| `t_cell_celsius` | DOUBLE PRECISION | Temperatura de célula modelo Faiman |
| `power_con_kw` | DOUBLE PRECISION | Consumo industrial simulado |
| `calculated_at_utc` | TIMESTAMPTZ | Timestamp del cálculo |

---

## Schema Gold — Star Schema

### Dimensiones

#### `gold.dim_client`

| Campo | Tipo SQL | Descripción |
|-------|----------|-------------|
| `client_id` | TEXT PK | Identificador del cliente |
| `name` | TEXT | Nombre normalizado |
| `description` | TEXT | Descripción de la instalación |
| `latitude` | DOUBLE PRECISION | Coordenada GPS |
| `longitude` | DOUBLE PRECISION | Coordenada GPS |
| `timezone` | TEXT | Zona horaria local |
| `nominal_load_kw` | DOUBLE PRECISION | Carga nominal industrial |
| `pv_peak_power_kw` | DOUBLE PRECISION | Potencia FV pico instalada |
| `panel_area_m2` | DOUBLE PRECISION | Superficie de panel |
| `efficiency` | DOUBLE PRECISION | Eficiencia del panel (0–1) |
| `panel_type` | TEXT | Tipo de panel |
| `loss_pct` | DOUBLE PRECISION | Pérdidas de sistema % |
| `angle` | DOUBLE PRECISION | Inclinación del panel |
| `aspect` | DOUBLE PRECISION | Orientación del panel |
| `mounting` | TEXT | Tipo de montaje |
| `battery_capacity_kwh` | DOUBLE PRECISION | Capacidad de batería |
| `soc_min_pct` | DOUBLE PRECISION | SOC mínimo permitido % |
| `installation_cost_eur` | DOUBLE PRECISION | Coste de instalación |
| `has_solar` | INTEGER | **Flag derivado** — 1 si `pv_peak_power_kw > 0` |
| `has_battery` | INTEGER | **Flag derivado** — 1 si `battery_capacity_kwh > 0` |
| `_loaded_at_utc` | TIMESTAMPTZ | Timestamp de carga en Gold |

---

#### `gold.dim_assets`

| Campo | Tipo SQL | Descripción |
|-------|----------|-------------|
| `asset_id` | TEXT PK | Identificador del activo |
| `client_id` | TEXT | FK → dim_client |
| `asset_name` | TEXT | Nombre |
| `asset_type` | TEXT | Tipo de activo (enum validado en Silver) |
| `power_kw` | REAL | Potencia nominal |
| `capacity_kwh` | REAL | Capacidad de almacenamiento |
| `is_flexible` | INTEGER | 1 si el activo es desplazable, 0 si no |
| `flex_window_start` | INTEGER | Inicio ventana de flexibilidad (hora local) |
| `flex_window_end` | INTEGER | Fin ventana de flexibilidad (hora local) |
| `priority` | INTEGER | Orden de despacho (1 = más prioritario) |
| `notes` | TEXT | Observaciones |
| `has_capacity` | INTEGER | **Flag derivado** — 1 si `capacity_kwh > 0` |
| `is_overnight_flexible` | INTEGER | **Flag derivado** — 1 si `is_flexible=1` AND `flex_window_start ≤ 2` AND `flex_window_end ≥ 5` |
| `_loaded_at_utc` | TIMESTAMPTZ | Timestamp de carga en Gold |

---

#### `gold.dim_weather`

Catálogo de condiciones meteorológicas observadas. Deduplicado por `weather_id` mediante `ROW_NUMBER() OVER (PARTITION BY weather_id ORDER BY COUNT(*) DESC)` — se elige el par `(main, description)` más frecuente para cada código.

| Campo | Tipo SQL | Descripción |
|-------|----------|-------------|
| `weather_id` | INTEGER PK | Código OWM (ej: 800, 803, 500…) |
| `weather_main` | TEXT | Categoría principal OWM |
| `weather_description` | TEXT | Descripción más frecuente observada para ese código |
| `_loaded_at_utc` | TIMESTAMPTZ | Timestamp de carga en Gold |

---

#### `gold.dim_datetime`

Dimensión temporal enriquecida con atributos de negocio eléctrico español. Se genera desde la unión de unix_times de todas las tablas Silver (`clean_calculations`, `clean_weather`, `clean_prices`, `clean_context`) para cobertura completa.

| Campo | Tipo SQL | Descripción |
|-------|----------|-------------|
| `unix_time` | BIGINT PK | Timestamp UNIX UTC |
| `datetime_utc` | TIMESTAMPTZ | Fecha y hora UTC |
| `datetime_local` | TEXT | Fecha y hora local Madrid (Europe/Madrid) |
| `date` | DATE | Fecha UTC |
| `hour_utc` | SMALLINT | Hora UTC (0–23) |
| `hour_local` | SMALLINT | Hora local Madrid (0–23) |
| `day_of_week` | TEXT | Día de la semana en inglés lowercase (monday…sunday) |
| `is_daylight` | INTEGER | 1 si hora local entre 6 y 21 inclusive |
| `is_weekend` | INTEGER | 1 si sábado o domingo |
| `is_festivo` | INTEGER | 1 si festivo nacional español (9 festivos codificados) |
| `month` | SMALLINT | Mes local (1–12) |
| `year` | SMALLINT | Año local |
| `tariff_period` | TEXT | Período tarifario español: `P1`, `P2`, `P3`, `P6` |
| `tariff_label` | TEXT | Etiqueta legible: `punta`, `llano`, `valle`, `super-valle` |
| `_loaded_at_utc` | TIMESTAMPTZ | Timestamp de carga en Gold |

**Lógica de períodos tarifarios** (hora local Madrid):
- `P6` (super-valle): festivos y fines de semana — todas las horas
- `P1` (punta): laborables 10–14h y 18–22h
- `P2` (llano): laborables 08–10h, 14–18h, 22–24h
- `P3` (valle): laborables 00–08h

---

### Tablas de hechos

#### `gold.fact_energy_historical`

Serie histórica completa. **Solo crece**: upsert incremental con `ON CONFLICT DO UPDATE`. Fuente: `gold.fact_energy_forecast` (filas pasadas) enriquecida con contexto ESIOS D−1 pivotado inline. Debe ejecutarse **antes** que `fact_energy_forecast` (que trunca su tabla).

**Grain**: una fila = un cliente × una hora UTC.

| Campo | Tipo SQL | Descripción |
|-------|----------|-------------|
| `client_id` | TEXT PK | FK → dim_client |
| `unix_time` | BIGINT PK | FK → dim_datetime |
| `forecast_time_utc` | TIMESTAMPTZ | Timestamp de la previsión |
| `pv_gen_kw` | DOUBLE PRECISION | Generación FV prevista |
| `pv_performance_ratio` | DOUBLE PRECISION | PR efectivo del sistema |
| `poa_wm2` | DOUBLE PRECISION | Irradiancia POA W/m² |
| `t_cell_celsius` | DOUBLE PRECISION | Temperatura de célula Faiman |
| `consumption_kw` | DOUBLE PRECISION | Consumo industrial simulado |
| `temp_celsius` | DOUBLE PRECISION | Temperatura ambiente |
| `humidity_pct` | DOUBLE PRECISION | Humedad % |
| `clouds_pct` | DOUBLE PRECISION | Nubosidad % |
| `rain_prob_norm` | DOUBLE PRECISION | Probabilidad de lluvia (0–1) |
| `wind_speed_mps` | DOUBLE PRECISION | Velocidad de viento m/s |
| `weather_id` | INTEGER | FK → dim_weather |
| `national_price_pvpc_eur_mwh` | DOUBLE PRECISION | Precio PVPC ESIOS €/MWh |
| `national_demand_mw` | DOUBLE PRECISION | Demanda real peninsular MW (indicador 1293) |
| `national_pv_gen_mw` | DOUBLE PRECISION | Generación FV total peninsular MW (indicador 1295) |
| `national_co2_tco2_mwh` | DOUBLE PRECISION | CO2 de generación tCO2/MWh (indicador 10355) |
| `national_upward_imb_mw` | DOUBLE PRECISION | Desvío a subir €/MWh (indicador 685) |
| `_loaded_at_utc` | TIMESTAMPTZ | Timestamp de carga |

**Índices**: `idx_gold_hist_unix_time` sobre `unix_time`, `idx_gold_hist_weather_id` sobre `weather_id`.

---

#### `gold.fact_energy_forecast`

Ventana de previsión futura activa. **TRUNCATE + INSERT** en cada ejecución: solo contiene filas con `unix_time >= now()`. El histórico vive en `fact_energy_historical`.

**Grain**: una fila = un cliente × una hora UTC futura.

| Campo | Tipo SQL | Descripción |
|-------|----------|-------------|
| `client_id` | TEXT PK | FK → dim_client |
| `unix_time` | BIGINT PK | FK → dim_datetime |
| `forecast_time_utc` | TIMESTAMPTZ | Timestamp UTC |
| `pv_power_gen_kw` | DOUBLE PRECISION | Generación FV prevista |
| `pv_performance_ratio` | DOUBLE PRECISION | PR efectivo |
| `poa_wm2` | DOUBLE PRECISION | Irradiancia POA W/m² |
| `t_cell_celsius` | DOUBLE PRECISION | Temperatura de célula |
| `power_consumption_kw` | DOUBLE PRECISION | Consumo industrial simulado |
| `temp_celsius` | DOUBLE PRECISION | Temperatura ambiente |
| `humidity_pct` | DOUBLE PRECISION | Humedad % |
| `clouds_pct` | DOUBLE PRECISION | Nubosidad % |
| `rain_prob_norm` | DOUBLE PRECISION | Probabilidad de lluvia (0–1) |
| `wind_speed_mps` | DOUBLE PRECISION | Velocidad de viento m/s |
| `weather_id` | INTEGER | FK → dim_weather |
| `price_pvpc_eur_mwh` | DOUBLE PRECISION | Precio PVPC. `null` para D+2 en adelante (ESIOS solo publica D+1) |
| `_loaded_at_utc` | TIMESTAMPTZ | Timestamp de carga |

**Índices**: `idx_gold_fcast_unix_time` sobre `unix_time`, `idx_gold_fcast_weather_id` sobre `weather_id`.

---

## Schema ETL — Auditoría

### `etl.etl_metadata`

Registro de cada ejecución del pipeline. Se inserta al finalizar `run.py`, tanto en éxito como en fallo.

| Campo | Tipo SQL | Descripción |
|-------|----------|-------------|
| `id` | INTEGER PK | Autoincremental |
| `pipeline_name` | TEXT | Siempre `SunSaver_ETL` |
| `status` | TEXT | `SUCCESS`, `PARTIAL SUCCESS`, `FAILED AT STAGE N`, `CRITICAL ERROR` |
| `duration_seconds` | FLOAT | Duración total del pipeline en segundos |
| `rows_affected` | INTEGER | Total de filas procesadas sumando todos los stages |
| `error_message` | TEXT | Resumen de fallos (`null` en ejecuciones limpias) |
| `env` | TEXT | `PRD` en Fargate, `DEV` en local (variable `ENVIRONMENT`) |
| `_executed_by` | TEXT | Hostname del contenedor o máquina que ejecutó |
| `_executed_at` | DATETIME | Timestamp de ejecución (UTC) |

---

## Lineaje completo de datos

```
Excel clients_source.xlsx  (S3: inputs/)
  └── bronze/clients/*.json
        └── silver.clean_clients
              ├── gold.dim_client
              └── (coordenadas GPS) → bronze/weather/*.json
                                          └── silver.clean_weather
                                                ├── gold.dim_weather
                                                ├── gold.dim_datetime (unix_times)
                                                └── silver.clean_calculations (input motor PV)

Excel clients_source.xlsx  (hoja: assets)
  └── bronze/assets/*.json
        └── silver.clean_assets
              └── gold.dim_assets

ESIOS API (precios PVPC)
  └── bronze/prices/*.json
        └── silver.clean_prices
              ├── gold.dim_datetime (unix_times)
              ├── gold.fact_energy_forecast.price_pvpc_eur_mwh
              └── gold.fact_energy_historical.national_price_pvpc_eur_mwh

ESIOS API (indicadores D−1: demanda, FV, CO2, desvíos)
  └── bronze/context/*.json
        └── silver.clean_context
              ├── gold.dim_datetime (unix_times)
              └── gold.fact_energy_historical (pivot inline):
                    national_demand_mw
                    national_pv_gen_mw
                    national_co2_tco2_mwh
                    national_upward_imb_mw

silver.clean_calculations  (motor PV aplicado sobre clients × weather)
  ├── gold.dim_datetime (unix_times)
  ├── gold.fact_energy_historical (pv_gen_kw, pr, poa, t_cell, consumption)
  └── gold.fact_energy_forecast   (pv_power_gen_kw, pr, poa, t_cell, consumption)

run.py (orquestador)
  └── etl.etl_metadata  (status, duración, filas, host, entorno)
```

---

[← Motor PV](02_motor_pv.md) · [Informe operativo →](04_informe_operativo.md)