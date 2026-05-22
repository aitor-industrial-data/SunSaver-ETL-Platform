# 01 · Arquitectura del sistema

[← Volver al README](../README.md)

---

## ¿Por qué esta arquitectura?

SunSaver necesita integrar cuatro fuentes externas de naturaleza distinta (mercado eléctrico D+1, contexto del sistema eléctrico D−1, meteorología por coordenadas GPS, datos maestros de cliente), ejecutar un motor de cálculo físico intensivo, mantener histórico analítico y publicar un resultado operativo cada día. La arquitectura medallion sobre AWS Fargate responde a esas necesidades con el mínimo coste operativo: **no hay servidores que mantener, no hay nada que escalar manualmente, y el coste es proporcional al uso real** (una ejecución diaria de ~3 minutos).

---

## Pipeline: 8 stages secuenciales

El orquestador `run.py` ejecuta el pipeline en stages numerados. Si **todos** los pasos de un stage fallan, el pipeline se detiene. Los errores parciales permiten continuar; el resultado se registra como `PARTIAL SUCCESS` en la tabla de auditoría.

```
STAGE 1  ─── Bronze Ingest (fuentes estáticas + mercado)
              ├── extract_clients          Excel S3 (hoja: Clients Data) → JSON Bronze
              ├── extract_assets           Excel S3 (hoja: assets)       → JSON Bronze
              ├── extract_energy_prices    ESIOS API (PVPC D−1/D0/D+1)   → JSON Bronze
              └── extract_system_context   ESIOS API (4 indicadores D−1) → JSON Bronze

STAGE 2  ─── Silver Transform (independiente de weather)
              ├── transform_clients        Validación + normalización  → silver.clean_clients
              ├── transform_assets         Validación tipos activo     → silver.clean_assets
              ├── transform_energy_prices  Precios hora a hora         → silver.clean_prices
              └── transform_context        Pivot indicadores ESIOS     → silver.clean_context

STAGE 3  ─── Bronze Weather
              └── extract_openweather     OWM API por cliente (usa coordenadas GPS
                                          ya validadas de silver.clean_clients)
                                          → JSON Bronze por cliente

STAGE 4  ─── Silver Weather
              └── transform_openweather   OWM 3h → interpolación 1h → silver.clean_weather

STAGE 5  ─── Silver Calculation
              └── extract_generation_data JOIN clients × weather → motor PV
                                          → silver.clean_calculations

STAGE 6  ─── Gold Dimensions (paralelas lógicamente, secuenciales en ejecución)
              ├── gold_dim_clients         silver.clean_clients → gold.dim_client
              ├── gold_dim_datetime        UNION unix_times Silver → gold.dim_datetime
              ├── gold_dim_weather         silver.clean_weather → gold.dim_weather
              └── gold_dim_assets          silver.clean_assets → gold.dim_assets

STAGE 7  ─── Gold Facts (orden crítico: historical ANTES que forecast)
              ├── gold_fact_energy_historical   Upsert filas pasadas desde forecast
              │                                  + enriquecimiento con clean_context
              └── gold_fact_energy_forecast     TRUNCATE + INSERT filas futuras

STAGE 8  ─── Output
              ├── gold_fact_energy_decisions    Motor de reglas → dict de decisiones
              └── generate_report               HTML → S3 (histórico + latest.html)
```

**Por qué weather va en stage 3 y no en stage 1**: OWM se llama una vez por cliente usando las coordenadas GPS ya validadas de `silver.clean_clients`. Si un cliente tiene coordenadas inválidas, se descarta en Silver antes de gastar una llamada a la API meteorológica.

**Por qué historical antes que forecast**: `fact_energy_historical` lee de `fact_energy_forecast` las filas con `unix_time < now()`. Si forecast se trunca primero, se pierden los datos de ayer.

---

## Fuentes de datos externas

| Fuente | API | Qué se extrae | Ventana temporal |
|--------|-----|---------------|-----------------|
| **ESIOS / REE** | `apidatos.ree.es` (pública, sin key) | Precios PVPC hora a hora | D−1, D0, D+1 (D+1 disponible tras 20:30 CET) |
| **ESIOS / REE** | `api.esios.ree.es` (requiere `ESIOS_API_KEY`) | Demanda real, FV peninsular, CO2, desvío a subir | D−1 consolidado |
| **OpenWeatherMap** | `api.openweathermap.org/data/2.5/forecast` | Forecast meteorológico por coordenadas GPS | 5 días, resolución 3h |
| **Excel clientes** | S3 `inputs/clients_source.xlsx` | Parámetros de instalación + activos industriales | Estático, se actualiza manualmente |

