FROM python:3.11.3-slim
RUN apt-get update && apt-get install -y ghostscript build-essential && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . /app
RUN pip install --upgrade pip
RUN pip install -r requirements.txt
ENV PORT 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
