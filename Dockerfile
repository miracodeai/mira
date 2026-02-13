FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/miracodeai/mira"
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir "/app[serve]"

ARG POSTHOG_API_KEY=""
ARG POSTHOG_HOST="https://us.i.posthog.com"
ENV POSTHOG_API_KEY=${POSTHOG_API_KEY}
ENV POSTHOG_HOST=${POSTHOG_HOST}

CMD ["mira", "serve"]
