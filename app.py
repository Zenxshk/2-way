import os, json, asyncio, base64, requests
from quart import Quart, request, websocket
from twilio.twiml.voice_response import VoiceResponse, Start
from twilio.rest import Client
import google.generativeai as genai
from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.handlers import TranscriptResultStreamHandler

# ---------- CONFIG ----------
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")  # us-east-1 supports Transcribe
ELEVEN_API_KEY = os.getenv("ELEVEN_API_KEY")
ELEVEN_VOICE_ID = os.getenv("ELEVEN_VOICE_ID", "H8bdWZHK2OgZwTN7ponr")
GEN_API_KEY = os.getenv("GOOGLE_API_KEY")
genai.configure(api_key=GEN_API_KEY)

SYSTEM_PROMPT = """
You are UniCall AI — a polite, knowledgeable virtual agent for Galaxy Auto Products.
Keep your answers short, natural, and helpful.
"""

app = Quart(__name__)

# ---------- Twilio outbound call trigger ----------
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

    call = client.calls.create(
        to=to_number,
        from_=os.getenv("TWILIO_PHONE_NUMBER"),  # MUST be +E.164 format, e.g. +14155551234
        url=f"{os.getenv('RAILWAY_URL')}/voice"  # use new var
    )
    print("CALL CREATED:", call.sid)
    return {"status": "calling", "sid": call.sid}, 200


# ---------- Twilio voice webhook ----------
@app.post("/voice")
async def voice():
    resp = VoiceResponse()
    start = Start()
    ws_url = os.getenv("RAILWAY_URL").replace("https://", "wss://") + "/media"
    start.stream(url=ws_url)
    resp.append(start)
    resp.say("Hello! You are connected to UniCall AI. Start speaking now.")
    return str(resp), 200, {"Content-Type": "text/xml"}


@app.get("/")
async def home():
    return "🚀 UniCall AI with Live AI Voice is running!", 200


# ---------- AWS Transcribe Streaming ----------
from amazon_transcribe.model import AudioEvent

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
                    print("AWS Transcript:", text)
                    await self.output_queue.put(text)

async def aws_transcribe_stream(audio_queue, transcript_queue):
    """Send audio to AWS Transcribe and push transcripts into transcript_queue."""
    client = TranscribeStreamingClient(region=os.getenv("AWS_REGION", "us-east-1"))

    # 👇 FIXED LINE HERE
    stream = await client.start_stream_transcription(
        language_code="en-US",
        media_sample_rate_hz=8000,
        media_encoding="pcm",
    )

    async with stream:
        handler = MyTranscriptHandler(stream.output_stream, transcript_queue)

        async def send_audio():
            while True:
                chunk = await audio_queue.get()
                if chunk is None:
                    await stream.input_stream.end_stream()
                    break
                await stream.input_stream.send_audio_event(audio_chunk=chunk)

        await asyncio.gather(send_audio(), handler.handle_events())



# ---------- LLM (Gemini) ----------
async def ask_ai(prompt: str) -> str:
    model = genai.GenerativeModel("gemini-1.5-flash")
    response = model.generate_content([SYSTEM_PROMPT, prompt])
    return response.text.strip() if response else "I'm not sure how to respond."


# ---------- ElevenLabs TTS ----------
async def synthesize_speech(text: str) -> bytes:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"
    headers = {
        "xi-api-key": ELEVEN_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {"text": text,
               "voice_settings": {"stability": 0.4, "similarity_boost": 0.8}}
    r = requests.post(url, headers=headers, json=payload)
    return r.content


# ---------- WebSocket handler ----------
@app.websocket("/media")
async def handle_twilio_media():
    print("[Twilio connected]")
    ws = websocket._get_current_object()

    audio_queue = asyncio.Queue()
    transcript_queue = asyncio.Queue()

    transcribe_task = asyncio.create_task(
        aws_transcribe_stream(audio_queue, transcript_queue)
    )

    async def consume_ws():
        """Receive audio from Twilio."""
        while True:
            msg = await ws.receive()
            if msg is None:
                break
            data = json.loads(msg)
            event = data.get("event")

            if event == "media":
                raw = base64.b64decode(data["media"]["payload"])
                await audio_queue.put(raw)
            elif event == "stop":
                await audio_queue.put(None)
                await transcript_queue.put(None)
                break

    async def consume_transcripts():
        """Process caller speech and respond with AI voice."""
        while True:
            text = await transcript_queue.get()
            if text is None:
                break

            print("Caller:", text)
            ai_reply = await ask_ai(text)
            print("AI:", ai_reply)

            # Convert text → speech (ElevenLabs)
            speech_bytes = await synthesize_speech(ai_reply)

            # Convert to base64 for Twilio
            audio_base64 = base64.b64encode(speech_bytes).decode("utf-8")

            # Send audio back to Twilio
            await ws.send(json.dumps({
                "event": "media",
                "media": {"payload": audio_base64}
            }))

            # Optional small delay to let audio play smoothly
            await asyncio.sleep(0.5)

    await asyncio.gather(consume_ws(), consume_transcripts(), transcribe_task)
    print("[Twilio disconnected]")

# ---------- Entry ----------
if __name__ == "__main__":
    import hypercorn.asyncio
    from hypercorn.config import Config

    config = Config()
    config.bind = [f"0.0.0.0:{os.getenv('PORT', '8080')}"]
    config.use_reloader = False
    print("🚀 Starting UniCall AI with full streaming pipeline...")
    asyncio.run(hypercorn.asyncio.serve(app, config))
