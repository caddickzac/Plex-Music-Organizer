FROM python:3.11-slim

# 1. Install system dependencies
# build-essential for compiling, plus fonts and image libraries 
# for your playlist thumbnail generator (Pillow)
RUN apt-get update && apt-get install -y \
    build-essential \
    fonts-liberation \
    libfreetype6-dev \
    libjpeg-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 2. Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Copy your specific project files
COPY . .

# 4. Streamlit config
EXPOSE 8501

CMD ["streamlit", "run", "Plex_Streamlit_App.py", "--server.port=8501", "--server.address=0.0.0.0"]