# 03 · Modelo de datos

[← README](../README.md)

---

## Visión general

La base de datos en RDS PostgreSQL organiza los datos en dos schemas. `silver` contiene los datos curados listos para el cálculo. `gold` contiene el star schema relacional que consumen los analistas de datos y el motor de decisiones.

```
silver.clean_clients        →   gold.dim_client
silver.clean_assets         →   gold.dim_assets
silver.clean_weather        →   gold.dim_weather
                            →   gold.dim_datetime
silver.clean_calculations   →   gold.fact_energy_historical
silver.clean_prices         →   gold.fact_energy_forecast
```

---

## Schema Silver — Datos curados

### `silver.clean_clients`

Parámetros físicos y de negocio de cada instalación. Se reconstruye completa en cada ejecución (DROP + CREATE + INSERT). La deduplicación por `client_id` mantiene solo el registro más reciente si hay múltiples ingestas.

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `client_id` | TEXT PK | Identificador único (ej: CLT-0001) |
| `name` | TEXT | Nombre de la instalación (normalizado UPPER) |
| `latitude` | REAL | Coordenada GPS — 6 decimales |
| `longitude` | REAL | Coordenada GPS — 6 decimales |
| `pv_peak_power_kw` | REAL | Potencia pico instalada en STC |
| `nominal_load_kw` | REAL | Carga nominal industrial del cliente |
| `panel_area_m2` | REAL | Superficie total de panel |
| `efficiency` | REAL | Eficiencia del panel (0–1) |
| `loss_pct` | REAL | Pérdidas de sistema % (inversor, cableado…) |
| `angle` | REAL | Inclinación del panel (0–90°) |
| `aspect` | REAL | Orientación (1–360°, 180 = sur) |
| `battery_capacity_kwh` | REAL | Capacidad de batería (0 si no tiene) |
| `soc_min_pct` | REAL | Estado de carga mínimo permitido % |
| `timezone` | TEXT | Zona horaria local (ej: Europe/Madrid) |
| `_ingested_at_utc` | TIMESTAMP | Timestamp de ingesta Bronze |

**Validaciones aplicadas**: coordenadas en rango físico, `angle` 0–90 (default 30), `aspect` 1–360 (default 180), `loss_pct` 0–90 (default 14), `pv_peak_power_kw > 0`. Valores fuera de rango se corrigen al default, no se descartan.

### `silver.clean_assets`

Activos industriales desplazables o monitorizados por cliente.

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `asset_id` | TEXT PK | Identificador único del activo |
| `client_id` | TEXT | FK → clean_clients |
| `asset_name` | TEXT | Nombre descriptivo |
| `asset_type` | TEXT | Tipo: `forklift_battery`, `compressor`, `cold_storage`, `pump`, `autoclave`, `lighting`, `other` |
| `power_kw` | REAL | Potencia nominal del activo |
| `capacity_kwh` | REAL | Capacidad (baterías y almacenamiento frío) |
| `is_flexible` | INTEGER | 1 si el activo puede desplazarse en el tiempo |
| `flex_window_start` | INTEGER | Inicio de ventana de flexibilidad (hora local) |
| `flex_window_end` | INTEGER | Fin de ventana de flexibilidad (hora local) |
| `priority` | INTEGER | Prioridad de despacho (1 = más prioritario) |
| `notes` | TEXT | Observaciones del operario |

### `silver.clean_weather`

Forecast meteorológico por cliente y hora (5 días, resolución 3h de OWM interpolada).

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `client_id` | TEXT | FK → clean_clients |
| `unix_time` | BIGINT PK | Timestamp UNIX UTC |
| `forecast_time_utc` | TIMESTAMPTZ | Timestamp legible UTC |
| `temp_celsius` | DOUBLE | Temperatura ambiente |
| `humidity_pct` | DOUBLE | Humedad relativa % |
| `clouds_pct` | DOUBLE | Cobertura nubosa % (0–100) |
| `rain_prob_norm` | DOUBLE | Probabilidad de lluvia normalizada (0–1) |
| `wind_speed_mps` | DOUBLE | Velocidad de viento m/s |
| `weather_id` | INTEGER | Código OpenWeatherMap (8xx=claro, 2xx=tormenta…) |
| `weather_main` | TEXT | Categoría principal OWM |
| `is_daylight` | BOOLEAN | True si hay luz solar en ese slot |

### `silver.clean_calculations`

Resultados del motor PV por cliente y hora. Upsert en cada ejecución.

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `client_id` | TEXT | FK → clean_clients |
| `unix_time` | BIGINT PK | Timestamp UNIX UTC |
| `forecast_time_utc` | TIMESTAMPTZ | Timestamp UTC |
| `pv_power_gen_kw` | DOUBLE | Generación FV calculada |
| `pv_performance_ratio` | DOUBLE | PR efectivo (incluye derating térmico) |
| `poa_wm2` | DOUBLE | Irradiancia sobre el plano del panel |
| `t_cell_celsius` | DOUBLE | Temperatura de célula Faiman |
| `power_con_kw` | DOUBLE | Consumo industrial simulado |

---

## Schema Gold — Star Schema

### Dimensiones

#### `gold.dim_client`

