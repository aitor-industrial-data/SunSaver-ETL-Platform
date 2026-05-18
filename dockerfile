FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

# Instalar dependencias del sistema (necesario para psycopg2-binary)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copiar e instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código fuente
COPY src/ ./src/

# Variables de entorno
ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

# No definir LOCAL_DEV para que use SSM en AWS
# ENV LOCAL_DEV=0q

# Comando por defecto
CMD ["python", "src/run.py"]