# Pinned base image: a tag you control, not a moving "latest".
FROM python:3.12.8-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Unprivileged user: nothing in this app needs root.
RUN useradd --system --uid 10001 --no-create-home appuser

# Dependencies first. This layer is only rebuilt when requirements.txt
# changes, so editing app code does not reinstall everything.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Source last.
COPY app ./app

USER appuser
EXPOSE 8080

# Default is the API. The worker overrides this in compose:
#   command: ["python", "-m", "app.worker"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
