FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    NEWS_OFFICER_DB_PATH=/data/news-officer.sqlite3

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY feeds.json ./
COPY src ./src

CMD ["python", "-m", "news_officer"]
