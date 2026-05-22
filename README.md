# ☀️ SunSaver ETL Platform

> Plataforma de previsión de generación solar y optimización energética para industria.  
> Pipeline serverless en AWS que convierte datos de mercado eléctrico y meteorología  
> en un **plan de acción operativo** listo cada mañana antes de que arranque el turno.

<br>

[![AWS Fargate](https://img.shields.io/badge/AWS-Fargate-FF9900?logo=amazonaws&logoColor=white)](https://aws.amazon.com/fargate/)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://python.org)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-RDS-336791?logo=postgresql&logoColor=white)](https://aws.amazon.com/rds/)
[![Deploy](https://img.shields.io/badge/Deploy-GitHub_Actions_→_ECR_→_ECS-2088FF?logo=githubactions&logoColor=white)](https://github.com/aitor-industrial-data/SunSaver-ETL-Platform/actions)
[![Informe live](https://img.shields.io/badge/Informe_live-Ver_ahora_→-A2E865)](https://sunsaver-bronze.s3.eu-south-2.amazonaws.com/reports/latest.html)

---

## 🚀 Ver el informe en vivo

El resultado tangible del pipeline: un **informe operativo diario** con decisiones concretas para la planta.

<div>

<a href="https://sunsaver-bronze.s3.eu-south-2.amazonaws.com/reports/latest.html">
  <img src="https://img.shields.io/badge/🔗_VER_INFORME_LIVE_AHORA_→-A2E865?style=for-the-badge&logo=googleanalytics&logoColor=white&labelColor=1a1a1a" alt="Ver informe live" height="48">
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
                    ┌──────────────────────────────────────────────┐
  ESIOS / REE ─────►│                                              │
  Precios PVPC      │                                              │
                    │   BRONZE  →  SILVER  →  GOLD  →  INFORME     │
  OpenWeatherMap ──►│   (S3)       (RDS)      (RDS)    (S3 HTML)   │
  Forecast 5 días   │                                              │
                    │                                              │
  Excel clientes ──►│      Pipeline Medallion en AWS Fargate       │
                    └──────────────────────────────────────────────┘
```

| Capa | Qué ocurre |
|------|-----------|
| **Bronze** | Extracción raw de ESIOS, OpenWeatherMap y clientes. JSON sin tocar en S3. |
| **Silver** | Limpieza, normalización y cálculo de generación PV con motor físico propio. |
| **Gold** | Star schema relacional listo para analistas: dims + facts con histórico. |
| **Output** | Informe HTML diario con decisiones operativas y previsión a 5 días. |

---

## El motor de cálculo solar

El núcleo diferencial del proyecto. No usa estimaciones genéricas: calcula la generación real hora a hora aplicando física de paneles solares:

1. **Posición solar** — elevación y azimut exactos para las coordenadas GPS del cliente (`pvlib`)
2. **Irradiancia GHI** — modelo Haurwitz + factor Kasten-Czeplak de nubosidad + coeficiente por tipo de meteoro (tormenta / lluvia / nieve / despejado...)
3. **Descomposición Erbs** — separa irradiancia directa (DNI) y difusa (DHI) por índice de claridad
4. **POA** — irradiancia real sobre el plano del panel según inclinación y orientación del cliente
5. **Temperatura de célula** — modelo Faiman con enfriamiento por velocidad del viento
6. **Potencia AC** — derating térmico (γ = −0.4 %/°C), pérdidas de sistema y Performance Ratio final

```python
# engine_pv_physics.py — cada función es independiente y testeable
alfa, azimuth = calculate_solar_position(lat, lon, forecast_time_utc)
ghi           = calculate_ghi(alfa, clouds_pct, weather_id)
dni, dhi      = decompose_erbs(ghi, alfa, forecast_time_utc)
poa           = calculate_total_poa(dni, dhi, ghi, alfa, azimuth, angle, aspect)
t_cell        = calculate_t_cell(temp_ambient, wind_speed, poa)
p_gen, pr     = calculate_power_output(poa, t_cell, peak_power_kw, loss_pct)
```

> 📄 Documentación completa del motor → [`docs/02_motor_pv.md`](docs/02_motor_pv.md)

---

## Arquitectura AWS

```
┌──────────────────────────────────────────────────────────────────────────┐
│    GitHub Actions                                                        │
│    push → main  ──►  docker build  ──►  ECR (SHA tag + latest)           │
│                          │                                               │
│                          ▼  register-task-definition                     │
└──────────────────────────┼───────────────────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────────────────┐
│    EventBridge Scheduler                                                 │
│    cron(05 19 * * ? *)  →  ECS RunTask  →  Fargate  (1 vCPU / 2 GB)      │
│                                │                                         │
│         ┌──────────────────────┼──────────────────────┐                  │
│         ▼                      ▼                      ▼                  │
│     S3 Bucket            RDS PostgreSQL       SSM Parameter Store        │
│  bronze/  reports/       silver + gold         DB creds, API keys        │
│  (raw JSON + HTML)        star schema          (nunca en código)         │
└──────────────────────────────────────────────────────────────────────────┘
```

| Servicio | Uso | Por qué |
|----------|-----|---------|
| **ECS Fargate** | Ejecuta el pipeline | Serverless: sin instancias que mantener, pago por ejecución |
| **ECR** | Registro de imagen Docker | Integrado con ECS, SHA tag por cada deploy |
| **EventBridge Scheduler** | Dispara el pipeline a las 21h | Cron gestionado, sin cron servers |
| **S3** | Bronze raw + informe HTML público | Almacenamiento ilimitado, coste mínimo |
| **RDS PostgreSQL** | Silver + Gold (star schema) | SQL estándar, directo para analistas |
| **SSM Parameter Store** | Secretos en producción | Las API keys nunca tocan el código ni las variables de entorno en claro |
| **CloudWatch Logs** | Logs del pipeline | Trazabilidad completa de cada ejecución |
| **GitHub Actions + OIDC** | CI/CD sin claves AWS | El workflow asume un rol IAM federado; no hay `AWS_ACCESS_KEY_ID` en ningún secreto |

> 📄 Arquitectura detallada → [`docs/01_arquitectura.md`](docs/01_arquitectura.md)

---

## Plataforma multicliente

El sistema está diseñado para operar con múltiples instalaciones desde el inicio. Cada cliente se define en un Excel interno con sus parámetros específicos —coordenadas GPS, potencia pico, inclinación y orientación del panel, carga nominal, activos desplazables— y el pipeline los procesa a todos en cada ejecución.

```
clients_source.xlsx
│
├── CLT-0001  →  lat: 42.80  lon: -1.70  peak: 16 kW  angle: 30°  aspect: 180°
├── CLT-0002  →  lat: 41.38  lon:  2.17  peak: 48 kW  angle: 25°  aspect: 200°
└── CLT-XXXX  →  añadir cliente = nueva fila en el Excel
```

El motor físico calcula independientemente para cada coordenada: el sol no está en el mismo ángulo en Navarra que en Barcelona a la misma hora.

---

## Output: el informe operativo

**[→ Ver informe live](https://sunsaver-bronze.s3.eu-south-2.amazonaws.com/reports/latest.html)**

El informe generado cada día contiene:

- **KPIs del día** — pico FV previsto, hora de máxima generación, precio mínimo y máximo PVPC
- **Plan de acción** — decisiones concretas por activo (carretillas, compresores, frío industrial...) ordenadas por prioridad y urgencia
- **Gráficos horarios** — curva de generación solar y precios PVPC solapados para ver las ventanas de oportunidad
- **Previsión 5 días** — outlook semanal por clima con fiabilidad indicada (sin PVP disponible más allá de D+1)

> 📄 Lógica del motor de decisiones → [`docs/04_informe_operativo.md`](docs/04_informe_operativo.md)

---

## Modelo de datos

Base de datos relacional PostgreSQL (RDS) con star schema preparado para analistas:

```
gold.dim_client     →  Parámetros de cada instalación
gold.dim_assets     →  Activos industriales por cliente
gold.dim_weather    →  Dimensión meteorológica
gold.dim_datetime   →  Dimensión temporal

gold.fact_energy_historical  →  Serie histórica completa (acumula ejecuciones)
gold.fact_energy_forecast    →  Ventana futura activa (se sobreescribe cada día)
```

> 📄 Esquema completo y diccionario de datos → [`docs/03_modelo_datos.md`](docs/03_modelo_datos.md)

---

## Stack técnico

| Categoría | Tecnología |
|-----------|-----------|
| Lenguaje | Python 3.12 |
| Motor solar | `pvlib`, `numpy` |
| Datos | `pandas`, `SQLAlchemy`, `psycopg2` |
| APIs | `requests` + ESIOS (REE) + OpenWeatherMap |
| Base de datos | PostgreSQL 15 en AWS RDS |
| Infraestructura | AWS Fargate · S3 · RDS · ECR · EventBridge · SSM · CloudWatch |
| CI/CD | GitHub Actions → ECR → ECS (OIDC, sin claves hardcodeadas) |
| Contenerización | Docker (imagen Python slim) |

---

## Estructura del repositorio

```
SunSaver-ETL-Platform/
│
├── src/
│   ├── engine_pv_physics.py             # Motor de cálculo solar (núcleo del sistema)
│   ├── run.py                           # Orquestador del pipeline (8 stages)
│   │
│   ├── bronze_ingest_prices_ree.py      # Extracción ESIOS/REE
│   ├── bronze_ingest_weather_owm.py     # Extracción OpenWeatherMap
│   ├── bronze_ingest_clients.py         # Extracción clientes desde Excel
│   ├── bronze_ingest_assets.py          # Extracción activos industriales
│   │
│   ├── silver_transform_*.py            # Limpieza y normalización (4 módulos)
│   ├── silver_calc_pv_generation.py     # Aplicación del motor solar sobre Silver
│   │
│   ├── gold_dim_*.py                    # Carga de dimensiones (4 módulos)
│   ├── gold_fact_energy_historical.py
│   ├── gold_fact_energy_forecast.py
│   │
│   ├── gold_fact_energy_decisions.py    # Motor de reglas: genera decisiones operativas
│   ├── report_generator.py              # Renderiza HTML y publica en S3
│   │
│   ├── config_paths.py                  # Rutas S3 y helpers AWS
│   ├── database_utils.py                # Engine SQLAlchemy
│   └── audit_metadata.py                # Registro de ejecuciones
│
├── docs/
│   ├── 01_arquitectura.md
│   ├── 02_motor_pv.md
│   ├── 03_modelo_datos.md
│   └── 04_informe_operativo.md
│
├── .github/workflows/deploy.yml         # CI/CD → ECR + ECS
├── task-definition.json                 # ECS Fargate task definition
├── Dockerfile
└── requirements.txt
```

---

## Documentación técnica

| Documento | Contenido |
|-----------|-----------|
| [01 · Arquitectura](docs/01_arquitectura.md) | Pipeline detallado, AWS, decisiones de diseño |
| [02 · Motor PV](docs/02_motor_pv.md) | Física solar, fórmulas, modelos aplicados |
| [03 · Modelo de datos](docs/03_modelo_datos.md) | Star schema, diccionario de campos, lineaje |
| [04 · Informe operativo](docs/04_informe_operativo.md) | Motor de decisiones, reglas, output HTML |

---

<sub>Desarrollado con Python · Desplegado en AWS eu-south-2 (España) · Ejecución diaria automática</sub>