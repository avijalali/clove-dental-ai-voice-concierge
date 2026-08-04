import json
import boto3

from config import Config
from logger import logger


lambda_client = boto3.client(
    "lambda",
    region_name=Config.AWS_REGION
)


class LambdaInvoker:

    @staticmethod
    def invoke(function_name: str, payload: dict):

        logger.info(f"Invoking Lambda: {function_name}")

        response = lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload)
        )

        body = response["Payload"].read()

        if not body:
            return {}

        return json.loads(body)