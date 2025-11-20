# Use a small Python base image
FROM python:3.11-slim

# Where our app will live
WORKDIR /app

# Install system deps (optional but good practice)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Copy and install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the code
COPY . .

# Cloud Run will inject PORT, but we set a default
ENV PORT=8080

# Tell Functions Framework which function to run
ENV FUNCTION_TARGET=sync_shopify_orders
ENV FUNCTION_SIGNATURE_TYPE=http

# Start the HTTP server
CMD ["functions-framework", "--target=sync_shopify_orders", "--port", "8080"]
