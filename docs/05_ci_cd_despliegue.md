# 05 · CI/CD y despliegue

[← README](../README.md)

---

## Visión general

Cada vez que se hace push a `main`, GitHub Actions construye la imagen Docker, la publica en ECR y registra una nueva revisión de la task definition en ECS. EventBridge Scheduler apunta a la familia sin número de revisión, de modo que la próxima ejecución automática coge la imagen recién desplegada sin ninguna intervención manual.

```
git push → main
     │
     ▼
GitHub Actions
  1. Checkout código
  2. Docker Buildx setup
  3. Asumir rol IAM via OIDC  ← sin claves AWS en secretos
  4. Login ECR
  5. docker build + push  (:SHA + :latest)
  6. Render task definition  (inyecta SHA exacto)
  7. Register task definition  (nueva revisión en ECS)
     │
     └── EventBridge cron(05 19 * * ? *)
              └── ECS RunTask → Fargate ejecuta :latest
```

---

## Autenticación OIDC — sin claves AWS

El workflow no usa `AWS_ACCESS_KEY_ID` ni `AWS_SECRET_ACCESS_KEY`. En su lugar usa federación de identidad OIDC:

```yaml
permissions:
  id-token: write   # permite solicitar token OIDC a GitHub
  contents: read

- name: Configure AWS Credentials
  uses: aws-actions/configure-aws-credentials@v4
  with:
    role-to-assume: arn:aws:iam::610140802215:role/github-sunsaver-ecr-role
    aws-region: eu-south-2
    audience: sts.amazonaws.com
```

**Cómo funciona**: GitHub emite un token OIDC firmado que identifica el repositorio y la rama. AWS STS verifica ese token contra el proveedor OIDC configurado en IAM y devuelve credenciales temporales para el rol `github-sunsaver-ecr-role`. Las credenciales duran el tiempo del job y no se almacenan en ningún secreto del repositorio.

**Ventaja de seguridad**: si el repositorio se compromete, no hay claves de larga duración que rotar. El token OIDC solo es válido para el repositorio y rama configurados en la política de confianza del rol IAM.

---

## Step a step del workflow

### Step 1 — Checkout

```yaml
- uses: actions/checkout@v4
```

Descarga el código del commit que disparó el push. Todo lo que sigue opera sobre ese estado exacto.

---

### Step 2 — Docker Buildx

```yaml
- uses: docker/setup-buildx-action@v3
```

Instala BuildKit, el backend moderno de Docker. Habilita caché de capas entre builds, builds multi-plataforma y mejor manejo de secretos en tiempo de build.

---

### Step 3 — Login ECR

```yaml
- id: login-ecr
  uses: aws-actions/amazon-ecr-login@v2
```

Usa las credenciales OIDC del step anterior para autenticarse en ECR `eu-south-2`. Devuelve `steps.login-ecr.outputs.registry` con la URL del registro (`610140802215.dkr.ecr.eu-south-2.amazonaws.com`).

---

### Step 4 — Build, Tag y Push

```yaml
env:
  ECR_REGISTRY:   ${{ steps.login-ecr.outputs.registry }}
  ECR_REPOSITORY: sunsaver-etl
  IMAGE_TAG:      ${{ github.sha }}

run: |
  docker build -t $ECR_REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG .
  docker tag     $ECR_REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG \
                 $ECR_REGISTRY/$ECR_REPOSITORY:latest
  docker push    $ECR_REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG
  docker push    $ECR_REGISTRY/$ECR_REPOSITORY:latest
  echo "image=$ECR_REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG" >> $GITHUB_OUTPUT
```

Se generan **dos tags**:

| Tag | Valor | Uso |
|-----|-------|-----|
| `:SHA` | SHA del commit (ej: `a3f9c12...`) | Historial inmutable. Permite hacer rollback a cualquier commit. |
| `:latest` | Alias al SHA actual | Lo que Fargate ejecuta. Siempre apunta al último deploy. |

El SHA se expone como output del step para usarlo en el siguiente.

---

### Step 5 — Render task definition

```yaml
- id: render-task-def
  uses: aws-actions/amazon-ecs-render-task-definition@v1
  with:
    task-definition: task-definition.json
    container-name:  sunsaver-etl
    image:           ${{ steps.build-image.outputs.image }}
```

Lee `task-definition.json` del repositorio y sustituye el campo `image` del contenedor `sunsaver-etl` por la URL con el SHA exacto del commit. Produce un JSON temporal con la definición actualizada.

**Por qué el SHA y no `:latest`**: registrar la task definition con el SHA exacto garantiza que ECS sabe qué código ejecuta cada revisión. Si hay un fallo en producción se puede identificar el commit exacto desde la consola ECS sin buscar en los logs.

---

### Step 6 — Register task definition

