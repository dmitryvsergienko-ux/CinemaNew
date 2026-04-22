FROM python:3.12-alpine

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY events.py ./

EXPOSE 8082

CMD ["python", "events.py"]
