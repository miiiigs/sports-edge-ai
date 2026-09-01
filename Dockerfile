FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY . .

RUN python -m pip install --upgrade pip \
    && pip install -e "./code[api]"

CMD ["python", "-m", "sports_ai.dashboard.runtime_supervisor"]
