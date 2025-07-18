'''
    https://github.com/deepgram-devs/sts-twilio/blob/main/server.py
    https://developers.deepgram.com/docs/twilio-and-deepgram-voice-agent
'''
import os
import asyncio
import base64
import json
import sys
import threading
from urllib.parse import parse_qs, urlparse
import websockets
import ssl
import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from fastapi import HTTPException
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from dotenv import load_dotenv

load_dotenv()
DEEPGRAM_APIKEY = os.getenv('DEEPGRAM_APIKEY')
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
def send_sms(to_number, body):
    try:
        message = twilio_client.messages.create(
            body=body,
            from_=TWILIO_PHONE_NUMBER,
            to=to_number
        )
        print(f"✅ SMS sent to {to_number}! SID: {message.sid}")
    except Exception as e:
        print(f"❌ Error sending SMS: {e}")

def get_ngrok_url():
    try:
        tunnels = requests.get("http://127.0.0.1:4040/api/tunnels").json()
        for tunnel in tunnels["tunnels"]:
            if tunnel["proto"] == "https":
                return tunnel["public_url"]
    except Exception as e:
        print("⚠️ Couldn't get ngrok URL:", e)
        return None

app = FastAPI()

@app.get("/")
def index():
    return "FastAPI is running!"

@app.post("/voice")
async def voice(request: Request):
    # from_number = request.get("From")
    form = await request.form()
    from_number = form.get("From")
    if not from_number:
        raise HTTPException(status_code=400, detail="Missing 'From' field")
    # stream_url = f"wss://your-domain.com/twilio?from={from_number}"
    ngrok_url = get_ngrok_url()
    stream_url = f"{ngrok_url.replace('https', 'wss')}/twilio/{from_number}"
    print(f"ngrok stream {stream_url}")

    # response = f"""
    # <?xml version="1.0" encoding="UTF-8"?>
    # <Response>
    #     <Say language="en">Hello! Connecting you to our assistant.</Say>
    #     <Connect>
    #         <Stream url="{stream_url}" />
    #     </Connect>
    # </Response>
    # """
    # return Response(response, mimetype="text/xml")

    response = VoiceResponse()
    connect = Connect()
    connect.stream(url=stream_url)  # Your WebSocket server
    response.append(connect)
    print(str(response))
    return HTMLResponse(content=str(response), status_code=200)

def sts_connect():
    sts_ws = websockets.connect(
    "wss://agent.deepgram.com/agent", subprotocols=["token", DEEPGRAM_APIKEY]
    )
    return sts_ws