```yaml
- run: |
    aws ecs register-task-definition \
      --cli-input-json file://${{ steps.render-task-def.outputs.task-definition }}
```

Registra la nueva revisión en ECS. ECS asigna automáticamente el siguiente número de revisión (`sunsaver-etl-task:N`). EventBridge Scheduler apunta a la familia `sunsaver-etl-task` sin número, de modo que en la próxima ejecución cron coge esta revisión nueva automáticamente, sin modificar el scheduler.

---

## El Dockerfile explicado

```dockerfile
FROM public.ecr.aws/docker/library/python:3.12-slim
```
Imagen base oficial desde el registry público de ECR (evita rate limits de Docker Hub en Fargate).

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*
```
`psycopg2-binary` necesita `libpq-dev` en compilación. `gcc` para extensiones C de numpy/pandas. Se limpia la caché de apt para reducir tamaño de imagen.

```dockerfile
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
```
Las dependencias se instalan antes de copiar el código fuente. Así Docker cachea esta capa y no reinstala paquetes si solo cambia el código.

```dockerfile
COPY src/ ./src/
ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1
CMD ["python", "src/run.py"]
```
`PYTHONUNBUFFERED=1` es crítico en Fargate: sin él, los logs de Python van a un buffer interno y CloudWatch no los recibe hasta que el buffer se vacía, lo que puede dejar la ejecución sin logs visibles durante minutos.

---

## Despliegue manual de emergencia

Si hay que ejecutar el pipeline fuera del cron programado (reproceso, corrección de datos, prueba en producción):

```bash
aws ecs run-task \
  --cluster sunsaver-cluster \
  --task-definition sunsaver-etl-task \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={
    subnets=[subnet-05a527b7089d50fbe],
    securityGroups=[sg-0af186c633b6cc3fe],
    assignPublicIp=ENABLED
  }" \
  --region eu-south-2
```

Para ejecutar desde un stage concreto (por ejemplo, saltarse el Bronze y empezar desde Silver):

```bash
# Sobrescribir el CMD del contenedor añadiendo --stage N
aws ecs run-task \
  --cluster sunsaver-cluster \
  --task-definition sunsaver-etl-task \
  --launch-type FARGATE \
  --overrides '{"containerOverrides":[{"name":"sunsaver-etl","command":["python","src/run.py","--stage","3"]}]}' \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-05a527b7089d50fbe],securityGroups=[sg-0af186c633b6cc3fe],assignPublicIp=ENABLED}" \
  --region eu-south-2
```

Para hacer un dry-run (ver el plan de ejecución sin ejecutar nada):

```bash
--overrides '{"containerOverrides":[{"name":"sunsaver-etl","command":["python","src/run.py","--dry-run"]}]}'
```

---

## Rollback a una versión anterior

```bash
# 1. Listar revisiones disponibles
aws ecs list-task-definitions \
  --family-prefix sunsaver-etl-task \
  --region eu-south-2

# 2. Activar una revisión anterior como la que ejecutará el próximo cron
#    (EventBridge Scheduler usa la familia sin número → coge la activa por defecto)
aws ecs describe-task-definition \
  --task-definition sunsaver-etl-task:42 \
  --region eu-south-2

# 3. Registrar esa revisión como la más reciente
aws ecs register-task-definition \
  --cli-input-json file://task-definition-v42.json \
  --region eu-south-2
```

Alternativamente: hacer `git revert` del commit problemático y dejar que el CI/CD construya y registre la versión revertida automáticamente.

---

## Variables de entorno: DEV vs PRD

| Variable | DEV (`.env` local) | PRD (SSM → ECS) |
|----------|-------------------|-----------------|
| `DB_USER` | `.env` | `arn:aws:ssm:.../DB_USER` |
| `DB_PASS` | `.env` | `arn:aws:ssm:.../DB_PASS` |
| `DB_HOST` | `.env` | `arn:aws:ssm:.../DB_HOST` |
| `DB_NAME` | `.env` | `arn:aws:ssm:.../DB_NAME` |
| `DB_PORT` | `.env` (default 5432) | `arn:aws:ssm:.../DB_PORT` |
| `ESIOS_API_KEY` | `.env` | `arn:aws:ssm:.../ESIOS_API_KEY` |
| `WEATHER_API_KEY` | `.env` | `arn:aws:ssm:.../WEATHER_API_KEY` |
| `AWS_REGION` | `.env` | task-definition `environment` |
| `S3_BUCKET` | `.env` | task-definition `environment` |
| `BRONZE_PREFIX` | `.env` | task-definition `environment` |
| `ENVIRONMENT` | `DEV` | `PRD` (task-definition `environment`) |

`ENVIRONMENT=PRD` activa: solo stdout en logs (no FileHandler local), SSM para secretos, conexión RDS en producción.

---

[← Arquitectura](01_arquitectura.md) · [Operaciones →](06_operaciones.md)