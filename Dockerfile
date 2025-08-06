# Sử dụng Python 3.12 slim image
FROM python:3.12-slim

# Cài đặt system dependencies và locale tiếng Việt
RUN apt-get update && apt-get install -y \
    curl \
    gnupg2 \
    locales \
    unixodbc \
    unixodbc-dev \
    && echo "vi_VN.UTF-8 UTF-8" > /etc/locale.gen \
    && locale-gen vi_VN.UTF-8 \
    && rm -rf /var/lib/apt/lists/*

ENV LANG=vi_VN.UTF-8
ENV LANGUAGE=vi_VN:vi
ENV LC_ALL=vi_VN.UTF-8

# Cài đặt Microsoft ODBC Driver 17 for SQL Server
RUN curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list > /etc/apt/sources.list.d/mssql-release.list \
    && echo "deb [signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" >> /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y msodbcsql17 \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first (for better caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create non-root user
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app
USER app

# Expose port (mặc định 5000, Render sẽ ghi đè bằng $PORT)
ENV PORT=5000
EXPOSE $PORT

# Run the application
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "4", "--timeout", "120", "AI:app"]