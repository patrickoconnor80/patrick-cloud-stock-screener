"""
Lambda entrypoint for the SPY/QQQ dealer-gamma snapshot.

Same container image as the stock screener, with the image command overridden
to gex_handler.lambda_handler (see tf/gex.tf). EventBridge fires it every 15
minutes on weekdays; the schedule is in UTC, so it is padded to cover market
hours in both EST and EDT, and this handler skips runs outside 9:30-16:15 ET.

Environment variables (set by Terraform):
  RESULTS_BUCKET              S3 bucket to write gex.json to
  GEX_KEY                     S3 key, e.g. stock-screener/gex.json
  CLOUDFRONT_DISTRIBUTION_ID  distribution to invalidate after writing
"""

import json
import os
import time as _time
from datetime import datetime, time

import boto3

import gex

RESULTS_BUCKET = os.environ.get("RESULTS_BUCKET", "patrick-cloud.com")
GEX_KEY = os.environ.get("GEX_KEY", "stock-screener/gex.json")
CLOUDFRONT_DISTRIBUTION_ID = os.environ.get("CLOUDFRONT_DISTRIBUTION_ID")


def lambda_handler(event, context):
    now = datetime.now(gex.ET)
    force = isinstance(event, dict) and event.get("force")
    if not force and (now.weekday() >= 5 or not time(9, 30) <= now.time() <= time(16, 15)):
        return {"statusCode": 204, "skipped": "outside market hours"}

    payload = gex.build_snapshot()
    boto3.client("s3").put_object(
        Bucket=RESULTS_BUCKET,
        Key=GEX_KEY,
        Body=json.dumps(payload, default=str),
        ContentType="application/json",
        CacheControl="max-age=60",
    )
    if CLOUDFRONT_DISTRIBUTION_ID:
        boto3.client("cloudfront").create_invalidation(
            DistributionId=CLOUDFRONT_DISTRIBUTION_ID,
            InvalidationBatch={
                "Paths": {"Quantity": 1, "Items": [f"/{GEX_KEY}"]},
                "CallerReference": str(_time.time()),
            },
        )
    return {
        "statusCode": 200,
        "tickers": {t["ticker"]: t.get("regime") for t in payload["tickers"]},
        "pin_ready": [t["ticker"] for t in payload["tickers"] if t.get("pin", {}).get("ready")],
    }
