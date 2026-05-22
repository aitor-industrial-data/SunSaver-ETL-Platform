# 01 · Arquitectura del sistema

[← Volver al README](../README.md)

---

## ¿Por qué esta arquitectura?

SunSaver necesita integrar tres fuentes externas de naturaleza distinta (mercado eléctrico, meteorología, datos maestros de cliente), ejecutar un motor de cálculo físico intensivo, mantener histórico analítico y publicar un resultado operativo cada día. La arquitectura medallion sobre AWS Fargate responde a esas necesidades con el mínimo coste operativo: **no hay servidores que mantener, no hay nada que escalar manualmente, y el coste es proporcional al uso real** (una ejecución diaria de ~3 minutos).

---

## Pipeline: 8 stages secuenciales

El orquestador `run.py` ejecuta el pipeline en stages numerados. Si un stage falla completamente, el pipeline se detiene y lo registra. Los errores parciales permiten continuar.

```
STAGE 1  →  Bronze Ingest
             ├── extract_clients          (Excel S3 → JSON Bronze)
             ├── extract_assets           (Excel S3 → JSON Bronze)
             ├── extract_energy_prices    (ESIOS API → JSON Bronze)
             └── extract_system_context   (contexto peninsular REE)

STAGE 2  →  Silver Transform (independiente de weather)
             ├── transform_clients        (validación + normalización)
             ├── transform_assets         (validación tipos de activo)
             ├── transform_energy_prices  (precios hora a hora)
             └── transform_context

STAGE 3  →  Bronze Weather
             └── extract_openweather     (OWM API por cada cliente GPS)

STAGE 4  →  Silver Weather
             └── transform_openweather   (forecast 5 días normalizado)

STAGE 5  →  Silver Calculation
             └── extract_generation_data (motor PV hora × cliente)

STAGE 6  →  Gold Dimensions
             ├── gold_dim_clients
             ├── gold_dim_datetime
             ├── gold_dim_weather
             └── gold_dim_assets

STAGE 7  →  Gold Facts
             ├── gold_fact_energy_historical  (acumula histórico)
             └── gold_fact_energy_forecast    (ventana futura, TRUNCATE + INSERT)

STAGE 8  →  Output
             ├── gold_fact_energy_decisions   (motor de reglas → dict)
             └── generate_report              (HTML → S3 public)
```

El weather se lanza después de transformar clientes (stage 2) porque necesita las coordenadas GPS ya validadas de `silver.clean_clients`.

---

## Arquitectura AWS

### Visión general

```
┌─── GitHub ──────────────────────────────────────────────────┐
│  push → main                                                │
│    │                                                        │
│    ▼  GitHub Actions (OIDC — sin claves AWS)                │
│    1. docker build                                          │
│    2. push → ECR  (:SHA + :latest)                          │
│    3. register-task-definition  (nueva revisión ECS)        │
└─────────────────────────────────────────────────────────────┘
                              │
                              │  EventBridge Scheduler
                              │  cron(05 19 * * ? *)  [UTC = 21:05 Madrid]
                              ▼
┌─── AWS eu-south-2 ──────────────────────────────────────────┐
│                                                             │
│   ECS Fargate Task  (1 vCPU · 2 GB · ~3 min · awsvpc)       │
│   Imagen: ECR sunsaver-etl:latest                           │
│      │                                                      │
│      ├──► S3  sunsaver-bronze                               │
│      │    ├── bronze/prices/          (raw ESIOS JSON)      │
│      │    ├── bronze/weather/         (raw OWM JSON)        │
│      │    ├── bronze/clients/         (raw clientes JSON)   │
│      │    ├── bronze/manifests/       (estado de ingesta)   │
│      │    └── reports/latest.html    ← URL pública fija     │
│      │                                                      │
│      ├──► RDS PostgreSQL 15                                 │
│      │    ├── schema: silver          (datos curados)       │
│      │    └── schema: gold            (star schema)         │
│      │                                                      │
│      └──► SSM Parameter Store                               │
│           /sunsaver/prd/DB_HOST                             │
│           /sunsaver/prd/DB_USER                             │
│           /sunsaver/prd/DB_PASS                             │
│           /sunsaver/prd/ESIOS_API_KEY                       │
│           /sunsaver/prd/WEATHER_API_KEY                     │
│                                                             │
│   CloudWatch Logs  /ecs/sunsaver-etl                        │
└─────────────────────────────────────────────────────────────┘
```

