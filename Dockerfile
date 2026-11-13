# mosaic rooms server -- memory-only, no volumes, no database.
FROM python:3.12-slim

WORKDIR /app
COPY mosaic ./mosaic

# Behind a cloud load balancer: trust X-Forwarded-For for rate limiting.
# Never write bytecode or anything else: the container can run read-only.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MOSAIC_BEHIND_PROXY=1

USER nobody
EXPOSE 8765

# Cloud hosts set $PORT; mosaic reads it. Set MOSAIC_CREATE_KEY and
# MOSAIC_PUBLIC_URL in the host's environment settings.
CMD ["python", "-m", "mosaic", "serve", "--host", "0.0.0.0"]