---

## Arquitectura AWS

```
┌─── GitHub ─────────────────────────────────────────────────────────────────┐
│  push → main                                                               │
│    │                                                                       │
│    ▼  GitHub Actions (OIDC — sin claves AWS en secretos)                   │
│    1. docker build  (python:3.12-slim)                                     │
│    2. push → ECR  (:SHA del commit + :latest)                              │
│    3. render-task-definition  (inyecta SHA exacto en el JSON)              │
│    4. register-task-definition  (nueva revisión en ECS)                    │
│       EventBridge apunta a la familia → coge esta revisión automáticamente │
└────────────────────────────────────────────────────────────────────────────┘
                              │
                              │  EventBridge Scheduler
                              │  cron(05 19 * * ? *)  [UTC = 21:05 CET]
                              ▼
┌─── AWS eu-south-2 (España) ───────────────────────────────────────────────┐
│                                                                           │
│   ECS Fargate Task                                                        │
│   sunsaver-etl:latest  |  1 vCPU · 2 GB  |  awsvpc  |  ~3 min             │
│      │                                                                    │
│      ├──► SSM Parameter Store  (al arrancar el contenedor)                │
│      │    /sunsaver/prd/DB_USER      /sunsaver/prd/DB_PASS                │
│      │    /sunsaver/prd/DB_HOST      /sunsaver/prd/DB_NAME                │
│      │    /sunsaver/prd/DB_PORT      /sunsaver/prd/ESIOS_API_KEY          │
│      │    /sunsaver/prd/WEATHER_API_KEY                                   │
│      │    (las credenciales nunca están en el código ni en texto plano)   │
│      │                                                                    │
│      ├──► S3  sunsaver-bronze                                             │
│      │    ├── bronze/prices/          raw ESIOS precios JSON              │
│      │    ├── bronze/context/         raw ESIOS indicadores JSON          │
│      │    ├── bronze/weather/         raw OWM JSON por cliente            │
│      │    ├── bronze/clients/         raw Excel clientes JSON             │
│      │    ├── bronze/assets/          raw Excel activos JSON              │
│      │    ├── bronze/manifests/       estado de ingesta por fuente        │
│      │    ├── inputs/clients_source.xlsx   fuente maestra de clientes     │
│      │    └── reports/                                                    │
│      │         ├── 2026-05-23/CLT-0001_energy_report.html  (histórico)    │
│      │         └── latest.html  ← URL pública fija (ACL pública)          │
│      │                                                                    │
│      ├──► RDS PostgreSQL 15  (sslmode=require)                            │
│      │    ├── schema: silver   datos curados (se reconstruye cada día)    │
│      │    ├── schema: gold     star schema   (histórico acumulativo)      │
│      │    └── schema: etl      auditoría de ejecuciones                   │
│      │                                                                    │
│      └──► CloudWatch Logs  /ecs/sunsaver-etl                              │
│           Formato: timestamp | level | módulo | mensaje                   │
│           En DEV: también archivo local logs/sunsaver_YYYY-MM-DD.log      │
└───────────────────────────────────────────────────────────────────────────┘
```

---

## Servicio por servicio

**ECS Fargate** — El pipeline corre en un contenedor efímero. Sin instancias permanentes: arranca, ejecuta, muere. Task definition: `1024` CPU units (1 vCPU), `2048` MB RAM, networking `awsvpc` (IP propia por tarea), imagen `sunsaver-etl:latest` desde ECR `eu-south-2`.

**ECR (Elastic Container Registry)** — Cada push a `main` genera una imagen con doble tag: el SHA del commit (historial inmutable) y `latest` (lo que Fargate ejecuta en la próxima invocación). El step `render-task-definition` inyecta el SHA exacto en el JSON antes de registrar la revisión, garantizando trazabilidad commit→ejecución.

**EventBridge Scheduler** — Cron `05 19 * * ? *` UTC = 21:05 hora Madrid. Lanza `ECS RunTask` directamente sobre la familia `sunsaver-etl-task` sin número de revisión → coge siempre la última registrada por el CI/CD. Sin Lambda intermediaria.

