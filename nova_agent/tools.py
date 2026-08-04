from lambda_client import LambdaInvoker
from config import Config


# def book_appointment(
#     patient_name: str,
#     phone_number: str,
#     preferred_date: str,
#     preferred_time: str,
#     treatment: str = ""
# ):

#     payload = {
#         "sessionState": {
#             "intent": {
#                 "name": "BookAppointment",
#                 "slots": {
#                     "PatientName": {
#                         "value": {
#                             "interpretedValue": patient_name
#                         }
#                     },
#                     "PhoneNumber": {
#                         "value": {
#                             "interpretedValue": phone_number
#                         }
#                     },
#                     "PreferredDate": {
#                         "value": {
#                             "interpretedValue": preferred_date
#                         }
#                     },
#                     "PreferredTime": {
#                         "value": {
#                             "interpretedValue": preferred_time
#                         }
#                     },
#                     "Treatment": {
#                         "value": {
#                             "interpretedValue": treatment
#                         }
#                     }
#                 }
#             }
#         }
#     }

#     response = LambdaInvoker.invoke(
#         Config.BOOK_APPOINTMENT_LAMBDA,
#         payload
#     )

#     return response

def book_appointment(
    patient_name,
    phone_number,
    preferred_date,
    preferred_time,
    treatment=""
):
    print("BOOK_APPOINTMENT CALLED")
    print(patient_name)
    print(phone_number)
    print(preferred_date)
    print(preferred_time)
    print(treatment)

    payload = {
        "sessionState": {
            "intent": {
                "name": "BookAppointment",
                "slots": {
                    "PatientName": {
                        "value": {
                            "interpretedValue": patient_name
                        }
                    },
                    "PhoneNumber": {
                        "value": {
                            "interpretedValue": phone_number
                        }
                    },
                    "PreferredDate": {
                        "value": {
                            "interpretedValue": preferred_date
                        }
                    },
                    "PreferredTime": {
                        "value": {
                            "interpretedValue": preferred_time
                        }
                    },
                    "Treatment": {
                        "value": {
                            "interpretedValue": treatment
                        }
                    }
                }
            }
        }
    }

    response = LambdaInvoker.invoke(
        Config.BOOK_APPOINTMENT_LAMBDA,
        payload
    )

    print("LAMBDA RESPONSE:")
    print(response)

    return response