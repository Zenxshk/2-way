import os, json, asyncio, base64, requests
from quart import Quart, request, websocket
from twilio.twiml.voice_response import VoiceResponse, Start
from twilio.rest import Client
from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.handlers import TranscriptResultStreamHandler
from amazon_transcribe.model import AudioEvent

app = Quart(__name__)

# ---------- CONFIG ----------
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
ELEVEN_API_KEY = os.getenv("ELEVEN_API_KEY")
ELEVEN_VOICE_ID = os.getenv("ELEVEN_VOICE_ID", "H8bdWZHK2OgZwTN7ponr")

# ---------- OUTBOUND CALL ----------
@app.post("/trigger-call")
async def trigger_call():
    data = await request.get_json()
    to_number = data.get("to")
    if not to_number:
        return {"error": "Missing 'to' number"}, 400

    client = Client(
        os.getenv("TWILIO_ACCOUNT_SID"),
        os.getenv("TWILIO_AUTH_TOKEN"),
    )

    railway_url = os.getenv("RAILWAY_URL")
    call = client.calls.create(
        to=to_number,
        from_=os.getenv("TWILIO_PHONE_NUMBER"),
        url=f"{railway_url}/voice"
    )
    print(f"[✅ CALL TRIGGERED] SID: {call.sid}")
    return {"status": "calling", "sid": call.sid}, 200


# ---------- TWILIO WEBHOOK ----------
@app.post("/voice")
async def voice():
    """Twilio connects call → we start media stream."""
    print("[☎️ Twilio Voice Webhook Triggered]")
    resp = VoiceResponse()
    start = Start()
    ws_url = os.getenv("RAILWAY_URL").replace("https://", "wss://") + "/media"
    start.stream(url=ws_url)
    resp.append(start)
    # No <Say> — greeting will come from ElevenLabs.
    return str(resp), 200, {"Content-Type": "text/xml"}


@app.get("/")
async def home():
    return "🧩 Debug version of UniCall AI running!", 200


# ---------- AWS TRANSCRIBE ----------
class MyTranscriptHandler(TranscriptResultStreamHandler):
    def __init__(self, stream, output_queue):
        super().__init__(stream)
        self.output_queue = output_queue

    async def handle_transcript_event(self, transcript_event):
        for result in transcript_event.transcript.results:
            if result.is_partial:
                continue
            if result.alternatives:
                text = result.alternatives[0].transcript.strip()
                if text:
                    print(f"[🗣 USER SAID] {text}")
                    await self.output_queue.put(text)


async def aws_transcribe_stream(audio_queue, transcript_queue):
    """Send audio to AWS Transcribe and push transcripts."""
    try:
        print("[🔄 Starting AWS Transcribe Stream...]")
        client = TranscribeStreamingClient(region=AWS_REGION)
        stream = await client.start_stream_transcription(
            language_code="en-US",
            media_sample_rate_hz=8000,
            media_encoding="pcm",
        )

        handler = MyTranscriptHandler(stream.output_stream, transcript_queue)

        async def send_audio():
            while True:
                chunk = await audio_queue.get()
                if chunk is None:
                    print("[🛑 Ending Transcribe Audio Stream]")
                    await stream.input_stream.end_stream()
                    break
                await stream.input_stream.send_audio_event(audio_chunk=chunk)

        await asyncio.gather(send_audio(), handler.handle_events())
        print("[✅ AWS Transcribe Finished]")

    except Exception as e:
        print(f"[❌ AWS Transcribe ERROR] {e}")
        await transcript_queue.put(None)


# ---------- ElevenLabs SPEECH ----------
async def play_eleven_greeting(ws):
    """Send the first greeting TTS audio to Twilio."""
    text = "Hello! This is UniCall AI assistant. How can I help you today?"
    print("[🎙 Generating greeting from ElevenLabs...]")

    try:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"
        headers = {
            "xi-api-key": ELEVEN_API_KEY,
            "Content-Type": "application/json"
        }
        payload = {"text": text, "voice_settings": {"stability": 0.4, "similarity_boost": 0.8}}
        r = requests.post(url, headers=headers, json=payload)

        if r.status_code != 200:
            print(f"[❌ ElevenLabs Error] {r.status_code}: {r.text}")
            return

        audio_b64 = base64.b64encode(r.content).decode("utf-8")
        await ws.send(json.dumps({"event": "media", "media": {"payload": audio_b64}}))
        print("[✅ Greeting sent to Twilio]")
    except Exception as e:
        print(f"[❌ ElevenLabs Exception] {e}")


# ---------- WEBSOCKET ----------
@app.websocket("/media")
async def handle_twilio_media():
    print("[🌐 Twilio WebSocket Connected]")
    ws = websocket._get_current_object()

    audio_queue = asyncio.Queue()
    transcript_queue = asyncio.Queue()

    # Start AWS Transcribe listener
    transcribe_task = asyncio.create_task(aws_transcribe_stream(audio_queue, transcript_queue))

    # First, play ElevenLabs greeting
    await play_eleven_greeting(ws)
    await asyncio.sleep(0.5)

    async def consume_ws():
        """Receive audio from Twilio and push to AWS."""
        while True:
            try:
                msg = await ws.receive()
                if msg is None:
                    break
                data = json.loads(msg)
                event = data.get("event")

                if event == "media":
                    raw = base64.b64decode(data["media"]["payload"])
                    await audio_queue.put(raw)
                elif event == "stop":
                    print("[🛑 Twilio Media Stopped]")
                    await audio_queue.put(None)
                    await transcript_queue.put(None)
                    break
            except Exception as e:
                print(f"[❌ WS RECEIVE ERROR] {e}")
                break

    # Only printing user speech for now
    async def print_transcripts():
        while True:
            text = await transcript_queue.get()
            if text is None:
                break
            print(f"[🧠 TRANSCRIBED] {text}")

    await asyncio.gather(consume_ws(), print_transcripts(), transcribe_task)
    print("[🔚 Twilio Disconnected]")


# ---------- ENTRY ----------
if __name__ == "__main__":
    import hypercorn.asyncio
    from hypercorn.config import Config

    config = Config()
    config.bind = [f"0.0.0.0:{os.getenv('PORT', '8080')}"]
    config.use_reloader = False

    print("🚀 Running DEBUG UniCall AI server...")
    asyncio.run(hypercorn.asyncio.serve(app, config))
