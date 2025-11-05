import os, json, asyncio, base64, requests
from quart import Quart, request, websocket
from twilio.twiml.voice_response import VoiceResponse, Start
from twilio.rest import Client
import google.generativeai as genai
import azure.cognitiveservices.speech as speechsdk

# ---------- CONFIG ----------
AZURE_SPEECH_KEY = os.getenv("AZURE_SPEECH_KEY")
AZURE_SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION")
AZURE_SPEECH_ENDPOINT = os.getenv("AZURE_SPEECH_ENDPOINT")

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
        from_=os.getenv("TWILIO_FROM_NUMBER"),
        url=f"{os.getenv('RAILWAY_URL')}/voice"
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
    return "🚀 UniCall AI (Azure Streaming Version) is running!", 200


# ---------- μ-law → PCM16 Conversion (Pure Python) ----------
def convert_twilio_audio(raw_bytes: bytes) -> bytes:
    """
    Twilio streams 8kHz μ-law audio. Convert to 16-bit PCM (no external libs).
    """
    try:
        pcm16 = bytearray()
        for b in raw_bytes:
            b ^= 0xFF
            sign = b & 0x80
            exponent = (b >> 4) & 0x07
            mantissa = b & 0x0F
            magnitude = ((mantissa << 3) + 0x84) << exponent
            sample = -magnitude if sign else magnitude
            pcm16 += sample.to_bytes(2, "little", signed=True)
        return bytes(pcm16)
    except Exception as e:
        print("Audio conversion error:", e)
        return raw_bytes


# ---------- Azure Speech Streaming ----------
async def azure_transcribe_stream(audio_queue: asyncio.Queue, transcript_queue: asyncio.Queue):
    """
    Send Twilio audio to Azure Speech (streaming) and push transcripts to transcript_queue.
    """
    try:
        if AZURE_SPEECH_ENDPOINT:
            speech_config = speechsdk.SpeechConfig(endpoint=AZURE_SPEECH_ENDPOINT, subscription=AZURE_SPEECH_KEY)
        else:
            speech_config = speechsdk.SpeechConfig(subscription=AZURE_SPEECH_KEY, region=AZURE_SPEECH_REGION)

        speech_config.speech_recognition_language = "en-US"

        audio_format = speechsdk.audio.AudioStreamFormat(samples_per_second=8000, bits_per_sample=16, channels=1)
        push_stream = speechsdk.audio.PushAudioInputStream(stream_format=audio_format)
        audio_config = speechsdk.audio.AudioConfig(stream=push_stream)
        recognizer = speechsdk.SpeechRecognizer(speech_config=speech_config, audio_config=audio_config)

        def recognized_cb(evt):
            if evt.result.reason == speechsdk.ResultReason.RecognizedSpeech:
                text = evt.result.text.strip()
                if text:
                    print("Azure Transcript:", text)
                    try:
                        loop = asyncio.get_event_loop()
                        loop.call_soon_threadsafe(asyncio.create_task, transcript_queue.put(text))
                    except RuntimeError:
                        transcript_queue.put_nowait(text)

        def canceled_cb(evt):
            print("Azure recognition canceled:", evt)

        recognizer.recognized.connect(recognized_cb)
        recognizer.canceled.connect(canceled_cb)

        recognizer.start_continuous_recognition()

        while True:
            chunk = await audio_queue.get()
            if chunk is None:
                push_stream.close()
                recognizer.stop_continuous_recognition()
                await transcript_queue.put(None)
                break

            pcm_chunk = convert_twilio_audio(chunk)
            push_stream.write(pcm_chunk)

    except Exception as e:
        print("⚠ Azure Transcribe error:", e)
        await transcript_queue.put(None)


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
    payload = {"text": text, "voice_settings": {"stability": 0.4, "similarity_boost": 0.8}}
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
        azure_transcribe_stream(audio_queue, transcript_queue)
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
    print("🚀 Starting UniCall AI (Azure Streaming Version)...")
    asyncio.run(hypercorn.asyncio.serve(app, config))
