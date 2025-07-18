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
import re
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

DEEPGRAM_TTS_URL = "wss://api.deepgram.com/v1/speak"
DEEPGRAM_STT_URL = "wss://api.deepgram.com/v1/listen?encoding=mulaw&sample_rate=8000"
DEEPGRAM_LLM_URL = "https://api.deepgram.com/v1/chat/completions"

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

    response = VoiceResponse()
    connect = Connect()
    connect.stream(url=stream_url)  # Your WebSocket server
    response.append(connect)
    print(str(response))
    return HTMLResponse(content=str(response), status_code=200)

async def handle_twilio_stream(websocket: WebSocket, user_number: str):
    async with websockets.connect(
        DEEPGRAM_STT_URL, subprotocols=["token", DEEPGRAM_APIKEY]) as dg_ws:

        async def forward_audio():
            async for message in websocket:
                event = json.loads(message)
                if event["event"] == "media":
                    payload = base64.b64decode(event["media"]["payload"])
                    await dg_ws.send(payload)

        async def receive_transcripts():
            async for msg in dg_ws:
                data = json.loads(msg)
                if "channel" in data:
                    transcript = data["channel"]["alternatives"][0]["transcript"]
                    if transcript and data.get("is_final"):
                        print("User:", transcript)
                        response = await get_llm_response(transcript)
                        print("Bot:", response)

                        match = re.search(r"\[SMS\](.*?)\[/SMS\]", response, re.DOTALL)
                        if match:
                            sms_content = match.group(1)
                            print(f"📩 Sending SMS: {sms_content}")
                            if user_number:
                                send_sms(user_number, sms_content)
                        else:
                            audio = await synthesize_speech(response)
                            await send_audio(websocket, audio)

        await asyncio.gather(forward_audio(), receive_transcripts())

async def get_llm_response(user_input):
    headers = {
        "Authorization": f"Token {DEEPGRAM_APIKEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "nova-2",
        "messages": [
            {
                "role": "system",
                "content": 
                    "You're a friendly and professional car sales representative."
                    "You have access to our latest car listings: "
                    "- Toyota Corolla 2022, blue, $18,000"
                    "- Honda Civic 2023, red, $21,000"
                    "- Ford Mustang 2021, black, $25,000"
                    "Always act as a real human, never reveal or mention that you are an AI or virtual assistant."
                    "When the conversation starts, begin with a friendly welcome message before the user speaks."
                    "Start the conversation first by saying: 'Hi! This is Ashley. How can I help you today?' "
                    "Then ask for their name before moving forward."
                    "If the user asks to receive something by text or SMS, include the message to send within [SMS] tag and [/SMS] tag."
                    "Be engaging, patient, and helpful — just like a real dealership staff member."
            },
            {"role": "user", "content": user_input}
        ]
    }
    res = requests.post(DEEPGRAM_LLM_URL, headers=headers, json=payload)
    print(f"LLM response: {res}")
    return res.json()["choices"][0]["message"]["content"]

async def synthesize_speech(text):
    headers = {
        "Authorization": f"Token {DEEPGRAM_APIKEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "text": text,
        "voice": "aura-orion-en",
        "encoding": "linear16",
        "sample_rate": 8000
    }
    res = requests.post(DEEPGRAM_TTS_URL, headers=headers, json=payload)
    return res.content  # Raw audio bytes

async def send_audio(websocket, audio_bytes):
    # Split into 20ms chunks (~320 bytes for 8kHz, 16-bit mono)
    chunk_size = 320
    for i in range(0, len(audio_bytes), chunk_size):
        chunk = audio_bytes[i:i+chunk_size]
        base64_chunk = base64.b64encode(chunk).decode("utf-8")
        await websocket.send(json.dumps({
            "event": "media",
            "media": {"payload": base64_chunk}
        }))
        await asyncio.sleep(0.02)  # 20ms

@app.websocket("/twilio/{incoming}")
async def websocket_endpoint(websocket: WebSocket, incoming: str):
    await websocket.accept()
    user_number = incoming
    print(f"Incoming call from: {user_number}")
    with open("log.txt", "a") as file:
        file.write(f"Incoming call from: {user_number}\n")
    
    await handle_twilio_stream(websocket, user_number)

def run_websocket():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    run_websocket()