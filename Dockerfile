FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/miracodeai/mira"
LABEL org.opencontainers.image.description="Self-hostable AI code reviewer"
LABEL org.opencontainers.image.licenses="Apache-2.0"

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir "/app[serve]"

CMD ["mira", "serve"]
