import os
import asyncio
import base64
import json
import uuid
import warnings
import pytz
import random
import hashlib
import datetime
import time
import inspect
from google_maps_service import GoogleMapsService
import webbrowser
from tool_schema import TOOLS

from tools import book_appointment

from dotenv import load_dotenv
import os

# --- FastAPI (browser voice bridge) ------------------------------------
# Added so app2.py can be served with `uvicorn app2:app`. This does not
# touch the existing Nova Sonic / tool-calling pipeline below — it only
# adds a WebSocket transport as an alternative to the PyAudio CLI loop.
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

# --- PyAudio is CLI-only -------------------------------------------------
# The browser client streams audio over the /ws/voice WebSocket instead of
# a local microphone, so PyAudio is no longer a hard requirement to run
# this file under uvicorn. It's still used by AudioStreamer/main() below
# for the original terminal demo, and is imported lazily there.
try:
    import pyaudio
    _PYAUDIO_AVAILABLE = True
except ImportError:
    pyaudio = None
    _PYAUDIO_AVAILABLE = False

load_dotenv()
selected_clinic = None

from aws_sdk_bedrock_runtime.client import BedrockRuntimeClient, InvokeModelWithBidirectionalStreamOperationInput
from aws_sdk_bedrock_runtime.models import InvokeModelWithBidirectionalStreamInputChunk, BidirectionalInputPayloadPart
from aws_sdk_bedrock_runtime.config import Config
from smithy_aws_core.identity.environment import EnvironmentCredentialsResolver

# Suppress warnings
warnings.filterwarnings("ignore")

# Audio configuration
INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000
CHANNELS = 1
FORMAT = pyaudio.paInt16 if _PYAUDIO_AVAILABLE else None
CHUNK_SIZE = 1024  # Number of frames per buffer

# Debug mode flag
DEBUG = False

def debug_print(message):
    """Print only if debug mode is enabled"""
    if DEBUG:
        functionName = inspect.stack()[1].function
        if  functionName == 'time_it' or functionName == 'time_it_async':
            functionName = inspect.stack()[2].function
        print('{:%Y-%m-%d %H:%M:%S.%f}'.format(datetime.datetime.now())[:-3] + ' ' + functionName + ' ' + message)

def time_it(label, methodToRun):
    start_time = time.perf_counter()
    result = methodToRun()
    end_time = time.perf_counter()
    debug_print(f"Execution time for {label}: {end_time - start_time:.4f} seconds")
    return result

async def time_it_async(label, methodToRun):
    start_time = time.perf_counter()
    result = await methodToRun()
    end_time = time.perf_counter()
    debug_print(f"Execution time for {label}: {end_time - start_time:.4f} seconds")
    return result

def _adapt_clinics_for_client(clinics_raw):
    """Map GoogleMapsService.find_nearby_clinics() results onto the Clinic
    shape the Next.js frontend renders (see types/index.ts on the frontend).

    Defensive .get() lookups with several possible key names are used here
    since this file doesn't redefine google_maps_service.py — if your
    service uses different field names, adjust the .get() fallbacks below.
    """
    adapted = []
    for i, clinic in enumerate(clinics_raw or []):
        adapted.append({
            "id": clinic.get("place_id") or clinic.get("id") or f"clinic-{i}",
            "name": clinic.get("name", "Clove Dental"),
            "address": clinic.get("address") or clinic.get("vicinity", ""),
            "distanceText": clinic.get("distance") or clinic.get("distanceText", ""),
            "rating": float(clinic.get("rating") or 0),
            "phone": clinic.get("phone") or clinic.get("phone_number", ""),
            "isOpenNow": bool(clinic.get("open_now") if clinic.get("open_now") is not None else clinic.get("isOpenNow", False)),
            "googleMapsUrl": clinic.get("googleMapsUrl") or clinic.get("maps_url", ""),
            "latitude": clinic.get("latitude") or clinic.get("lat", 0),
            "longitude": clinic.get("longitude") or clinic.get("lng", 0),
        })
    return adapted


def _adapt_booking_for_client(booking_raw, params):
    """Map book_appointment()'s return value onto the BookingConfirmationData
    shape the frontend renders. Falls back to the tool-call params for any
    field the booking function doesn't echo back.
    """
    clinic_name = booking_raw.get("clinic_name")
    if not clinic_name and selected_clinic:
        clinic_name = selected_clinic.get("name")

    return {
        "id": booking_raw.get("id") or booking_raw.get("event_id") or str(uuid.uuid4()),
        "patientName": booking_raw.get("patient_name") or params.get("patient_name", ""),
        "clinicName": clinic_name or "Clove Dental",
        "treatment": booking_raw.get("treatment") or params.get("treatment", ""),
        "date": booking_raw.get("date") or params.get("preferred_date", ""),
        "time": booking_raw.get("time") or params.get("preferred_time", ""),
        "status": "confirmed",
    }


