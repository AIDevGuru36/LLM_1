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
from pydub import AudioSegment

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

def stt_connect():
    stt_ws = websockets.connect(
        # Alter the protocol and base URL below.
        f'wss://api.deepgram.com/v1/listen?encoding=mulaw&sample_rate=8000',
        subprotocols=["token", DEEPGRAM_APIKEY]
    )
    return stt_ws

@app.websocket("/twilio/{incoming}")
async def websocket_endpoint(websocket: WebSocket, incoming: str):
    await websocket.accept()
    user_number = incoming
    print(f"Incoming call from: {user_number}")
    with open("log.txt", "a") as file:
        file.write(f"Incoming call from: {user_number}\n")

    outbox = asyncio.Queue()
    audio_queue = asyncio.Queue()
    streamsid_queue = asyncio.Queue()

    async with stt_connect() as stt_ws:
        # async def sender(ws):
        #     """ Sends the data, mimicking a real-time connection.
        #     """
        #     nonlocal data
        #     try:
        #         total = len(data)
        #         while len(data):
        #             # How many bytes are in `REALTIME_RESOLUTION` seconds of audio?
        #             i = int(byte_rate * REALTIME_RESOLUTION)
        #             chunk, data = data[:i], data[i:]
        #             # Send the data
        #             await ws.send(chunk)
        #             # Mimic real-time by waiting `REALTIME_RESOLUTION` seconds
        #             # before the next packet.
        #             await asyncio.sleep(REALTIME_RESOLUTION)

        #         # An empty binary message tells Deepgram that no more audio
        #         # will be sent. Deepgram will close the connection once all
        #         # audio has finished processing.
        #         await ws.send(b'')
        #     except Exception as e:
        #         print(f'Error while sending: {e}')
        #         raise

        async def sts_sender(sts_ws):
            print("sts_sender started")
            while True:
                chunk = await audio_queue.get()
                await sts_ws.send(chunk)

        async def stt_receiver(stt_ws):
            """ Print out the messages received from the server.
            """
            print("stt_receiver started")
            async for msg in stt_ws:
                res = json.loads(msg)
                print(res)
                try:
                    # To see interim results in this demo, remove the conditional `if res['is_final']:`.
                    if res['is_final']:
                        transcript = res['channel']['alternatives'][0]['transcript']
                        start = res['start']
                        print(f'{transcript}')
                except KeyError:
                    print(msg)

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

        async def client_receiver(client_ws):
            print('started client receiver')

			# directly outputting the audio to a file can be useful
			# the audio can be converted to a regular wav file via something like:
			# $ ffmpeg -f mulaw -ar 8000 -ac 2 -i mixed mixed.wav
            # file_inbound = open('inbound', 'wb')
            # file_outbound = open('outbound', 'wb')
            # file_mixed = open('mixed', 'wb')
            # file_manually_mixed = open('manually_mixed', 'wb')

			# we will use a buffer of 20 messages (20 * 160 bytes, 0.4 seconds) to improve throughput performance
			# NOTE: twilio seems to consistently send media messages of 160 bytes
            BUFFER_SIZE = 20 * 160
			# the algorithm to deal with mixing the two channels is ever so slightly sophisticated
			# I try here to implement an algorithm which fills in silence for channels if that channel is either
			#   A) not currently streaming (e.g. the outbound channel when the inbound channel starts ringing it)
			#   B) packets are dropped (this happens, and sometimes the timestamps which come back for subsequent packets are not aligned, I try to deal with this)
            inbuffer = bytearray(b'')
            outbuffer = bytearray(b'')
            empty_byte_received = False
            inbound_chunks_started = False
            outbound_chunks_started = False
            latest_inbound_timestamp = 0
            latest_outbound_timestamp = 0
            async for message in client_ws:
                try:
                    data = json.loads(message)
                    if data["event"] in ("connected", "start"):
                        print("Media WS: Received event connected or start")
                        continue
                    if data["event"] == "media":
                        media = data["media"]
                        chunk = base64.b64decode(media["payload"])
                        if media['track'] == 'inbound':
							# fills in silence if there have been dropped packets
                            if inbound_chunks_started:
                                if latest_inbound_timestamp + 20 < int(media['timestamp']):
                                    bytes_to_fill = 8 * (int(media['timestamp']) - (latest_inbound_timestamp + 20))
                                    print ('INBOUND WARNING! last timestamp was ' + str(latest_inbound_timestamp) + ' but current packet is for timestamp ' + media['timestamp'] + ', filling in ' + str(bytes_to_fill) + ' bytes of silence')
                                    inbuffer.extend(b"\xff" * bytes_to_fill) # NOTE: 0xff is silence for mulaw audio, and there are 8 bytes per ms of data for our format (8 bit, 8000 Hz)
                            else:
                                print ('started receiving inbound chunks!')
                                # make it known that inbound chunks have started arriving
                                inbound_chunks_started = True
                                latest_inbound_timestamp = int(media['timestamp'])
                                # this basically sets the starting point for outbound timestamps
                                latest_outbound_timestamp = int(media['timestamp']) - 20
                            latest_inbound_timestamp = int(media['timestamp'])
                            # extend the inbound audio buffer with data
                            inbuffer.extend(chunk)
                        if media['track'] == 'outbound':
                            # make it known that outbound chunks have started arriving
                            outbound_chunked_started = True
                            # fills in silence if there have been dropped packets
                            if latest_outbound_timestamp + 20 < int(media['timestamp']):
                                bytes_to_fill = 8 * (int(media['timestamp']) - (latest_outbound_timestamp + 20))
                                print ('OUTBOUND WARNING! last timestamp was ' + str(latest_outbound_timestamp) + ' but current packet is for timestamp ' + media['timestamp'] + ', filling in ' + str(bytes_to_fill) + ' bytes of silence')
                                outbuffer.extend(b"\xff" * bytes_to_fill) # NOTE: 0xff is silence for mulaw audio, and there are 8 bytes per ms of data for our format (8 bit, 8000 Hz)
                            latest_outbound_timestamp = int(media['timestamp'])
							# extend the outbound audio buffer with data
                            outbuffer.extend(chunk)
                        if chunk == b'':
                            empty_byte_received = True
                    if data["event"] == "stop":
                        print("Media WS: Received event stop")
                        break

					# check if our buffer is ready to send to our outbox (and, thus, then to deepgram)
                    while len(inbuffer) >= BUFFER_SIZE and len(outbuffer) >= BUFFER_SIZE or empty_byte_received:
                        if empty_byte_received:
                            break

                        print ( str(len(inbuffer)) + ' ' + str(len(outbuffer)) )
                        asinbound = AudioSegment(inbuffer[:BUFFER_SIZE], sample_width=1, frame_rate=8000, channels=1)
                        asoutbound = AudioSegment(outbuffer[:BUFFER_SIZE], sample_width=1, frame_rate=8000, channels=1)
                        mixed = AudioSegment.from_mono_audiosegments(asinbound, asoutbound)

						# if you don't have a nice library for mixing, you can always trivially manually mix the channels like so
                        # manually_mixed = bytearray(b'')
                        # for i in range(BUFFER_SIZE):
                        # 	manually_mixed.append(inbuffer[i])
                        # 	manually_mixed.append(outbuffer[i])

                        # file_inbound.write(asinbound.raw_data)
                        # file_outbound.write(asoutbound.raw_data)
                        # file_mixed.write(mixed.raw_data)
                        # file_manually_mixed.write(manually_mixed)

						# sending to deepgram
                        outbox.put_nowait(mixed.raw_data)
#						outbox.put_nowait(manually_mixed)

						# clearing buffers
                        inbuffer = inbuffer[BUFFER_SIZE:]
                        outbuffer = outbuffer[BUFFER_SIZE:]
                except:
                    print('message from client not formatted correctly, bailing')
                    break

			# if the empty byte was received, the async for loop should end, and we should here forward the empty byte to deepgram
			# or, if the empty byte was not received, but the WS connection to the client (twilio) died, then the async for loop will end and we should forward an empty byte to deepgram
            outbox.put_nowait(b'')
            print('finished client receiver')

            # file_inbound.close()
            # file_outbound.close()
            # file_mixed.close()
            # file_manually_mixed.close()

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
                asyncio.ensure_future(stt_receiver(stt_ws)),
                # asyncio.ensure_future(sts_sender(sts_ws)),
                # asyncio.ensure_future(sts_receiver(sts_ws)),
			    asyncio.ensure_future(client_receiver(stt_ws)),
                asyncio.ensure_future(twilio_receiver()),
            ]
        )

def run_websocket():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    run_websocket()