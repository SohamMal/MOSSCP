# mosaic rooms server -- memory-only, no volumes, no database.
FROM python:3.12-slim

# Predictor rooms compile each peer's mine.cpp, so the image needs g++.
RUN apt-get update \
 && apt-get install -y --no-install-recommends g++ \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY mosaic ./mosaic
# The branch-predictor simulator and the traces predictor rooms run on.
# Point MOSAIC_BP_TRACES somewhere else to use other traces.
COPY bpsim ./bpsim

# Behind a cloud load balancer: trust X-Forwarded-For for rate limiting.
# Never write bytecode. Similarity rooms write nothing; predictor rooms need a
# writable /tmp for the one build or run in progress (deleted straight after).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MOSAIC_BEHIND_PROXY=1

USER nobody
EXPOSE 8765

# Cloud hosts set $PORT; mosaic reads it. Set MOSAIC_CREATE_KEY and
# MOSAIC_PUBLIC_URL in the host's environment settings.
CMD ["python", "-m", "mosaic", "serve", "--host", "0.0.0.0"]