class ToolProcessor:
    def __init__(self, on_client_event=None):
        # ThreadPoolExecutor could be used for complex implementations
        self.tasks = {}
        # Browser-reported lat/lng (see BedrockStreamManager.set_client_location),
        # used as a fallback when the model doesn't supply an explicit address.
        self.client_location = None
        # Optional async callback: (tool_name, tool_content, result) -> None.
        # Lets the WebSocket bridge relay structured tool results (clinics,
        # bookings, map links) to the browser without changing what the
        # tools themselves do below.
        self.on_client_event = on_client_event
    
    async def process_tool_async(self, tool_name, tool_content):
        """Process a tool call asynchronously and return the result"""
        # Create a unique task ID
        task_id = str(uuid.uuid4())
        
        # Create and store the task
        task = asyncio.create_task(self._run_tool(tool_name, tool_content))
        self.tasks[task_id] = task
        
        try:
            # Wait for the task to complete
            result = await task

            if self.on_client_event:
                try:
                    await self.on_client_event(tool_name, tool_content, result)
                except Exception as cb_err:
                    debug_print(f"on_client_event callback failed: {cb_err}")

            return result
        finally:
            # Clean up the task reference
            if task_id in self.tasks:
                del self.tasks[task_id]
    
    async def _run_tool(self, tool_name, tool_content):
        global selected_clinic
    
        print("===== TOOL NAME =====")
        print(tool_name)
    
        print("===== RAW TOOL CONTENT =====")
        print(tool_content)
    
        debug_print(f"Running tool: {tool_name}")
    
        tool = tool_name.lower()
    
        try:
            params = json.loads(tool_content.get("content", "{}"))
        except Exception:
            params = {}
    
        print("===== PARSED PARAMS =====")
        print(params)
    
        # ----------------------------------------------------
        # BOOK APPOINTMENT TOOL
        # ----------------------------------------------------
        if tool == "bookappointmenttool":
            try:
                print("ABOUT TO CALL book_appointment")
    
                response = book_appointment(
                    patient_name=params.get("patient_name", ""),
                    phone_number=params.get("phone_number", ""),
                    preferred_date=params.get("preferred_date", ""),
                    preferred_time=params.get("preferred_time", ""),
                    treatment=params.get("treatment", "")
                )
    
                print("BOOK_APPOINTMENT RETURNED")
                print(response)
    
                return response
    
            except Exception as e:
                import traceback
                traceback.print_exc()
    
                return {
                    "error": str(e)
                }
    
        # ----------------------------------------------------
        # FIND NEARBY CLINIC TOOL
        # ----------------------------------------------------
        elif tool == "findnearbyclinictool":
        
            print("GOOGLE MAPS TOOL CALLED")
    
            service = GoogleMapsService()

            # Prefer whatever the model extracted from speech; fall back to
            # the coordinates the browser sent via the "location" WS message.
            location_query = params.get("location") or self.client_location or ""

            clinics = service.find_nearby_clinics(
                address=location_query
            )
    
            print(clinics)
    
            if not clinics:
                return {
                    "message": "No nearby Clove Dental clinics found."
                }
    
            # Remember the nearest clinic
            selected_clinic = clinics[0]
    
            return {
                "clinics": clinics
            }
    
        # ----------------------------------------------------
        # OPEN GOOGLE MAPS TOOL
        # ----------------------------------------------------
        elif tool == "opengooglemapstool":
        
            if not selected_clinic:
                return {
                    "message": "No clinic has been selected yet."
                }
    
            url = selected_clinic.get("googleMapsUrl")
    
            if not url:
                return {
                    "message": "Google Maps link not available."
                }
    
            print("OPENING GOOGLE MAPS")
            print(url)
    
            webbrowser.open_new_tab(url)
    
            return {
                "message": "Google Maps has been opened."
            }
    
        # ----------------------------------------------------
        # UNKNOWN TOOL
        # ----------------------------------------------------
        else:
            return {
                "error": f"Unsupported tool: {tool_name}"
            }

