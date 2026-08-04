import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

    BOOK_APPOINTMENT_LAMBDA = os.getenv("BOOK_APPOINTMENT_LAMBDA")
    CANCEL_APPOINTMENT_LAMBDA = os.getenv("CANCEL_APPOINTMENT_LAMBDA")
    RESCHEDULE_APPOINTMENT_LAMBDA = os.getenv("RESCHEDULE_APPOINTMENT_LAMBDA")

    FIND_CLINIC_LAMBDA = os.getenv("FIND_CLINIC_LAMBDA")
    FAQ_LAMBDA = os.getenv("FAQ_LAMBDA")
    CLINIC_HOURS_LAMBDA = os.getenv("CLINIC_HOURS_LAMBDA")
    TRANSFER_LAMBDA = os.getenv("TRANSFER_LAMBDA")

    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    APP_NAME = "Clove Nova Concierge"