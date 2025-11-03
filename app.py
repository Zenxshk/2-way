import os, json, base64, requests, asyncio
from quart import Quart, request
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Start

app = Quart(__name__)

# ----------- CONFIG -----------
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.getenv("TWILIO_PHONE_NUMBER")
RAILWAY_URL = os.getenv("RAILWAY_URL")  # e.g. https://2-way-production.up.railway.app

ELEVEN_API_KEY = os.getenv("ELEVEN_API_KEY")
ELEVEN_VOICE_ID = os.getenv("ELEVEN_VOICE_ID", "H8bdWZHK2OgZwTN7ponr")

# ----------- TRIGGER CALL -----------
@app.post("/trigger-call")
async def trigger_call():
    data = await request.get_json()
    to_number = data.get("to")
    if not to_number:
        return {"error": "Missing 'to' number"}, 400

    client = Client(TWILIO_SID, TWILIO_AUTH)
    call = client.calls.create(
        to=to_number,
        from_=TWILIO_FROM,
        url=f"{RAILWAY_URL}/voice"
    )

    print(f"✅ Call triggered: {call.sid}")
    return {"status": "calling", "sid": call.sid}, 200


# ----------- TWILIO WEBHOOK (when call connects) -----------
@app.post("/voice")
async def voice():
    """When Twilio connects the call, this plays ElevenLabs audio."""
    print("☎️ Twilio webhook triggered — generating ElevenLabs greeting...")

    # 1️⃣ Generate audio using ElevenLabs API
    greeting_text = "Hello! This is UniCall AI assistant. How can I help you today?"
    audio_url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"
    headers = {"xi-api-key": ELEVEN_API_KEY, "Content-Type": "application/json"}
    payload = {
        "text": greeting_text,
        "voice_settings": {"stability": 0.4, "similarity_boost": 0.8}
    }

    # Make request to ElevenLabs
    r = requests.post(audio_url, headers=headers, json=payload)
    if r.status_code != 200:
        print(f"❌ ElevenLabs error: {r.status_code} {r.text}")
        # fallback: Twilio <Say> for debugging
        resp = VoiceResponse()
        resp.say("Sorry, the greeting failed to generate.")
        return str(resp), 200, {"Content-Type": "text/xml"}

    # 2️⃣ Save audio temporarily
    filename = "/tmp/greeting.mp3"
    with open(filename, "wb") as f:
        f.write(r.content)
    print(f"✅ Saved ElevenLabs greeting as {filename}")

    # 3️⃣ Twilio plays audio from a public URL — so serve it via your Railway app
    # We'll expose /greeting endpoint below
    resp = VoiceResponse()
    resp.play(f"{RAILWAY_URL}/greeting")
    return str(resp), 200, {"Content-Type": "text/xml"}


# ----------- SERVE THE AUDIO FILE -----------
@app.get("/greeting")
async def greeting():
    """Serve the ElevenLabs greeting audio file to Twilio."""
    from quart import send_file
    filename = "/tmp/greeting.mp3"
    if not os.path.exists(filename):
        return "Greeting not found", 404
    return await send_file(filename, mimetype="audio/mpeg")


@app.get("/")
async def home():
    return "🚀 Simple Twilio + ElevenLabs working!", 200


# ----------- ENTRY POINT -----------
if __name__ == "__main__":
    import hypercorn.asyncio
    from hypercorn.config import Config
    config = Config()
    config.bind = [f"0.0.0.0:{os.getenv('PORT', '8080')}"]
    config.use_reloader = False

    print("🚀 Running basic Twilio + ElevenLabs server...")
    asyncio.run(hypercorn.asyncio.serve(app, config))
