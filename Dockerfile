FROM python:3.10-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV STOCK_LAB_HOME=/app/runtime
EXPOSE 8010
CMD ["python", "-m", "app.launch", "--host", "0.0.0.0", "--port", "8010", "--no-browser"]