class BedrockStreamManager:
    """Manages bidirectional streaming with AWS Bedrock using asyncio"""
    
    # Event templates
    START_SESSION_EVENT = '''{
        "event": {
            "sessionStart": {
            "inferenceConfiguration": {
                "maxTokens": 1024,
                "topP": 0.9,
                "temperature": 0.7
                }
            }
        }
    }'''

    CONTENT_START_EVENT = '''{
        "event": {
            "contentStart": {
            "promptName": "%s",
            "contentName": "%s",
            "type": "AUDIO",
            "interactive": true,
            "role": "USER",
            "audioInputConfiguration": {
                "mediaType": "audio/lpcm",
                "sampleRateHertz": 16000,
                "sampleSizeBits": 16,
                "channelCount": 1,
                "audioType": "SPEECH",
                "encoding": "base64"
                }
            }
        }
    }'''

    AUDIO_EVENT_TEMPLATE = '''{
        "event": {
            "audioInput": {
            "promptName": "%s",
            "contentName": "%s",
            "content": "%s"
            }
        }
    }'''

    TEXT_CONTENT_START_EVENT = '''{
        "event": {
            "contentStart": {
            "promptName": "%s",
            "contentName": "%s",
            "type": "TEXT",
            "role": "%s",
            "interactive": false,
                "textInputConfiguration": {
                    "mediaType": "text/plain"
                }
            }
        }
    }'''

    TEXT_INPUT_EVENT = '''{
        "event": {
            "textInput": {
            "promptName": "%s",
            "contentName": "%s",
            "content": "%s"
            }
        }
    }'''

    TOOL_CONTENT_START_EVENT = '''{
        "event": {
            "contentStart": {
                "promptName": "%s",
                "contentName": "%s",
                "interactive": false,
                "type": "TOOL",
                "role": "TOOL",
                "toolResultInputConfiguration": {
                    "toolUseId": "%s",
                    "type": "TEXT",
                    "textInputConfiguration": {
                        "mediaType": "text/plain"
                    }
                }
            }
        }
    }'''

    CONTENT_END_EVENT = '''{
        "event": {
            "contentEnd": {
            "promptName": "%s",
            "contentName": "%s"
            }
        }
    }'''

    PROMPT_END_EVENT = '''{
        "event": {
            "promptEnd": {
            "promptName": "%s"
            }
        }
    }'''

    SESSION_END_EVENT = '''{
        "event": {
            "sessionEnd": {}
        }
    }'''

    def start_prompt(self):
        """Create a promptStart event"""

        book_appointment_schema = json.dumps({
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
        })

        find_nearby_clinic_schema = json.dumps({
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "User's city, locality, landmark or address"
                }
            },
            "required": [
                "location"
            ]
        })

        open_google_maps_schema = json.dumps({
            "type": "object",
            "properties": {},
            "required": []
        })

        prompt_start_event = {
            "event": {
                "promptStart": {
                    "promptName": self.prompt_name,
                    "textOutputConfiguration": {
                        "mediaType": "text/plain"
                    },
                    "audioOutputConfiguration": {
                        "mediaType": "audio/lpcm",
                        "sampleRateHertz": 24000,
                        "sampleSizeBits": 16,
                        "channelCount": 1,
                        "voiceId": "matthew",
                        "encoding": "base64",
                        "audioType": "SPEECH"
                    },
                    "toolUseOutputConfiguration": {
                        "mediaType": "application/json"
                    },
                    "toolConfiguration": {
                        "tools": [
                            {
                                "toolSpec": {
                                    "name": "bookAppointmentTool",
                                    "description": "Books a dental appointment in Google Calendar after collecting and confirming all booking details.",
                                    "inputSchema": {
                                        "json": book_appointment_schema
                                    }
                                }
                            },
                            {
                                "toolSpec": {
                                    "name": "findNearbyClinicTool",
                                    "description": "Find nearby Clove Dental clinics using Google Maps. Use this whenever the user asks about nearby clinics, clinic locations, addresses, directions, travel time, distance or clinic timings.",
                                    "inputSchema": {
                                        "json": find_nearby_clinic_schema
                                    }
                                }
                            },
                            {
                                "toolSpec": {
                                    "name": "openGoogleMapsTool",
                                    "description": "Open Google Maps directions for the clinic that was previously selected. Use this when the user says 'open directions', 'navigate there', 'show me on Google Maps', 'open Google Maps', or 'take me there'.",
                                    "inputSchema": {
                                        "json": open_google_maps_schema
                                    }
                                }
                            }
                        ]
                    }
                }
            }
        }

        return json.dumps(prompt_start_event)
    
    def tool_result_event(self, content_name, content, role):
        """Create a tool result event"""

        if isinstance(content, dict):
            content_json_string = json.dumps(content)
        else:
            content_json_string = content
            
        tool_result_event = {
            "event": {
                "toolResult": {
                    "promptName": self.prompt_name,
                    "contentName": content_name,
                    "content": content_json_string
                }
            }
        }
        return json.dumps(tool_result_event)
   
    def __init__(self, model_id='amazon.nova-2-sonic-v1:0', region='us-east-1', event_callback=None):
        """Initialize the stream manager.

        event_callback: optional async callable, `await event_callback(dict)`.
        When provided (the FastAPI /ws/voice bridge does this), structured
        events — status changes, transcript lines, audio chunks, and tool
        results — are forwarded to it so a browser client can render them.
        Left as None, behavior is identical to the original CLI app.
        """
        self.model_id = model_id
        self.region = region
        
        # Replace RxPy subjects with asyncio queues
        self.audio_input_queue = asyncio.Queue()
        self.audio_output_queue = asyncio.Queue()
        self.output_queue = asyncio.Queue()
        
        self.response_task = None
        self.stream_response = None
        self.is_active = False
        self.barge_in = False
        self.bedrock_client = None
        
        # Audio playback components
        self.audio_player = None
        
        # Text response components
        self.display_assistant_text = False
        self.role = None

        # Session information
        self.prompt_name = str(uuid.uuid4())
        self.content_name = str(uuid.uuid4())
        self.audio_content_name = str(uuid.uuid4())
        self.toolUseContent = ""
        self.toolUseId = ""
        self.toolName = ""

        # Browser bridge -----------------------------------------------
        self.event_callback = event_callback
        self._last_status = None

        # Add a tool processor
        self.tool_processor = ToolProcessor(on_client_event=self._on_tool_client_event)
        
        # Add tracking for in-progress tool calls
        self.pending_tool_tasks = {}

    # ----------------------------------------------------------------
    # Browser bridge helpers (additive — CLI mode never calls these
    # since event_callback stays None there).
    # ----------------------------------------------------------------

    async def _emit(self, message: dict):
        """Forward a structured event to the attached browser client, if any."""
        if self.event_callback:
            try:
                await self.event_callback(message)
            except Exception as e:
                debug_print(f"event_callback failed: {e}")

    async def _emit_status(self, status: str):
        """Emit a status change, de-duplicated so the UI doesn't flicker."""
        if self._last_status != status:
            self._last_status = status
            await self._emit({"type": "status", "status": status})

    def set_client_location(self, latitude, longitude):
        """Store browser-reported coordinates for findNearbyClinicTool to
        fall back on when the model doesn't extract an explicit address."""
        self.tool_processor.client_location = f"{latitude},{longitude}"

    async def send_user_text_event(self, text: str):
        """Send a one-off free-text user turn (optional text-input fallback
        for browser clients that aren't using the microphone)."""
        content_name = str(uuid.uuid4())
        content_start = self.TEXT_CONTENT_START_EVENT % (self.prompt_name, content_name, "USER")
        content = self.TEXT_INPUT_EVENT % (self.prompt_name, content_name, json.dumps(text)[1:-1])
        content_end = self.CONTENT_END_EVENT % (self.prompt_name, content_name)
        for event in (content_start, content, content_end):
            await self.send_raw_event(event)

    async def _on_tool_client_event(self, tool_name, tool_content, result):
        """ToolProcessor callback: shape a completed tool's raw result into
        the JSON contract the Next.js frontend expects, and relay it.

        Nothing here changes what the tools return internally — it only
        adapts the existing return values (from GoogleMapsService,
        book_appointment, and the selected_clinic global) for the browser.
        """
        tool = tool_name.lower()

        try:
            params = json.loads(tool_content.get("content", "{}"))
        except Exception:
            params = {}

        if tool == "findnearbyclinictool":
            clinics_raw = result.get("clinics", []) if isinstance(result, dict) else []
            await self._emit({
                "type": "tool_result",
                "tool": "findNearbyClinicTool",
                "clinics": _adapt_clinics_for_client(clinics_raw),
            })

        elif tool == "bookappointmenttool":
            if isinstance(result, dict) and not result.get("error"):
                await self._emit({
                    "type": "tool_result",
                    "tool": "bookAppointmentTool",
                    "booking": _adapt_booking_for_client(result, params),
                })
            else:
                error_message = (result or {}).get("error", "Booking failed.") if isinstance(result, dict) else "Booking failed."
                await self._emit({"type": "error", "message": error_message})

        elif tool == "opengooglemapstool":
            url = selected_clinic.get("googleMapsUrl") if selected_clinic else None
            if url:
                await self._emit({
                    "type": "tool_result",
                    "tool": "openGoogleMapsTool",
                    "url": url,
                })

    def _initialize_client(self):
        """Initialize the Bedrock client."""
        config = Config(
            endpoint_uri=f"https://bedrock-runtime.{self.region}.amazonaws.com",
            region=self.region,
            aws_credentials_identity_resolver=EnvironmentCredentialsResolver(),
        )
        self.bedrock_client = BedrockRuntimeClient(config=config)
    
    async def initialize_stream(self):
        """Initialize the bidirectional stream with Bedrock."""
        if not self.bedrock_client:
            self._initialize_client()
        
        try:
            self.stream_response = await time_it_async("invoke_model_with_bidirectional_stream", lambda : self.bedrock_client.invoke_model_with_bidirectional_stream( InvokeModelWithBidirectionalStreamOperationInput(model_id=self.model_id)))
            self.is_active = True
            prompt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt.txt")

            with open(prompt_path, "r", encoding="utf-8") as f:
                default_system_prompt = json.dumps(f.read())[1:-1]
                
            
            # Send initialization events
            prompt_event = self.start_prompt()
            text_content_start = self.TEXT_CONTENT_START_EVENT % (self.prompt_name, self.content_name, "SYSTEM")
            text_content = self.TEXT_INPUT_EVENT % (self.prompt_name, self.content_name, default_system_prompt)
            text_content_end = self.CONTENT_END_EVENT % (self.prompt_name, self.content_name)
            
            init_events = [self.START_SESSION_EVENT, prompt_event, text_content_start, text_content, text_content_end]
            
            for event in init_events:
                await self.send_raw_event(event)
                # Small delay between init events
                await asyncio.sleep(0.1)
            
            # Start listening for responses
            self.response_task = asyncio.create_task(self._process_responses())
            
            # Start processing audio input
            asyncio.create_task(self._process_audio_input())
            
            # Wait a bit to ensure everything is set up
            await asyncio.sleep(0.1)
            
            debug_print("Stream initialized successfully")
            return self
        except Exception as e:
            self.is_active = False
            print(f"Failed to initialize stream: {str(e)}")
            raise
    
    async def send_raw_event(self, event_json):
        """Send a raw event JSON to the Bedrock stream."""
        if not self.stream_response or not self.is_active:
            debug_print("Stream not initialized or closed")
            return
       
        event = InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(bytes_=event_json.encode('utf-8'))
        )
        
        try:
            await self.stream_response.input_stream.send(event)
            # For debugging large events, you might want to log just the type
            if DEBUG:
                if len(event_json) > 200:
                    event_type = json.loads(event_json).get("event", {}).keys()
                    debug_print(f"Sent event type: {list(event_type)}")
                else:
                    debug_print(f"Sent event: {event_json}")
        except Exception as e:
            debug_print(f"Error sending event: {str(e)}")
            if DEBUG:
                import traceback
                traceback.print_exc()
    
    async def send_audio_content_start_event(self):
        """Send a content start event to the Bedrock stream."""
        content_start_event = self.CONTENT_START_EVENT % (self.prompt_name, self.audio_content_name)
        await self.send_raw_event(content_start_event)
    
    async def _process_audio_input(self):
        """Process audio input from the queue and send to Bedrock."""
        while self.is_active:
            try:
                # Get audio data from the queue
                data = await self.audio_input_queue.get()
                print("QUEUE ITEM RECEIVED")
                
                audio_bytes = data.get('audio_bytes')
                if not audio_bytes:
                    debug_print("No audio bytes received")
                    continue
                
                # Base64 encode the audio data
                blob = base64.b64encode(audio_bytes)
                audio_event = self.AUDIO_EVENT_TEMPLATE % (
                    self.prompt_name, 
                    self.audio_content_name, 
                    blob.decode('utf-8')
                )
                
                # Send the event
                print("SENDING AUDIO EVENT TO BEDROCK")
                await self.send_raw_event(audio_event)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                debug_print(f"Error processing audio: {e}")
                if DEBUG:
                    import traceback
                    traceback.print_exc()
    
    def add_audio_chunk(self, audio_bytes):
        """Add an audio chunk to the queue."""
        self.audio_input_queue.put_nowait({
            'audio_bytes': audio_bytes,
            'prompt_name': self.prompt_name,
            'content_name': self.audio_content_name
        })
    
    async def send_audio_content_end_event(self):
        """Send a content end event to the Bedrock stream."""
        if not self.is_active:
            debug_print("Stream is not active")
            return
        
        content_end_event = self.CONTENT_END_EVENT % (self.prompt_name, self.audio_content_name)
        await self.send_raw_event(content_end_event)
        debug_print("Audio ended")
    
    async def send_tool_start_event(self, content_name, tool_use_id):
        """Send a tool content start event to the Bedrock stream."""
        content_start_event = self.TOOL_CONTENT_START_EVENT % (self.prompt_name, content_name, tool_use_id)
        debug_print(f"Sending tool start event: {content_start_event}")  
        await self.send_raw_event(content_start_event)

    async def send_tool_result_event(self, content_name, tool_result):
        """Send a tool content event to the Bedrock stream."""
        # Use the actual tool result from processToolUse
        tool_result_event = self.tool_result_event(content_name=content_name, content=tool_result, role="TOOL")
        debug_print(f"Sending tool result event: {tool_result_event}")
        await self.send_raw_event(tool_result_event)
    
    async def send_tool_content_end_event(self, content_name):
        """Send a tool content end event to the Bedrock stream."""
        tool_content_end_event = self.CONTENT_END_EVENT % (self.prompt_name, content_name)
        debug_print(f"Sending tool content event: {tool_content_end_event}")
        await self.send_raw_event(tool_content_end_event)
    
    async def send_prompt_end_event(self):
        """Close the stream and clean up resources."""
        if not self.is_active:
            debug_print("Stream is not active")
            return
        
        prompt_end_event = self.PROMPT_END_EVENT % (self.prompt_name)
        await self.send_raw_event(prompt_end_event)
        debug_print("Prompt ended")
        
    async def send_session_end_event(self):
        """Send a session end event to the Bedrock stream."""
        if not self.is_active:
            debug_print("Stream is not active")
            return

        await self.send_raw_event(self.SESSION_END_EVENT)
        self.is_active = False
        debug_print("Session ended")
    
    async def _process_responses(self):
        """Process incoming responses from Bedrock."""
        try:            
            while self.is_active:
                try:
                    output = await self.stream_response.await_output()
                    result = await output[1].receive()
                    if result.value and result.value.bytes_:
                        try:
                            response_data = result.value.bytes_.decode('utf-8')
                            print(response_data)
                            json_data = json.loads(response_data)
                            
                            # Handle different response types
                            if 'event' in json_data:
                                if 'completionStart' in json_data['event']:
                                    debug_print(f"completionStart: {json_data['event']}")
                                elif 'contentStart' in json_data['event']:
                                    debug_print("Content start detected")
                                    content_start = json_data['event']['contentStart']
                                    # set role
                                    self.role = content_start['role']
                                    # Check for speculative content
                                    if 'additionalModelFields' in content_start:
                                        try:
                                            additional_fields = json.loads(content_start['additionalModelFields'])
                                            if additional_fields.get('generationStage') == 'SPECULATIVE':
                                                debug_print("Speculative content detected")
                                                self.display_assistant_text = True
                                            else:
                                                self.display_assistant_text = False
                                        except json.JSONDecodeError:
                                            debug_print("Error parsing additionalModelFields")
                                elif 'textOutput' in json_data['event']:
                                    text_content = json_data['event']['textOutput']['content']
                                    role = json_data['event']['textOutput']['role']
                                    # Check if there is a barge-in
                                    if '{ "interrupted" : true }' in text_content:
                                        debug_print("Barge-in detected. Stopping audio output.")
                                        self.barge_in = True
                                        await self._emit({"type": "audio_end"})
                                        await self._emit_status("listening")

                                    if (self.role == "ASSISTANT" and self.display_assistant_text):
                                        print(f"Assistant: {text_content}")
                                        await self._emit({
                                            "type": "transcript",
                                            "role": "assistant",
                                            "text": text_content,
                                            "isFinal": True,
                                        })
                                    elif (self.role == "USER"):
                                        print(f"User: {text_content}")
                                        await self._emit({
                                            "type": "transcript",
                                            "role": "user",
                                            "text": text_content,
                                            "isFinal": True,
                                        })
                                elif 'audioOutput' in json_data['event']:
                                    audio_content = json_data['event']['audioOutput']['content']
                                    audio_bytes = base64.b64decode(audio_content)
                                    await self.audio_output_queue.put(audio_bytes)
                                    # Browser clients play chunks directly off the
                                    # WebSocket — no local queue/pacing needed.
                                    await self._emit_status("speaking")
                                    await self._emit({"type": "audio_chunk", "data": audio_content})
                                elif 'toolUse' in json_data['event']:
                                    self.toolUseContent = json_data['event']['toolUse']
                                    self.toolName = json_data['event']['toolUse']['toolName']
                                    self.toolUseId = json_data['event']['toolUse']['toolUseId']
                                    debug_print(f"Tool use detected: {self.toolName}, ID: {self.toolUseId}")
                                    await self._emit_status("thinking")
                                elif 'contentEnd' in json_data['event'] and json_data['event'].get('contentEnd', {}).get('type') == 'TOOL':
                                    debug_print("Processing tool use and sending result")
                                     # Start asynchronous tool processing - non-blocking
                                    self.handle_tool_request(self.toolName, self.toolUseContent, self.toolUseId)
                                    debug_print("Processing tool use asynchronously")
                                elif 'contentEnd' in json_data['event']:
                                    debug_print("Content end")
                                elif 'completionEnd' in json_data['event']:
                                    # Handle end of conversation, no more response will be generated
                                    debug_print("End of response sequence")
                                    await self._emit({"type": "audio_end"})
                                    await self._emit_status("idle")
                                elif 'usageEvent' in json_data['event']:
                                    debug_print(f"UsageEvent: {json_data['event']}")
                            # Put the response in the output queue for other components
                            await self.output_queue.put(json_data)
                        except json.JSONDecodeError:
                            await self.output_queue.put({"raw_data": response_data})
                except StopAsyncIteration:
                    # Stream has ended
                    break
                except Exception as e:
                   # Handle ValidationException properly
                    if "ValidationException" in str(e):
                        error_message = str(e)
                        print(f"Validation error: {error_message}")
                    else:
                        print(f"Error receiving response: {e}")
                    break
                    
        except Exception as e:
            print(f"Response processing error: {e}")
        finally:
            self.is_active = False

    def handle_tool_request(self, tool_name, tool_content, tool_use_id):
        """Handle a tool request asynchronously"""
        # Create a unique content name for this tool response
        tool_content_name = str(uuid.uuid4())
        
        # Create an asynchronous task for the tool execution
        task = asyncio.create_task(self._execute_tool_and_send_result(
            tool_name, tool_content, tool_use_id, tool_content_name))
        
        # Store the task
        self.pending_tool_tasks[tool_content_name] = task
        
        # Add error handling
        task.add_done_callback(
            lambda t: self._handle_tool_task_completion(t, tool_content_name))
    
    def _handle_tool_task_completion(self, task, content_name):
        """Handle the completion of a tool task"""
        # Remove task from pending tasks
        if content_name in self.pending_tool_tasks:
            del self.pending_tool_tasks[content_name]
        
        # Handle any exceptions
        if task.done() and not task.cancelled():
            exception = task.exception()
            if exception:
                debug_print(f"Tool task failed: {str(exception)}")
    
    async def _execute_tool_and_send_result(self, tool_name, tool_content, tool_use_id, content_name):
        """Execute a tool and send the result"""
        try:
            debug_print(f"Starting tool execution: {tool_name}")
            
            # Process the tool - this doesn't block the event loop
            tool_result = await self.tool_processor.process_tool_async(tool_name, tool_content)
            
            # Send the result sequence
            await self.send_tool_start_event(content_name, tool_use_id)
            await self.send_tool_result_event(content_name, tool_result)
            await self.send_tool_content_end_event(content_name)
            
            debug_print(f"Tool execution complete: {tool_name}")
        except Exception as e:
            debug_print(f"Error executing tool {tool_name}: {str(e)}")
            # Try to send an error response if possible
            try:
                error_result = {"error": f"Tool execution failed: {str(e)}"}
                
                await self.send_tool_start_event(content_name, tool_use_id)
                await self.send_tool_result_event(content_name, error_result)
                await self.send_tool_content_end_event(content_name)
            except Exception as send_error:
                debug_print(f"Failed to send error response: {str(send_error)}")
    
    async def close(self):
        """Close the stream properly."""
        if not self.is_active:
            return
        
        # Cancel any pending tool tasks
        for task in self.pending_tool_tasks.values():
            task.cancel()

        if self.response_task and not self.response_task.done():
            self.response_task.cancel()

        await self.send_audio_content_end_event()
        await self.send_prompt_end_event()
        await self.send_session_end_event()

        if self.stream_response:
            await self.stream_response.input_stream.close()