**S3 `sunsaver-bronze`** — Doble función: almacén Bronze particionado por fuente (JSON sin transformar, trazabilidad completa) y servidor del informe HTML. Solo `reports/latest.html` tiene ACL pública; el resto del bucket es privado. El informe histórico fechado sirve para auditoría.

**RDS PostgreSQL 15** — Tres schemas: `silver` (datos curados, se reconstruye en cada ejecución), `gold` (star schema, el histórico se acumula, el forecast se sobreescribe), `etl` (auditoría de ejecuciones). Conexión con `sslmode=require`. Accesible directamente con cualquier cliente SQL estándar para analistas.

**SSM Parameter Store** — Siete secretos en producción: credenciales de base de datos (5 parámetros) y API keys de ESIOS y OpenWeatherMap. La task definition los referencia por ARN completo en la sección `secrets`; ECS los inyecta como variables de entorno en tiempo de arranque. En desarrollo local los lee `.env` con `python-dotenv`. El código es idéntico en ambos entornos.

**CloudWatch Logs** — Grupo `/ecs/sunsaver-etl`, prefijo `ecs`. Se crea automáticamente (`awslogs-create-group: true`). Formato estructurado: `timestamp | level | módulo (30 chars) | mensaje`. En local (`ENVIRONMENT=DEV`) se añade además un FileHandler a `logs/sunsaver_YYYY-MM-DD.log`.

**GitHub Actions con OIDC** — El workflow asume el rol `github-sunsaver-ecr-role` (ARN: `arn:aws:iam::610140802215:role/github-sunsaver-ecr-role`) mediante federación de identidad OIDC. No existe ningún `AWS_ACCESS_KEY_ID` en los secretos del repositorio. El token OIDC de GitHub es suficiente para autenticar y asumir el rol IAM.

---

## Gestión de secretos: dos entornos, un solo código

```
LOCAL (DEV)                          FARGATE (PRD)
───────────────                      ──────────────────────────────
.env file                            task-definition.json → "secrets"
  DB_USER=...                          { "name": "DB_USER",
  DB_PASS=...                            "valueFrom": "arn:aws:ssm:..." }
  ESIOS_API_KEY=...                  ECS inyecta SSM → variable de entorno
  WEATHER_API_KEY=...
        │                                      │
        └──────────────┬───────────────────────┘
                       ▼
              os.getenv("DB_USER")   ← mismo código, mismo comportamiento
```

---

## Decisiones de diseño

### Medallion Architecture (Bronze / Silver / Gold)

**Por qué**: las cuatro fuentes tienen naturaleza y ritmo distintos. Bronze preserva el dato original para auditoría y reproceso sin necesidad de llamar de nuevo a la API. Silver homogeneiza tipos, zonas horarias y reglas de negocio. Gold es el contrato estable que consumen los analistas y el motor de decisiones.

**Alternativa descartada**: procesar directamente de API a Gold en un solo paso. Descartada porque impide reprocesar histórico sin coste de API y mezcla responsabilidades de extracción, transformación y modelado.

### Manifests por fuente en S3

Cada fuente Bronze tiene su propio manifest JSON en `bronze/manifests/`. El manifest registra el estado (`pending`, `success`, `error`) de cada archivo ingestado. El step Silver lee solo los archivos `pending` o `error`, lo que permite:
- Reprocesar selectivamente sin re-extraer de la API
- Auditar qué archivos se han procesado y cuándo
- Detectar fallos parciales (un cliente de OWM falla, el resto continúan)

### TRUNCATE + INSERT en `fact_energy_forecast`

Cada ejecución sobreescribe completamente la ventana futura. La previsión meteorológica mejora conforme se acerca la fecha: el dato más reciente es siempre el más fiable. El histórico vive en `fact_energy_historical` y nunca se toca.

### Upsert idempotente en Silver y `fact_energy_historical`

Todos los steps de Silver y el histórico usan `ON CONFLICT DO UPDATE`. Relanzar el pipeline para el mismo periodo es seguro y no genera duplicados. Esto permite recuperarse de fallos parciales sin limpiar datos.

### Auditoría en `etl.etl_metadata`

`run.py` acumula filas procesadas por stage y al finalizar persiste un registro en `etl.etl_metadata` con status, duración, total de filas y hostname del contenedor. Permite monitorizar tendencias de rendimiento y detectar degradación sin revisar logs.

---

[← README](../README.md) · [Motor PV →](02_motor_pv.md)