# Stock Screener

Daily stock screener (200-day SMA + 8-day EMA + market cap, ranked by EPS)
running on AWS Lambda, triggered by EventBridge, results published to
[patrick-cloud.com/stock-screener/results.json](https://patrick-cloud.com/stock-screener/results.json)
and rendered on the [website](https://patrick-cloud.com/stock-screener.html).

See [stock-screener/README.md](../stock-screener/README.md) for the screener
logic itself and local (non-Lambda) usage.

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
