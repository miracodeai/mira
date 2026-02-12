FROM python:3.12-slim

COPY . /app
WORKDIR /app

RUN pip install --no-cache-dir /app

COPY .github/actions/mira-review/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
