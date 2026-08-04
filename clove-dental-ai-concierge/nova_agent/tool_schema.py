BOOK_APPOINTMENT_TOOL = {
    "name": "book_appointment",
    "description": (
        "Book a dental appointment in Google Calendar."
        "Use only after collecting and confirming all details."
    ),
    "inputSchema": {
        "json": {
            "type": "object",
            "properties": {
                "patient_name": {
                    "type": "string"
                },
                "phone_number": {
                    "type": "string"
                },
                "preferred_date": {
                    "type": "string",
                    "description": "YYYY-MM-DD"
                },
                "preferred_time": {
                    "type": "string",
                    "description": "HH:MM 24-hour format"
                },
                "treatment": {
                    "type": "string"
                }
            },
            "required": [
                "patient_name",
                "phone_number",
                "preferred_date",
                "preferred_time"
            ]
        }
    }
}



FIND_NEARBY_CLINIC_TOOL = {
    "name": "findNearbyClinicTool",
    "description": (
        "Find the nearest Clove Dental clinics based on the user's location."
    ),
    "inputSchema": {
        "json": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "User's city, locality or address"
                }
            },
            "required": [
                "location"
            ]
        }
    }
}


OPEN_MAPS_TOOL = {
    "name": "openGoogleMapsTool",
    "description": (
        "Open Google Maps directions for the previously selected clinic."
    ),
    "inputSchema": {
        "json": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
}

TOOLS = [
    BOOK_APPOINTMENT_TOOL,
    FIND_NEARBY_CLINIC_TOOL,
    OPEN_MAPS_TOOL
]