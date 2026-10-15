FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ingestor.py .

ENV PYTHONUNBUFFERED=1
CMD ["python", "ingestor.py"]
