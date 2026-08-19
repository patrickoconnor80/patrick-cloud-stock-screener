# Stock Screener

Daily stock screener (200-day SMA + 8-day EMA + market cap, ranked by EPS)
running on AWS Lambda, triggered by EventBridge weekdays at 4:01pm ET (right
after market close), results published to
[patrick-cloud.com/stock-screener/results.json](https://patrick-cloud.com/stock-screener/results.json)
and rendered in the Daily Screener section of the
[Stocks page](https://patrick-cloud.com/stocks.html).

Top 15 by trailing EPS. Each row also carries forward P/E and sector/industry
(both from yfinance) alongside price, market cap, and trailing EPS.

## Layout

- `screener.py` / `lambda_handler.py` — the screener + Lambda entrypoint
- `Dockerfile` — Lambda container image (`public.ecr.aws/lambda/python:3.11` base)
- `tf/` — ECR repo, Lambda function, EventBridge schedule, execution IAM role

## Deploys

- `.github/workflows/terraform.yml` — applies `tf/` on push to `main` (infra only)
- `.github/workflows/deploy-app.yml` — builds + pushes the image to ECR and updates
  the Lambda's code on push to `main` when `screener.py`, `lambda_handler.py`,
  `requirements.txt`, or `Dockerfile` change, then invokes it once so results
  are fresh immediately rather than waiting for the next scheduled run

Both authenticate to AWS via the GitHub OIDC deployer role set up in
`patrick-cloud-base-infra` (`tf/iam/roles.tf`) — no static AWS keys.

## Local run

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python screener.py
```
