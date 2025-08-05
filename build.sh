#!/bin/bash

# Exit on any error
set -e

echo "=== Bắt đầu cài đặt ODBC Driver cho SQL Server ==="

# Update package list
apt-get update

# Install required packages
apt-get install -y curl gnupg2 unixodbc-dev

# Add Microsoft repository
echo "Thêm Microsoft repository..."
curl https://packages.microsoft.com/keys/microsoft.asc | apt-key add -
curl https://packages.microsoft.com/config/ubuntu/20.04/prod.list > /etc/apt/sources.list.d/mssql-release.list

# Update with new repository
apt-get update

# Install ODBC Driver 17 for SQL Server
echo "Cài đặt ODBC Driver 17 for SQL Server..."
ACCEPT_EULA=Y apt-get install -y msodbcsql17

# Verify installation
echo "Kiểm tra driver đã cài đặt:"
odbcinst -q -d

# Install Python dependencies
echo "Cài đặt Python dependencies..."
pip install -r requirements.txt

echo "=== Hoàn thành build script ==="