Tabla de dimensión de clientes. Refleja `silver.clean_clients` con campos adicionales derivados.

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `client_id` | TEXT PK | Identificador del cliente |
| `name` | TEXT | Nombre normalizado |
| `description` | TEXT | Descripción de la instalación |
| `nominal_load_kw` | REAL | Carga nominal industrial |
| `pv_peak_power_kw` | REAL | Potencia FV pico instalada |
| `has_solar` | BOOLEAN | True si tiene instalación FV activa |
| `has_battery` | BOOLEAN | True si tiene almacenamiento |
| `timezone` | TEXT | Zona horaria local del cliente |

#### `gold.dim_assets`

Activos industriales con campos de negocio enriquecidos.

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `asset_id` | TEXT PK | Identificador del activo |
| `client_id` | TEXT | FK → dim_client |
| `asset_name` | TEXT | Nombre |
| `asset_type` | TEXT | Tipo de activo (enum validado) |
| `power_kw` | REAL | Potencia nominal |
| `is_flexible` | BOOLEAN | Desplazable en el tiempo |
| `flex_window_start` | INTEGER | Inicio ventana (hora local) |
| `flex_window_end` | INTEGER | Fin ventana (hora local) |
| `has_capacity` | BOOLEAN | True si tiene capacidad de almacenamiento |
| `is_overnight_flexible` | BOOLEAN | True si puede cargarse de noche |
| `priority` | INTEGER | Orden de despacho |

#### `gold.dim_weather`

Condiciones meteorológicas horarias por cliente.

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `client_id` | TEXT | FK → dim_client |
| `unix_time` | BIGINT PK | Timestamp UNIX |
| `temp_celsius` | DOUBLE | Temperatura |
| `humidity_pct` | DOUBLE | Humedad |
| `clouds_pct` | DOUBLE | Nubosidad |
| `wind_speed_mps` | DOUBLE | Viento |
| `weather_id` | INTEGER | Código OWM |

#### `gold.dim_datetime`

Dimensión temporal estándar para análisis.

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `unix_time` | BIGINT PK | Timestamp UNIX |
| `datetime_utc` | TIMESTAMPTZ | Fecha y hora UTC |
| `year` | INTEGER | Año |
| `month` | INTEGER | Mes (1–12) |
| `day` | INTEGER | Día del mes |
| `hour` | INTEGER | Hora UTC (0–23) |
| `weekday` | INTEGER | Día de semana (0=lunes) |
| `is_weekend` | BOOLEAN | Sábado o domingo |

---

### Tablas de hechos

#### `gold.fact_energy_historical`

Serie histórica completa. **Solo crece**: cada ejecución añade los registros del día calculado con upsert. Nunca se borra histórico.

**Grain**: una fila = un cliente × una hora UTC.

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `client_id` | TEXT PK | FK → dim_client |
| `unix_time` | BIGINT PK | FK → dim_datetime |
| `forecast_time_utc` | TIMESTAMPTZ | Timestamp de la previsión |
| `pv_power_gen_kw` | DOUBLE | Generación FV prevista / real |
| `pv_performance_ratio` | DOUBLE | PR efectivo del sistema |
| `poa_wm2` | DOUBLE | Irradiancia POA |
| `t_cell_celsius` | DOUBLE | Temperatura de célula |
| `power_consumption_kw` | DOUBLE | Consumo industrial |
| `temp_celsius` | DOUBLE | Temperatura ambiente |
| `humidity_pct` | DOUBLE | Humedad |
| `clouds_pct` | DOUBLE | Nubosidad % |
| `wind_speed_mps` | DOUBLE | Velocidad de viento |
| `weather_id` | INTEGER | Código meteorológico OWM |
| `price_pvpc_eur_mwh` | DOUBLE | Precio PVPC ESIOS (null si D+2 o más) |
| `demand_mw` | DOUBLE | Demanda peninsular (contexto de mercado) |
| `renewable_pct` | DOUBLE | % renovable en la red peninsular |
| `_loaded_at_utc` | TIMESTAMPTZ | Timestamp de carga en Gold |

#### `gold.fact_energy_forecast`

Ventana de previsión futura activa. **TRUNCATE + INSERT** en cada ejecución: solo contiene filas con `unix_time >= now()`. El histórico ya está en `fact_energy_historical`; este fact es la vista de lo que viene.

**Grain**: una fila = un cliente × una hora UTC futura.

Mismos campos que `fact_energy_historical` salvo los de contexto peninsular (`demand_mw`, `renewable_pct`), que no tienen sentido en previsión futura.

`price_pvpc_eur_mwh` es `null` para D+2 en adelante (ESIOS solo publica D+1 a las 20:30).

---

## Lineaje de datos

```
ESIOS API
  └── bronze/prices/*.json
        └── silver.clean_prices
              └── gold.fact_energy_historical.price_pvpc_eur_mwh
              └── gold.fact_energy_forecast.price_pvpc_eur_mwh

OpenWeatherMap API
  └── bronze/weather/*.json
        └── silver.clean_weather
              ├── gold.dim_weather
              └── silver.clean_calculations  (input al motor PV)
                    ├── gold.fact_energy_historical
                    └── gold.fact_energy_forecast

Excel clients_source.xlsx
  └── bronze/clients/*.json
        └── silver.clean_clients
              └── gold.dim_client
              └── silver.clean_assets (vía bronze/assets/)
                    └── gold.dim_assets
```

---

[← Motor PV](02_motor_pv.md) · [Informe operativo →](04_informe_operativo.md)