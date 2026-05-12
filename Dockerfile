# syntax=docker/dockerfile:1.6

FROM docker:24-cli AS docker-cli

FROM python:3.11-slim

WORKDIR /app

# Copy the docker CLI binary so `docker exec engine ...` works for the diag runner.
# The docker daemon is provided by the host via the mounted /var/run/docker.sock.
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app /app/app

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
