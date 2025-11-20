import os
import json
import datetime
import logging

import functions_framework
import requests
from google.cloud import bigquery

# ---- Logging setup ----
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _get_env(name: str) -> str:
    """Get required env var or raise nice error."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


# ---- Config from env ----
SHOPIFY_STORE_DOMAIN = _get_env("SHOPIFY_STORE_DOMAIN")  # e.g. "yourstore.myshopify.com"
SHOPIFY_API_TOKEN = _get_env("SHOPIFY_API_TOKEN")
BQ_PROJECT = _get_env("BQ_PROJECT")
BQ_DATASET = _get_env("BQ_DATASET")
BQ_TABLE = _get_env("BQ_TABLE")

ORDERS_API_VERSION = "2024-07"  # you can bump this later


# ---- Helper for shipping price ----
def _get_shipping_price(order_obj) -> float:
    shipping_lines = order_obj.get("shipping_lines") or []
    total = 0.0
    for s in shipping_lines:
        try:
            total += float(s.get("price") or 0)
        except (TypeError, ValueError):
            pass
    return total


# ---- HTTP entrypoint for Cloud Run ----
@functions_framework.http
def sync_shopify_orders(request):
    """
    Trigger: HTTP (Cloud Run)

    - GET  or POST on "/" runs "yesterday" backfill.
    - Returns text response with how many orders were inserted or what failed.
    """

    logger.info("Incoming request: method=%s path=%s", request.method, request.path)

    # Simple ping route: /health just returns OK
    if request.path == "/health":
        return ("OK", 200)

    client = bigquery.Client(project=BQ_PROJECT)
    table_id = f"{BQ_PROJECT}.{BQ_DATASET}.{BQ_TABLE}"

    # ---- Date window: yesterday ----
    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    created_at_min = yesterday.isoformat() + "T00:00:00Z"
    created_at_max = yesterday.isoformat() + "T23:59:59Z"

    logger.info(
        "Fetching Shopify orders from %s to %s", created_at_min, created_at_max
    )

    base_url = (
        f"https://{SHOPIFY_STORE_DOMAIN}/admin/api/"
        f"{ORDERS_API_VERSION}/orders.json"
    )
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_API_TOKEN,
        "Content-Type": "application/json",
    }
    params = {
        "status": "any",
        "limit": 250,
        "created_at_min": created_at_min,
        "created_at_max": created_at_max,
    }

    all_rows = []
    next_url = base_url

    # ---- Shopify pagination ----
    try:
        while True:
            resp = requests.get(next_url, headers=headers, params=params, timeout=30)
            logger.info("Shopify request: url=%s status=%s", resp.url, resp.status_code)
            resp.raise_for_status()
            data = resp.json()

            orders = data.get("orders", [])
            logger.info("Fetched %d orders in this page", len(orders))

            for o in orders:
                row = {
                    "order_id": str(o.get("id")),
                    "created_at": o.get("created_at"),
                    "updated_at": o.get("updated_at"),
                    "customer_id": (
                        str(o["customer"]["id"]) if o.get("customer") else None
                    ),
                    "email": o.get("email"),
                    "total_price": float(o.get("total_price") or 0),
                    "subtotal_price": float(o.get("subtotal_price") or 0),
                    "shipping_price": _get_shipping_price(o),
                    "tax_price": float(o.get("total_tax") or 0),
                    "discount_total": float(o.get("total_discounts") or 0),
                    "currency": o.get("currency"),
                    "financial_status": o.get("financial_status"),
                    "fulfillment_status": o.get("fulfillment_status"),
                    "utm_source": None,  # TODO: parse later
                    "utm_medium": None,
                    "utm_campaign": None,
                    "line_items_json": json.dumps(o.get("line_items", [])),
                }
                all_rows.append(row)

            # Pagination using Link header
            link_header = resp.headers.get("Link", "")
            if 'rel="next"' in link_header:
                parts = link_header.split(",")
                next_links = [p for p in parts if 'rel="next"' in p]
                if next_links:
                    link_part = next_links[0].split(";")[0].strip()
                    next_url = link_part.strip("<> ")
                    params = {}  # page_info already inside the URL
                    continue

            break

    except requests.exceptions.HTTPError as e:
        # This catches wrong token, wrong store, permission errors, etc.
        logger.exception(
            "Shopify API HTTP error: %s, body=%s",
            e,
            getattr(e.response, "text", "")[:500],
        )
        return ("Shopify API error – check logs for details", 500)
    except Exception:
        logger.exception("Unexpected error while calling Shopify API")
        return ("Unexpected Shopify error – check logs", 500)

    if not all_rows:
        logger.info("No orders found for %s", yesterday.isoformat())
        return (f"No orders found for {yesterday.isoformat()}", 200)

    # ---- Insert into BigQuery ----
    logger.info("Inserting %d rows into BigQuery table %s", len(all_rows), table_id)
    try:
        errors = client.insert_rows_json(table_id, all_rows)
    except Exception:
        logger.exception("BigQuery insert_rows_json() failed")
        return ("BigQuery insert error – see logs", 500)

    if errors:
        logger.error("BigQuery row insert errors: %s", errors)
        return (f"BigQuery row errors: {errors}", 500)

    logger.info("Inserted %d orders for %s", len(all_rows), yesterday.isoformat())
    return (f"Inserted {len(all_rows)} orders for {yesterday.isoformat()}", 200)
