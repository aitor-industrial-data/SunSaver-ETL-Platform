# 02 · Motor de cálculo fotovoltaico

[← README](../README.md)

---

## Por qué un motor propio

La mayoría de plataformas de monitorización solar usan estimaciones de producción basadas en regresiones históricas o modelos de caja negra. SunSaver calcula la generación desde primeros principios físicos, hora a hora, para las coordenadas exactas de cada instalación y con los datos meteorológicos reales del momento.

Esto permite dos cosas que los modelos estadísticos no pueden hacer bien: **extrapolar a clientes nuevos sin histórico previo** y **explicar exactamente por qué la generación prevista es la que es** (temperatura de célula alta, nubes pesadas, sol bajo en el horizonte...).

El motor está en `src/engine_pv_physics.py`. Cada función es independiente, testeable por separado y falla con `logger.warning` en lugar de propagar excepciones que paren el pipeline.

---

## Cadena de cálculo

La generación para cada cliente × cada hora sigue esta cadena secuencial:

```
Coordenadas GPS + timestamp
        │
        ▼
1. POSICIÓN SOLAR ──────────────────────── elevación (α), azimut
        │
        ▼
2. IRRADIANCIA GHI ─────────────────────── W/m² sobre superficie horizontal
   Haurwitz clear-sky × Kasten-Czeplak (nubes) × factor meteoro
        │
        ▼
3. DESCOMPOSICIÓN ERBS ─────────────────── DNI (directa normal) + DHI (difusa)
   Por índice de claridad kt
        │
        ▼
4. IRRADIANCIA POA ─────────────────────── W/m² sobre el plano del panel
   Beam + Liu-Jordan diffuse + albedo suelo (ρ=0.2)
        │
        ▼
5. TEMPERATURA DE CÉLULA ───────────────── °C real de operación
   Modelo Faiman con enfriamiento por viento
        │
        ▼
6. POTENCIA AC ─────────────────────────── kW generados + Performance Ratio
   Derating térmico γ + pérdidas de sistema
```

---

## Función 1 — Posición solar

```python
calculate_solar_position(latitude, longitude, forecast_time_utc)
→ (alfa: float, azimuth: float)
```

Usa `pvlib.solarposition.get_solarposition()` que implementa el algoritmo NREL SPA (Solar Position Algorithm), preciso a menos de 0.0003°. El timestamp se convierte a UTC antes de calcular para evitar errores de zona horaria.

Cuando `alfa ≤ 0` (sol bajo el horizonte), todas las funciones subsiguientes devuelven 0 directamente sin calcular. Esto elimina el coste computacional nocturno y evita divisiones por cero en ángulos rasantes.

---

## Función 2 — Irradiancia GHI

```python
calculate_ghi(alfa, clouds_pct, weather_id)
→ ghi: float  [W/m²]
```

**Modelo Haurwitz** (clear-sky):
```
G_clear = 1098 × sin(α) × exp(−0.057 / sin(α))
```

**Factor de nubosidad** (Kasten-Czeplak):
```
f_cloud = 1 − 0.75 × (clouds_pct / 100)³
```

**Factor por tipo de meteoro** — el `weather_id` de OpenWeatherMap se mapea a un coeficiente de transmitancia:

| Condición | weather_id | Factor |
|-----------|-----------|--------|
| Despejado | 800 | 1.00 |
| Nubes ligeras | 801–802 | 0.95 |
| Nubes pesadas | 803–804 | 0.80 |
| Lluvia | 5xx | 0.70 |
| Tormenta | 2xx | 0.40 |
| Nieve | 6xx | 0.75 |
| Niebla / bruma | 7xx | 0.60 |
| Llovizna | 3xx | 0.85 |

```
GHI = G_clear × f_cloud × f_weather
```

---

## Función 3 — Descomposición Erbs

```python
decompose_erbs(ghi, alfa, forecast_time_utc)
→ (dni: float, dhi: float)  [W/m²]
```

Separa la irradiancia global (GHI) en sus componentes directa (DNI) y difusa (DHI) usando el **modelo de Erbs** basado en el índice de claridad `kt`:

```
kt = GHI / (DNI_extra × sin(α))
```

La fracción difusa sigue tres regímenes según `kt`:
- `kt ≤ 0.22` → cielo muy nublado: `diff_frac = 1 − 0.09×kt`
- `0.22 < kt ≤ 0.80` → cielo parcial: polinomio de 4º grado
- `kt > 0.80` → cielo muy claro: `diff_frac = 0.165`

DNI se limita a la irradiancia extraterrestre calculada con `pvlib.irradiance.get_extra_radiation()` para evitar valores físicamente imposibles.

---

