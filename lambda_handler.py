"""
Lambda entrypoint for the daily stock screener.

Runs the screener (see screener.py), writes the results as JSON to the
website's S3 bucket, and invalidates CloudFront so the site picks up the
new data immediately.

Environment variables (set by Terraform, see tf/lambda.tf):
  RESULTS_BUCKET              S3 bucket to write results.json to
  RESULTS_KEY                 S3 key, e.g. stock-screener/results.json
  CLOUDFRONT_DISTRIBUTION_ID  distribution to invalidate after writing
  TOP_N                       number of results to keep (default 15)
  MIN_MARKETCAP               minimum market cap filter (default 1e9)
"""

import json
import os
import time
from datetime import datetime, timezone

import boto3

import screener
import thermo

RESULTS_BUCKET = os.environ.get("RESULTS_BUCKET", "patrick-cloud.com")
RESULTS_KEY = os.environ.get("RESULTS_KEY", "stock-screener/results.json")
CLOUDFRONT_DISTRIBUTION_ID = os.environ.get("CLOUDFRONT_DISTRIBUTION_ID")
TOP_N = int(os.environ.get("TOP_N", "15"))
MIN_MARKETCAP = float(os.environ.get("MIN_MARKETCAP", "1e9"))


def run_screen() -> dict:
    universe = screener.fetch_sp1500_universe()
    survivors = screener.technical_filter(universe, sma_period=200, ema_period=8)

    if not survivors:
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "results": []}

    df = screener.fundamental_filter(survivors, MIN_MARKETCAP)
    if df.empty:
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "results": []}

    df = df.sort_values("eps", ascending=False).head(TOP_N)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "filters": {
            "sma_period": 200,
            "ema_period": 8,
            "min_marketcap": MIN_MARKETCAP,
        },
        "results": df.to_dict(orient="records"),
    }


def lambda_handler(event, context):
    # Market thermometer (top/bottom gauge on data.html). It rides along in
    # results.json so the Lambda needs no new S3 key; a failure here must not
    # block the screen.
    try:
        thermometer = thermo.build(screener.fetch_sp1500_universe())
        print(f"thermometer {thermometer['as_of']}: {thermometer['score']} ({thermometer['bucket']})")
    except Exception as ex:
        thermometer = None
        print(f"thermometer failed: {type(ex).__name__}: {ex}")

    payload = run_screen()
    payload["thermometer"] = thermometer

    s3 = boto3.client("s3")
    s3.put_object(
        Bucket=RESULTS_BUCKET,
        Key=RESULTS_KEY,
        Body=json.dumps(payload, default=str),
        ContentType="application/json",
        CacheControl="max-age=300",
    )

    if CLOUDFRONT_DISTRIBUTION_ID:
        cf = boto3.client("cloudfront")
        cf.create_invalidation(
            DistributionId=CLOUDFRONT_DISTRIBUTION_ID,
            InvalidationBatch={
                "Paths": {"Quantity": 1, "Items": [f"/{RESULTS_KEY}"]},
                "CallerReference": str(time.time()),
            },
        )

    return {"statusCode": 200, "resultCount": len(payload["results"])}