@app.websocket("/twilio/{incoming}")
async def websocket_endpoint(websocket: WebSocket, incoming: str):
    await websocket.accept()
    user_number = incoming
    # print(websocket.url)
    # print(websocket.query_params)
    # print(websocket.path_params)
    # print(websocket.scope.get("scheme"))
    # print(websocket.scope.get("server")[0])
    # print(websocket.scope.get("server")[1])
    # print(websocket.scope.get("path"))
    # print(websocket.scope.get("query_string").decode())
    # user_number = websocket.query_params.get("from")
    print(f"Incoming call from: {user_number}")
    with open("log.txt", "a") as file:
        file.write(f"Incoming call from: {user_number}\n")
    # query_params = parse_qs(websocket.scope["query_string"].decode())
    # print(json.dumps(query_params))

    audio_queue = asyncio.Queue()
    streamsid_queue = asyncio.Queue()

    async with sts_connect() as sts_ws:
        config_message = {
            "type": "SettingsConfiguration",
            "audio": {
                "input": {
                    "encoding": "mulaw",
                    "sample_rate": 8000,
                },
                "output": {
                    "encoding": "mulaw",
                    "sample_rate": 8000,
                    "container": "none",
                },
            },
            "agent": {
                "listen": {"model": "nova-2"},
                "think": {
                    "provider": {
                        "type": "anthropic",
                    },
                    "model": "claude-3-haiku-20240307",
                    "instructions": (
                        "You're a friendly and professional car sales representative."
                        "You have access to our latest car listings: "
                        "- Toyota Corolla 2022, blue, $18,000"
                        "- Honda Civic 2023, red, $21,000"
                        "- Ford Mustang 2021, black, $25,000"
                        "Always act as a real human, never reveal or mention that you are an AI or virtual assistant."
                        "When the conversation starts, begin with a friendly welcome message before the user speaks."
                        "Start the call proactively by greeting the customer without waiting. "
                        "Start the conversation first by saying: 'Hi! This is Ashley. How can I help you today?' "
                        "Then ask for their name before moving forward."
                        f"User's phone number is {user_number}."
                        "If the user asks to receive something by text or SMS, respond naturally like, "
                        f"'Sure, I’ll text that over to you {user_number} now!' "
                        "Do not ask the number to send to."
                        "And include the message to send within [I sent] tag and [done] tag. Do not say the SMS content aloud."
                        "Do not speak the content within [I sent] tag and [done] tag including tags aloud — it's for internal use only."
                        "Never say you can't send messages or texts. "
                        "Be engaging, patient, and helpful — just like a real dealership staff member."
                    )
                },
                "speak": {
                    "model": "aura-orion-en"
                },
            },
        }

        await sts_ws.send(json.dumps(config_message))
        SILENT_CHUNK = b'\xff' * 160  # 20ms of silence
        for _ in range(5):
            await sts_ws.send(SILENT_CHUNK)
        # await sts_ws.send(json.dumps({
        #     "type": "ConversationCommand",
        #     "command": {
        #         "type": "agent_speak",
        #         "text": "Hello! This is Ashley. How can I help you?"
        #     }
        # }))

        async def sts_sender(sts_ws):
            print("sts_sender started")
            while True:
                chunk = await audio_queue.get()
                await sts_ws.send(chunk)

        async def sts_receiver(sts_ws):
            print("sts_receiver started")
            # we will wait until the twilio ws connection figures out the streamsid
            streamsid = await streamsid_queue.get()
            # for each sts result received, forward it on to the call
            sms_active = False
            sms_buffer = []
            async for message in sts_ws:
                if type(message) is str:
                    print(message)
                    with open("log.txt", "a") as file:
                        file.write(f"{message}\n")
                    # handle barge-in
                    decoded = json.loads(message)
                    
                    if decoded.get("type") == "Transcript":
                        transcript = decoded["channel"]["alternatives"][0]["transcript"]
                        if transcript:
                            print(f"🔹 User said: {transcript}")  # ✅ Print the transcript
                            # (Optional) Store in a variable or send to another service
                    if decoded['type'] == 'ConversationText':
                        content = decoded["content"]
                        # if "intent:send_sms" in content.lower():
                        #     if user_number:
                        #         send_sms(user_number, "Here is the car info you requested! 🚗")
                        #     else:
                        #         print("⚠️ No number available to send SMS.")
                        if "[I sent]" in content:
                            sms_active = True
                            content = content.split("[I sent]", 1)[1]  # Remove tag but keep content
                        if sms_active:
                            sms_buffer.append(content)
                        if "[done]" in content:
                            sms_active = False
                            # Clean up and extract text
                            combined_sms = " ".join(sms_buffer)
                            combined_sms = combined_sms.split("[done]", 1)[0].strip()
                            sms_buffer.clear()

                            print(f"📩 Sending SMS: {combined_sms}")
                            if user_number:
                                send_sms(user_number, combined_sms)

                    if decoded['type'] == 'UserStartedSpeaking':
                        clear_message = {
                            "event": "clear",
                            "streamSid": streamsid
                        }
                        await websocket.send_text(json.dumps(clear_message))
                    continue

                # print(type(message))
                raw_mulaw = message

                # construct a Twilio media message with the raw mulaw (see https://www.twilio.com/docs/voice/twiml/stream#websocket-messages---to-twilio)
                media_message = {
                    "event": "media",
                    "streamSid": streamsid,
                    "media": {"payload": base64.b64encode(raw_mulaw).decode("ascii")},
                }

                # send the TTS audio to the attached phonecall
                await websocket.send_text(json.dumps(media_message))

        async def twilio_receiver():
            print("twilio_receiver started")
            # twilio sends audio data as 160 byte messages containing 20ms of audio each
            # we will buffer 20 twilio messages corresponding to 0.4 seconds of audio to improve throughput performance
            BUFFER_SIZE = 20 * 160

            inbuffer = bytearray(b"")
            async for message in websocket.iter_text():
                try:
                    data = json.loads(message)
                    if data["event"] == "start":
                        print("got our streamsid")
                        start = data["start"]
                        streamsid = start["streamSid"]
                        await streamsid_queue.put(streamsid)
                    if data["event"] == "connected":
                        continue
                    if data["event"] == "media":
                        media = data["media"]
                        chunk = base64.b64decode(media["payload"])
                        if media["track"] == "inbound":
                            inbuffer.extend(chunk)
                    if data["event"] == "stop":
                        break

                    # check if our buffer is ready to send to our audio_queue (and, thus, then to sts)
                    while len(inbuffer) >= BUFFER_SIZE:
                        chunk = inbuffer[:BUFFER_SIZE]
                        await audio_queue.put(chunk)
                        inbuffer = inbuffer[BUFFER_SIZE:]
                except:
                    break

        # the async for loop will end if the ws connection from twilio dies
        # and if this happens, we should forward an some kind of message to sts
        # to signal sts to send back remaining messages before closing(?)
        # audio_queue.put_nowait(b'')

        await asyncio.wait(
            [
                asyncio.ensure_future(sts_sender(sts_ws)),
                asyncio.ensure_future(sts_receiver(sts_ws)),
                asyncio.ensure_future(twilio_receiver()),
            ]
        )

def run_websocket():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    run_websocket()