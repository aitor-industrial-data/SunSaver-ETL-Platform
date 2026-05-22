# ☀️ SunSaver ETL Platform

> Plataforma de previsión de generación solar y optimización energética para industria.  
> Pipeline serverless en AWS que convierte datos de mercado eléctrico y meteorología  
> en un **plan de acción operativo** listo cada mañana antes de que arranque el turno.

<br>

[![AWS Fargate](https://img.shields.io/badge/AWS-Fargate-FF9900?logo=amazonaws&logoColor=white)](https://aws.amazon.com/fargate/)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://python.org)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15-336791?logo=postgresql&logoColor=white)](https://aws.amazon.com/rds/)
[![pvlib](https://img.shields.io/badge/pvlib-0.15-F5A623?logo=python&logoColor=white)](https://pvlib-python.readthedocs.io/)
[![CI/CD](https://img.shields.io/badge/CI/CD-GitHub_Actions_→_ECR_→_ECS-2088FF?logo=githubactions&logoColor=white)](https://github.com/aitor-industrial-data/SunSaver-ETL-Platform/actions)
---

## Ver el informe en vivo

El resultado tangible del pipeline: un **plan de accción diario** con decisiones concretas para la planta.

<div>

<a href="https://sunsaver-bronze.s3.eu-south-2.amazonaws.com/reports/latest.html">
  <img src="https://img.shields.io/badge/🔗_Ver estrategia energética de hoy_→-A2E865?style=for-the-badge" alt="Ver informe live" height="48">
</a>

*Actualizado cada noche tras la ejecución del pipeline (~21:05 h)*

</div>

---

## El problema que resuelve

Una instalación fotovoltaica industrial genera datos que por sí solos no sirven para tomar decisiones. El precio de la electricidad cambia cada hora, la generación solar depende de la posición exacta del sol y de la temperatura real del panel, y el responsable de planta necesita saber **qué hacer mañana a las 7h**, no leer tablas en crudo.

**SunSaver cierra ese gap.** Cada noche a las 21h —cuando ESIOS publica los precios PVPC del día siguiente— el pipeline se ejecuta automáticamente y entrega un informe accionable: cuándo cargar las carretillas, cuándo no arrancar compresores, cuándo aprovechar el excedente solar, etc.

```
20:30 h  →  ESIOS publica precios PVPC D+1
21:00 h  →  SunSaver ejecuta pipeline completo  (~3 min en Fargate)
21:05 h  →  Informe HTML publicado en URL fija de S3
06:00 h  →  El jefe de planta abre el informe antes del primer turno
```

---

## Qué hace el sistema
 
```
  ESIOS (precios PVPC) ─────────────────────────────────────────┐
  ESIOS (demanda, FV, CO2, desvíos D−1) ───────────────────────►│
  OpenWeatherMap (forecast 5 días por cliente GPS) ────────────►│  BRONZE  →  SILVER  →  GOLD  →  INFORME HTML
  Excel clientes (parámetros instalación + activos) ───────────►│  (S3)       (RDS)      (RDS)    (S3 público)
                                                                │
                                              AWS Fargate · eu-south-2
```
 
| Capa | Qué ocurre |
|------|-----------|
| **Bronze** | Extracción raw de cuatro fuentes. JSON sin transformar en S3, con manifest de estado por fuente. |
| **Silver** | Limpieza, normalización, interpolación meteorológica y cálculo físico de generación PV hora a hora. |
| **Gold** | Star schema relacional: 4 dimensiones + 2 facts (histórico acumulativo + previsión futura). |
| **Output** | Informe HTML diario con KPIs, decisiones operativas por activo y previsión a 5 días. |
 
---
 
## El motor de cálculo solar
 
El núcleo diferencial del proyecto. No usa estimaciones genéricas: calcula la generación real hora a hora aplicando física de paneles solares, independientemente para las coordenadas GPS exactas de cada cliente.
 
```
Coordenadas GPS + hora UTC
        │
        ├─► Posición solar exacta (pvlib NREL SPA)  →  elevación α, azimut
        ├─► Irradiancia GHI  (Haurwitz + Kasten-Czeplak + factor meteoro OWM)
        ├─► Descomposición Erbs  →  DNI (directa) + DHI (difusa)
        ├─► Irradiancia POA  (beam + Liu-Jordan diffuse + albedo suelo)
        ├─► Temperatura de célula  (modelo Faiman con enfriamiento por viento)
        └─► Potencia AC  (derating térmico γ=−0.4%/°C + pérdidas de sistema)
```
 
```python
# engine_pv_physics.py — cadena de cálculo real, cada función independiente y testeable
alfa, azimuth = calculate_solar_position(lat, lon, forecast_time_utc)
ghi           = calculate_ghi(alfa, clouds_pct, weather_id)      # Haurwitz + nubes + meteoro
dni, dhi      = decompose_erbs(ghi, alfa, forecast_time_utc)     # Índice de claridad kt
poa           = calculate_total_poa(dni, dhi, ghi, alfa, azimuth, angle, aspect)
t_cell        = calculate_t_cell(temp_ambient, wind_speed, poa)  # Faiman U0=24.9, U1=6.1
p_gen, pr     = calculate_power_output(poa, t_cell, peak_kw, loss_pct)
```
 
> 📄 Física completa, ecuaciones y parámetros → [`docs/02_motor_pv.md`](docs/02_motor_pv.md)
 
---
 
## Arquitectura AWS
 
```
┌──────── GitHub Actions (OIDC — sin claves AWS hardcodeadas) ─────────────┐
│  push → main  ──►  docker build  ──►  ECR (:SHA + :latest)               │
│                          └──►  register-task-definition (nueva revisión) │
└───────────────────────────────────┬──────────────────────────────────────┘
                                    │
                    EventBridge Scheduler  cron(05 19 * * ? *)  [= 21:05 CET]
                                    │
                                    ▼
                    ECS Fargate  (1 vCPU · 2 GB · awsvpc · ~3 min)
                         │              │                │
                    S3 Bucket      RDS PostgreSQL    SSM Parameter Store
                  bronze/ + HTML   silver/gold/etl   7 secretos en PRD
                  (latest público)  star schema       (DB + API keys)
                         │
                    CloudWatch Logs  /ecs/sunsaver-etl
```
 
| Servicio | Rol en el sistema |
|----------|------------------|
| **ECS Fargate** | Contenedor efímero: arranca, ejecuta el pipeline, muere. Sin instancias permanentes. |
| **ECR** | Imagen Docker con doble tag SHA+latest. Trazabilidad commit → ejecución. |
| **EventBridge Scheduler** | Cron gestionado. Lanza `ECS RunTask` directo, sin Lambda. |
| **S3** | Bronze raw + informe HTML. Solo `reports/latest.html` es público (ACL). |
| **RDS PostgreSQL 15** | Schemas `silver` + `gold` + `etl`. SQL estándar para analistas. `sslmode=require`. |
| **SSM Parameter Store** | 7 parámetros en `/sunsaver/prd/`. Nunca en código ni `.env` en producción. |
| **CloudWatch Logs** | Logs estructurados por módulo. Formato fijo para facilitar filtrado. |
| **GitHub Actions + OIDC** | Federación de identidad. El rol IAM se asume con token OIDC, sin `AWS_ACCESS_KEY_ID`. |
 
> 📄 Arquitectura detallada, decisiones de diseño y manifests → [`docs/01_arquitectura.md`](docs/01_arquitectura.md)
 
---
 
## Plataforma multicliente
 
El sistema está diseñado para múltiples instalaciones desde el inicio. Cada cliente se define en un Excel interno con sus parámetros específicos. Añadir un cliente nuevo es añadir una fila.
 
```
clients_source.xlsx
├── hoja "Clients Data"   →  parámetros de instalación por cliente
│     CLT-0001  lat:42.80  lon:-1.70  peak:16kW  angle:30°  aspect:180°  tz:Europe/Madrid
│     CLT-0002  lat:41.38  lon: 2.17  peak:48kW  angle:25°  aspect:200°  ...
│
└── hoja "assets"         →  activos industriales por cliente
      forklift_battery · compressor · cold_storage · pump · autoclave · lighting
```
 
El motor físico calcula independientemente para cada coordenada: el sol no está en el mismo ángulo en Navarra que en Barcelona a la misma hora, y la temperatura del panel en una instalación ventosa es diferente aunque la irradiancia sea idéntica.
 
---
 
## Output: el informe operativo
 
**[→ Ver informe live](https://sunsaver-bronze.s3.eu-south-2.amazonaws.com/reports/latest.html)**
 
El informe generado cada noche contiene:
 
- **KPIs del día** — pico FV previsto con hora, precio PVPC mínimo y máximo con hora, horas de generación activa, horas de precio bajo y alto
- **Plan de acción** — decisiones concretas por activo ordenadas por urgencia (`critical` → `high` → `medium` → `low`): carretillas, compresores, cámara frigorífica, bombas, autoclaves
- **Gráficos horarios** — curva de generación FV (área) y precios PVPC hora a hora (barras en semáforo: verde/ámbar/rojo según umbral)
- **Previsión 5 días** — outlook semanal por día con emoji meteorológico, pico FV previsto, temperatura y fiabilidad indicada (sin PVPC disponible más allá de D+1)
> 📄 Motor de reglas de decisiones y lógica del informe → [`docs/04_informe_operativo.md`](docs/04_informe_operativo.md)
 
---
 
## Modelo de datos
 
Base de datos PostgreSQL en RDS con star schema preparado para analistas de datos:
 
```
gold.dim_client     →  Instalación: GPS, potencia pico, pérdidas, batería, flags has_solar/has_battery
gold.dim_assets     →  Activos industriales: tipo, potencia, flexibilidad, flags has_capacity/is_overnight_flexible
gold.dim_weather    →  Catálogo de condiciones meteorológicas OWM (deduplicado por frecuencia)
gold.dim_datetime   →  Dimensión temporal enriquecida: períodos tarifarios P1/P2/P3/P6, festivos, hora local Madrid
 
gold.fact_energy_historical  →  Serie histórica acumulativa (upsert incremental)
                                 Incluye contexto del sistema eléctrico peninsular:
                                 demanda real, FV total, CO2, desvío a subir (ESIOS D−1)
gold.fact_energy_forecast    →  Ventana futura activa (TRUNCATE + INSERT diario)
                                 precio_pvpc null para D+2 en adelante
 
etl.etl_metadata             →  Auditoría de ejecuciones: status, duración, filas, hostname, entorno
```
 
> 📄 Esquema completo, tipos SQL exactos y lineaje campo a campo → [`docs/03_modelo_datos.md`](docs/03_modelo_datos.md)
 
---
 
## Stack técnico
 
| Categoría | Tecnología |
|-----------|-----------|
| Lenguaje | Python 3.12 |
| Motor solar | `pvlib` 0.15, `numpy` 2.4 |
| Datos | `pandas` 3.0, `SQLAlchemy` 2.0, `psycopg2-binary` |
| APIs externas | `requests` + ESIOS/REE (pública + ESIOS_API_KEY) + OpenWeatherMap |
| Base de datos | PostgreSQL 15 en AWS RDS (`sslmode=require`) |
| Infraestructura | AWS Fargate · S3 · RDS · ECR · EventBridge · SSM · CloudWatch |
| CI/CD | GitHub Actions → ECR → ECS (OIDC, sin claves hardcodeadas) |
| Contenedor | Docker `python:3.12-slim`, `gcc` + `libpq-dev` para psycopg2 |
| Secretos PRD | AWS SSM Parameter Store (7 parámetros) |
 
---
 
## Estructura del repositorio
 
```
SunSaver-ETL-Platform/
│
├── src/
│   ├── engine_pv_physics.py           # Motor de cálculo solar (núcleo del sistema)
│   ├── run.py                         # Orquestador — 8 stages, auditoría, CLI
│   │
│   ├── bronze_ingest_clients.py       # Excel S3 hoja "Clients Data" → Bronze
│   ├── bronze_ingest_assets.py        # Excel S3 hoja "assets" → Bronze
│   ├── bronze_ingest_prices_ree.py    # ESIOS precios PVPC → Bronze
│   ├── bronze_ingest_context.py       # ESIOS indicadores D−1 (4 indicadores) → Bronze
│   ├── bronze_ingest_weather_owm.py   # OpenWeatherMap por cliente GPS → Bronze
│   │
│   ├── silver_transform_clients.py    # Validación + normalización clientes
│   ├── silver_transform_assets.py     # Validación tipos de activo industrial
│   ├── silver_transform_prices.py     # Precios hora a hora, interpolación lineal
│   ├── silver_transform_context.py    # Pivot indicadores ESIOS, validación por rangos
│   ├── silver_transform_weather.py    # OWM 3h → interpolación 1h
│   ├── silver_calc_pv_generation.py   # Aplica motor PV sobre clients × weather
│   │
│   ├── gold_dim_clients.py            # Dim cliente con flags has_solar / has_battery
│   ├── gold_dim_assets.py             # Dim activos con flags has_capacity / is_overnight_flexible
│   ├── gold_dim_datetime.py           # Dim temporal: períodos tarifarios, festivos, hora local
│   ├── gold_dim_weather.py            # Dim meteorológica deduplicada por frecuencia
│   ├── gold_fact_energy_historical.py # Upsert histórico + enriquecimiento ESIOS D−1
│   ├── gold_fact_energy_forecast.py   # TRUNCATE + INSERT ventana futura
│   │
│   ├── gold_fact_energy_decisions.py  # Motor de reglas → decisiones operativas por activo
│   ├── report_generator.py            # Renderiza HTML + publica S3 (histórico + latest)
│   │
│   ├── config_paths.py                # Rutas S3, helpers boto3, configuración entorno
│   ├── database_utils.py              # Engine SQLAlchemy PostgreSQL (DEV/PRD transparente)
│   ├── audit_metadata.py              # Registro ejecución → etl.etl_metadata
│   └── logger_config.py               # stdout → CloudWatch (PRD) + archivo (DEV)
│
├── docs/
│   ├── 01_arquitectura.md             # Pipeline detallado, AWS, manifests, decisiones
│   ├── 02_motor_pv.md                 # Física solar, ecuaciones, modelos aplicados
│   ├── 03_modelo_datos.md             # Star schema, tipos SQL exactos, lineaje completo
│   └── 04_informe_operativo.md        # Motor de decisiones, reglas por activo, output HTML
│
├── .github/workflows/deploy.yml       # CI/CD: build → ECR → register task definition
├── task-definition.json               # ECS Fargate task definition (SSM secrets, CloudWatch)
├── Dockerfile                         # python:3.12-slim, gcc + libpq-dev
└── requirements.txt                   # Dependencias pinadas
```
 
---
 
## Documentación técnica
 
| Documento | Contenido |
|-----------|-----------|
| [01 · Arquitectura](docs/01_arquitectura.md) | Pipeline 8 stages, AWS servicio a servicio, manifests, gestión de secretos, decisiones de diseño |
| [02 · Motor PV](docs/02_motor_pv.md) | Cadena de cálculo física: Haurwitz, Erbs, POA, Faiman, derating térmico |
| [03 · Modelo de datos](docs/03_modelo_datos.md) | 5 tablas Silver + 4 dims + 2 facts + auditoría, tipos SQL exactos, lineaje campo a campo |
| [04 · Informe operativo](docs/04_informe_operativo.md) | Motor de reglas por tipo de activo, KPIs, outlook semanal, publicación S3 |
| [05 · CI/CD y despliegue](docs/05_ci_cd_despliegue.md) | Workflow GitHub Actions, OIDC, Dockerfile, rollback, despliegue manual |
| [06 · Operaciones](docs/06_operaciones.md) | Monitorización CloudWatch, añadir clientes, troubleshooting por fuente, SQL analistas |
 
---
 
<sub>Desarrollado con Python · Desplegado en AWS eu-south-2 (España) · Ejecución automática diaria a las 21h CET</sub>