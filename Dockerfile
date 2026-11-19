FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV DB_PATH=/data/app.db
EXPOSE 8000

# 单 worker：SQLite 单写者模型下足够，且并发正确性由数据库层保证
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