## Función 4 — Irradiancia POA (Plane Of Array)

```python
calculate_total_poa(dni, dhi, ghi, alfa, azimuth_solar, angle, aspect)
→ poa: float  [W/m²]
```

La POA es la irradiancia real que llega al panel según su inclinación y orientación, suma de tres componentes:

```
POA = G_beam + G_diffuse + G_albedo
```

- **G_beam** = `DNI × cos(θ)` donde θ es el ángulo de incidencia calculado con `pvlib.irradiance.aoi(angle, aspect, zenith, azimuth_solar)`
- **G_diffuse** = `DHI × (1 + cos(angle)) / 2`  (modelo isotrópico Liu-Jordan)
- **G_albedo** = `GHI × 0.2 × (1 − cos(angle)) / 2`  (albedo suelo ρ = 0.20)

`angle` y `aspect` son parámetros propios de cada instalación cliente (ángulo de inclinación del panel y orientación respecto al sur).

---

## Función 5 — Temperatura de célula

```python
calculate_t_cell(temp_ambient, wind_speed, poa)
→ t_cell: float  [°C]
```

**Modelo Faiman** con coeficientes para montaje open-rack estándar:

```
T_cell = T_ambient + POA / (U0 + U1 × wind_speed)
```

- `U0 = 24.9` W/(m²·K) — pérdida de calor por convección natural
- `U1 = 6.1` W·s/(m³·K) — enfriamiento adicional por viento

A mayor velocidad de viento, menor temperatura de célula, mayor eficiencia. Este modelo captura el efecto real: un día ventoso con 30°C puede generar más que un día calmado con 25°C.

---

## Función 6 — Potencia AC y Performance Ratio

```python
calculate_power_output(poa, t_cell, peak_power_kw, loss_pct)
→ (p_out: float [kW], pr: float)
```

```
f_temp = 1 + γ × (T_cell − 25)        # γ = −0.004  (−0.4 %/°C)
PR     = f_temp × (1 − loss_pct/100)
P_out  = (POA / 1000) × peak_power_kw × PR
```

- **Derating térmico**: por cada grado por encima de 25°C la eficiencia cae un 0.4%. Un panel a 60°C en verano trabaja al 86% de su capacidad nominal.
- **`loss_pct`**: pérdidas de sistema del cliente (inversor, cableado, suciedad, sombreado parcial...). Parámetro configurable por instalación, típicamente 10–18%.
- **`peak_power_kw`**: potencia pico STC (Standard Test Conditions) de la instalación.

---

## Consumo industrial simulado

```python
calculate_industrial_consumption(forecast_time_utc, nominal_load_kw, temp_ambient)
→ consumption: float  [kW]
```

Modelo de consumo industrial que simula el perfil real de una fábrica:

- **Patrón de turnos**: base alta en horas de producción (06–22h laborables), baja en noche y fin de semana
- **Carga HVAC térmica**: +2% de nominal por cada grado por encima de 25°C (refrigeración), +1% por cada grado por debajo de 15°C (calefacción)
- **Variabilidad de proceso**: ruido gaussiano ±3% sobre la carga base (σ=0.03)

Este consumo se cruza en Gold con la generación FV y el precio PVPC para identificar las ventanas de oportunidad del informe.

---

## Integración en el pipeline

`silver_calc_pv_generation.py` orquesta el motor aplicándolo sobre el join de `silver.clean_clients` × `silver.clean_weather`, hora a hora, para todos los clientes activos:

```python
for _, row in df_merged.iterrows():
    alfa, azimuth = pvgen.calculate_solar_position(row["latitude"], row["longitude"], row["forecast_time_utc"])
    if alfa < 2:          # Sol bajo el horizonte: generación = 0
        p_gen, pr = 0.0, 0.0
    else:
        ghi       = pvgen.calculate_ghi(alfa, row["clouds_pct"], row["weather_id"])
        dni, dhi  = pvgen.decompose_erbs(ghi, alfa, row["forecast_time_utc"])
        poa       = pvgen.calculate_total_poa(dni, dhi, ghi, alfa, azimuth, row["angle"], row["aspect"])
        t_cell    = pvgen.calculate_t_cell(row["temp_celsius"], row["wind_speed_mps"], poa)
        p_gen, pr = pvgen.calculate_power_output(poa, t_cell, row["pv_peak_power_kw"], row["loss_pct"])
```

Los resultados se cargan en `silver.clean_calculations` con upsert `ON CONFLICT DO UPDATE`, de modo que relanzar el pipeline para el mismo periodo es seguro e idempotente.

---

[← Arquitectura](01_arquitectura.md) · [Modelo de datos →](03_modelo_datos.md)