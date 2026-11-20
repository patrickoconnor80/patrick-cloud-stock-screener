# Built and pushed to ECR by .github/workflows/deploy-app.yml on every code change.
FROM public.ecr.aws/lambda/python:3.11

COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r requirements.txt

COPY screener.py lambda_handler.py gex.py gex_handler.py thermo.py events.json ${LAMBDA_TASK_ROOT}/

# Default entrypoint is the screener; the GEX Lambda overrides it with
# gex_handler.lambda_handler via image_config in tf/gex.tf.
CMD ["lambda_handler.lambda_handler"]
