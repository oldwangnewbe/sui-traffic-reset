FROM python:3.12-alpine

WORKDIR /app

RUN apk add --no-cache tzdata

COPY sui_traffic_reset.py /app/sui_traffic_reset.py
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
COPY static /app/static

RUN chmod +x /app/sui_traffic_reset.py /app/docker-entrypoint.sh

ENV SUI_DB=/data/s-ui.db
ENV CHECK_INTERVAL=60

ENTRYPOINT ["/app/docker-entrypoint.sh"]