class AudioStreamer:
    """Handles continuous microphone input and audio output using separate streams."""
    
    def __init__(self, stream_manager):
        if not _PYAUDIO_AVAILABLE:
            raise RuntimeError(
                "PyAudio is not installed. AudioStreamer is only used for the "
                "terminal/CLI demo (`python app2.py`). Browser clients use the "
                "FastAPI /ws/voice WebSocket endpoint instead (`uvicorn app2:app`), "
                "which does not require PyAudio."
            )

        self.stream_manager = stream_manager
        self.is_streaming = False
        self.loop = asyncio.get_event_loop()

        # Initialize PyAudio
        debug_print("AudioStreamer Initializing PyAudio...")
        self.p = time_it("AudioStreamerInitPyAudio", pyaudio.PyAudio)
        debug_print("AudioStreamer PyAudio initialized")

        # Initialize separate streams for input and output
        # Input stream with callback for microphone
        debug_print("Opening input audio stream...")
        self.input_stream = time_it("AudioStreamerOpenAudio", lambda  : self.p.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=INPUT_SAMPLE_RATE,
            input=True,
            frames_per_buffer=CHUNK_SIZE,
            stream_callback=self.input_callback
        ))
        debug_print("input audio stream opened")

        # Output stream for direct writing (no callback)
        debug_print("Opening output audio stream...")
        self.output_stream = time_it("AudioStreamerOpenAudio", lambda  : self.p.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=OUTPUT_SAMPLE_RATE,
            output=True,
            frames_per_buffer=CHUNK_SIZE
        ))

        debug_print("output audio stream opened")

    def input_callback(self, in_data, frame_count, time_info, status):
        """Callback function that schedules audio processing in the asyncio event loop"""
        if self.is_streaming and in_data:
            # Schedule the task in the event loop
            asyncio.run_coroutine_threadsafe(
                self.process_input_audio(in_data), 
                self.loop
            )
        return (None, pyaudio.paContinue)

    async def process_input_audio(self, audio_data):
        """Process a single audio chunk directly"""
        try:
            # Send audio to Bedrock immediately
            self.stream_manager.add_audio_chunk(audio_data)
        except Exception as e:
            if self.is_streaming:
                print(f"Error processing input audio: {e}")
    
    async def play_output_audio(self):
        """Play audio responses from Nova Sonic"""
        while self.is_streaming:
            try:
                # Check for barge-in flag
                if self.stream_manager.barge_in:
                    # Clear the audio queue
                    while not self.stream_manager.audio_output_queue.empty():
                        try:
                            self.stream_manager.audio_output_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    self.stream_manager.barge_in = False
                    # Small sleep after clearing
                    await asyncio.sleep(0.05)
                    continue
                
                # Get audio data from the stream manager's queue
                audio_data = await asyncio.wait_for(
                    self.stream_manager.audio_output_queue.get(),
                    timeout=0.1
                )
                
                if audio_data and self.is_streaming:
                    # Write directly to the output stream in smaller chunks
                    chunk_size = CHUNK_SIZE  # Use the same chunk size as the stream
                    
                    # Write the audio data in chunks to avoid blocking too long
                    for i in range(0, len(audio_data), chunk_size):
                        if not self.is_streaming:
                            break
                        
                        end = min(i + chunk_size, len(audio_data))
                        chunk = audio_data[i:end]
                        
                        # Create a new function that captures the chunk by value
                        def write_chunk(data):
                            return self.output_stream.write(data)
                        
                        # Pass the chunk to the function
                        await asyncio.get_event_loop().run_in_executor(None, write_chunk, chunk)
                        
                        # Brief yield to allow other tasks to run
                        await asyncio.sleep(0.001)
                    
            except asyncio.TimeoutError:
                # No data available within timeout, just continue
                continue
            except Exception as e:
                if self.is_streaming:
                    print(f"Error playing output audio: {str(e)}")
                    import traceback
                    traceback.print_exc()
                await asyncio.sleep(0.05)
    
    async def start_streaming(self):
        """Start streaming audio."""
        if self.is_streaming:
            return
        
        print("Starting audio streaming. Speak into your microphone...")
        print("Press Enter to stop streaming...")
        
        # Send audio content start event
        await time_it_async("send_audio_content_start_event", lambda : self.stream_manager.send_audio_content_start_event())
        
        self.is_streaming = True
        
        # Start the input stream if not already started
        if not self.input_stream.is_active():
            self.input_stream.start_stream()
        
        # Start processing tasks
        #self.input_task = asyncio.create_task(self.process_input_audio())
        self.output_task = asyncio.create_task(self.play_output_audio())
        
        # Wait for user to press Enter to stop
        await asyncio.get_event_loop().run_in_executor(None, input)
        
        # Once input() returns, stop streaming
        await self.stop_streaming()
    
    async def stop_streaming(self):
        """Stop streaming audio."""
        if not self.is_streaming:
            return
            
        self.is_streaming = False

        # Cancel the tasks
        tasks = []
        if hasattr(self, 'input_task') and not self.input_task.done():
            tasks.append(self.input_task)
        if hasattr(self, 'output_task') and not self.output_task.done():
            tasks.append(self.output_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Stop and close the streams
        if self.input_stream:
            if self.input_stream.is_active():
                self.input_stream.stop_stream()
            self.input_stream.close()
        if self.output_stream:
            if self.output_stream.is_active():
                self.output_stream.stop_stream()
            self.output_stream.close()
        if self.p:
            self.p.terminate()
        
        await self.stream_manager.close() 


# =============================================================================
# FastAPI browser bridge
# =============================================================================
# Everything above this point is the original Nova Sonic pipeline: Bedrock
# bidirectional streaming, tool calling (Google Maps / Calendar / Lambda via
# tools.py), and the PyAudio-based CLI demo. Nothing above was rewritten.
#
# `app` below is what `uvicorn app2:app --host 0.0.0.0 --port 8000` serves.
# It exposes one endpoint, /ws/voice, which replaces AudioStreamer's role
# for browser clients: it streams mic audio in from the WebSocket instead
# of a local microphone, and streams Nova's audio back out over the same
# WebSocket instead of writing to a speaker. The Nova Sonic pipeline
# (BedrockStreamManager) itself is reused unchanged, just driven by
# WebSocket messages instead of PyAudio callbacks.
# =============================================================================

app = FastAPI(title="Clove Dental Nova Voice Backend")

# Loosen this in production to your actual frontend origin(s).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health_check():
    """Simple liveness check the frontend's services/api.ts pings on load."""
    return {"status": "ok"}


class VoiceSession:
    """
    Bridges a single browser WebSocket connection to a single
    BedrockStreamManager (Nova Sonic) session — the browser-mode
    replacement for AudioStreamer. It never touches PyAudio; audio comes
    from and goes to the WebSocket instead of a local mic/speaker.

    Message contract (matches the Next.js frontend's types/index.ts):

    Client -> server:
      {"type": "audio_start"}
      {"type": "audio_chunk", "data": "<base64 16-bit PCM, 16kHz mono>"}
      {"type": "audio_end"}
      {"type": "location", "latitude": float, "longitude": float}
      {"type": "text_input", "text": "..."}
      {"type": "interrupt"}

    Server -> client:
      {"type": "status", "status": "connected" | "listening" | "thinking" | "speaking" | "idle"}
      {"type": "transcript", "role": "user" | "assistant", "text": "...", "isFinal": true}
      {"type": "audio_chunk", "data": "<base64 16-bit PCM, 24kHz mono>"}
      {"type": "audio_end"}
      {"type": "tool_result", "tool": "findNearbyClinicTool", "clinics": [...]}
      {"type": "tool_result", "tool": "bookAppointmentTool", "booking": {...}}
      {"type": "tool_result", "tool": "openGoogleMapsTool", "url": "..."}
      {"type": "error", "message": "..."}
    """

    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.stream_manager: "BedrockStreamManager | None" = None
        self._send_lock = asyncio.Lock()

    async def send_json(self, message: dict):
        """event_callback target passed into BedrockStreamManager."""
        async with self._send_lock:
            await self.websocket.send_text(json.dumps(message))

    async def run(self):
        self.stream_manager = BedrockStreamManager(
            model_id='amazon.nova-2-sonic-v1:0',
            region=os.environ.get("AWS_REGION", "us-east-1"),
            event_callback=self.send_json,
        )

        try:
            await self.stream_manager.initialize_stream()
            await self.send_json({"type": "status", "status": "connected"})

            while True:
                raw_message = await self.websocket.receive_text()

                try:
                    message = json.loads(raw_message)
                except json.JSONDecodeError:
                    debug_print(f"Ignoring malformed WS frame: {raw_message[:100]}")
                    continue

                msg_type = message.get("type")

                if msg_type == "audio_start":
                    print("\n========== AUDIO START ==========")

                    await self.stream_manager.send_audio_content_start_event()
                    await self.stream_manager._emit_status("listening")

                elif msg_type == "audio_chunk":
                    print("AUDIO CHUNK RECEIVED")
                    print("BASE64 LENGTH:", len(message.get("data", "")))

                    try:
                        audio_bytes = base64.b64decode(message.get("data", ""))
                        print("PCM BYTES:", len(audio_bytes))
                    except Exception as e:
                        print("DECODE FAILED:", e)
                        continue
                    
                    self.stream_manager.add_audio_chunk(audio_bytes)

                elif msg_type == "audio_end":
                    print("========== AUDIO END ==========\n")
                    await self.stream_manager.send_audio_content_end_event()


                elif msg_type == "location":
                    latitude = message.get("latitude")
                    longitude = message.get("longitude")
                    if latitude is not None and longitude is not None:
                        self.stream_manager.set_client_location(latitude, longitude)

                elif msg_type == "text_input":
                    text = message.get("text", "")
                    if text:
                        await self.stream_manager.send_user_text_event(text)

                elif msg_type == "interrupt":
                    self.stream_manager.barge_in = True

                else:
                    debug_print(f"Unknown message type from client: {msg_type}")

        except WebSocketDisconnect:
            debug_print("Browser client disconnected from /ws/voice")
        except Exception as e:
            print(f"VoiceSession error: {e}")
            if DEBUG:
                import traceback
                traceback.print_exc()
            try:
                await self.send_json({"type": "error", "message": str(e)})
            except Exception:
                pass
        finally:
            if self.stream_manager:
                await self.stream_manager.close()


@app.websocket("/ws/voice")
async def websocket_voice(websocket: WebSocket):
    """The endpoint the frontend connects to (ws://<host>:8000/ws/voice)."""
    await websocket.accept()
    session = VoiceSession(websocket)
    await session.run()


# =============================================================================
# Original PyAudio CLI demo (unchanged) — run with `python app2.py`
# =============================================================================


async def main(debug=False):
    """Main function to run the application."""
    global DEBUG
    DEBUG = debug

    # Create stream manager
    stream_manager = BedrockStreamManager(model_id='amazon.nova-2-sonic-v1:0', region='us-east-1')

    # Create audio streamer
    audio_streamer = AudioStreamer(stream_manager)

    # Initialize the stream
    await time_it_async("initialize_stream", stream_manager.initialize_stream)

    try:
        # This will run until the user presses Enter
        await audio_streamer.start_streaming()
        
    except KeyboardInterrupt:
        print("Interrupted by user")
    finally:
        # Clean up
        await audio_streamer.stop_streaming()
        

if __name__ == "__main__":
    # Terminal/CLI mode (original behavior, uses PyAudio + local mic/speaker):
    #     python app2.py [--debug]
    #
    # Browser mode (new — FastAPI + WebSocket, no PyAudio required):
    #     uvicorn app2:app --host 0.0.0.0 --port 8000
    import argparse
    
    parser = argparse.ArgumentParser(description='Nova Sonic Python Streaming')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    args = parser.parse_args()
    # Set your AWS credentials here or use environment variables
    # os.environ['AWS_ACCESS_KEY_ID'] = "AWS_ACCESS_KEY_ID"
    # os.environ['AWS_SECRET_ACCESS_KEY'] = "AWS_SECRET_ACCESS_KEY"
    # os.environ['AWS_DEFAULT_REGION'] = "us-east-1"

    # Run the main function
    try:
        asyncio.run(main(debug=args.debug))
    except Exception as e:
        print(f"Application error: {e}")
        if args.debug:
            import traceback
            traceback.print_exc()