### Servicio por servicio

**ECS Fargate** — El pipeline corre en un contenedor efímero. Sin instancias permanentes: arranca, ejecuta, muere. La task definition especifica `1024 CPU units` (1 vCPU) y `2048 MB`. El networking es `awsvpc` (IP propia por tarea).

**ECR (Elastic Container Registry)** — Cada push a `main` genera una imagen con doble tag: el SHA del commit (historial inmutable) y `latest` (lo que Fargate ejecuta). El CI/CD registra una nueva revisión de task definition apuntando al SHA exacto; EventBridge usa la familia sin número y coge automáticamente la última.

**EventBridge Scheduler** — Cron `05 19 * * ? *` UTC = 21:05 hora Madrid. Lanza `ECS RunTask` directamente, sin Lambda intermediaria. Totalmente gestionado.

**S3 `sunsaver-bronze`** — Doble función: almacén Bronze raw (JSON sin transformar, particionado por fecha/fuente) y servidor del informe HTML. Solo `reports/latest.html` tiene ACL pública; el resto del bucket es privado.

**RDS PostgreSQL** — Dos schemas: `silver` (datos curados, se reconstruye en cada ejecución) y `gold` (star schema, el histórico se acumula, el forecast se sobreescribe). Accesible directamente con cualquier cliente SQL estándar.

**SSM Parameter Store** — Las credenciales de base de datos y las API keys nunca están en el código ni en variables de entorno en claro. La task definition las referencia por ARN; ECS las inyecta en el contenedor en tiempo de arranque.

**CloudWatch Logs** — Cada paso del pipeline emite logs estructurados con nivel, stage y métricas de filas procesadas. El grupo `/ecs/sunsaver-etl` se crea automáticamente (`awslogs-create-group: true`).

**GitHub Actions con OIDC** — El workflow asume el rol `github-sunsaver-ecr-role` mediante federación de identidad. No existe ningún `AWS_ACCESS_KEY_ID` en los secretos del repositorio; el token OIDC de GitHub es suficiente.

---

## Decisiones de diseño

### Medallion Architecture (Bronze / Silver / Gold)

**Por qué**: Las tres fuentes tienen naturaleza y ritmo distintos. Bronze preserva el dato original para auditoría y reproceso sin necesidad de llamar de nuevo a la API. Silver homogeneiza tipos, zonas horarias y reglas de negocio. Gold es el contrato estable que consumen los analistas y el motor de decisiones; si cambia una fuente upstream, solo Silver cambia, Gold permanece estable.

**Alternativa descartada**: procesar directamente de API a Gold en un solo paso. Descartada porque impide reprocesar histórico sin coste de API y mezcla responsabilidades de extracción, transformación y modelado.

### TRUNCATE + INSERT en `fact_energy_forecast`

Cada ejecución sobreescribe completamente la ventana de previsión futura. La previsión meteorológica mejora conforme se acerca la fecha, así que el dato más reciente siempre es el más fiable. El histórico vive en `fact_energy_historical` y nunca se toca.

### Gestión de secretos por SSM, no por `.env`

En producción (Fargate, `ENVIRONMENT=PRD`) las credenciales llegan por SSM vía `secrets` en la task definition. En desarrollo local se usa `.env`. El código no distingue: solo lee variables de entorno.

### Weather se lanza en stage 3, no en stage 1

La API de OpenWeatherMap se llama una vez por cliente, usando las coordenadas GPS ya validadas de `silver.clean_clients`. Si un cliente tiene coordenadas inválidas, se descarta en Silver antes de gastar una llamada a la API meteorológica.

---

[← README](../README.md) · [Motor PV →](02_motor_pv.md)