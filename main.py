import functions_framework
import requests
import json
import datetime
import os

from google.cloud import bigquery

SHOPIFY_STORE_DOMAIN = os.environ["SHOPIFY_STORE_DOMAIN"]
SHOPIFY_API_TOKEN = os.environ["SHOPIFY_API_TOKEN"]
BQ_PROJECT = os.environ["BQ_PROJECT"]
BQ_DATASET = os.environ["BQ_DATASET"]
BQ_TABLE = os.environ["BQ_TABLE"]

@functions_framework.http
def sync_shopify_orders(request):
    client = bigquery.Client(project=BQ_PROJECT)
    table_id = f"{BQ_PROJECT}.{BQ_DATASET}.{BQ_TABLE}"

    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    created_at_min = yesterday.isoformat() + "T00:00:00Z"
    created_at_max = yesterday.isoformat() + "T23:59:59Z"

    url = f"https://{SHOPIFY_STORE_DOMAIN}/admin/api/2024-07/orders.json"
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_API_TOKEN,
        "Content-Type": "application/json"
    }
    params = {
        "status": "any",
        "limit": 250,
        "created_at_min": created_at_min,
        "created_at_max": created_at_max,
    }

    all_rows = []
    next_url = url

    while True:
        resp = requests.get(next_url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        orders = data.get("orders", [])

        for o in orders:
            row = {
                "order_id": str(o.get("id")),
                "created_at": o.get("created_at"),
                "updated_at": o.get("updated_at"),
                "customer_id": str(o["customer"]["id"]) if o.get("customer") else None,
                "email": o.get("email"),
                "total_price": float(o.get("total_price") or 0),
                "subtotal_price": float(o.get("subtotal_price") or 0),
                "shipping_price": _get_shipping_price(o),
                "tax_price": float(o.get("total_tax") or 0),
                "discount_total": float(o.get("total_discounts") or 0),
                "currency": o.get("currency"),
                "financial_status": o.get("financial_status"),
                "fulfillment_status": o.get("fulfillment_status"),
                "utm_source": None,   
                "utm_medium": None,
                "utm_campaign": None,
                "line_items_json": json.dumps(o.get("line_items", [])),
            }
            all_rows.append(row)

        link_header = resp.headers.get("Link", "")
        if 'rel="next"' in link_header:
            parts = link_header.split(",")
            next_links = [p for p in parts if 'rel="next"' in p]
            if next_links:
                link_part = next_links[0].split(";")[0].strip()
                next_url = link_part.strip("<> ")
                params = {}
                continue
        break

    if not all_rows:
        return ("No orders found for yesterday", 200)

    errors = client.insert_rows_json(table_id, all_rows)
    if errors:
        print("BigQuery insert errors:", errors)
        return (f"Errors inserting rows: {errors}", 500)

    return (f"Inserted {len(all_rows)} orders for {yesterday}", 200)


def _get_shipping_price(order_obj):
    shipping_lines = order_obj.get("shipping_lines") or []
    if not shipping_lines:
        return 0.0
    total = 0.0
    for s in shipping_lines:
        try:
            total += float(s.get("price") or 0)
        except:
            pass
    return total
