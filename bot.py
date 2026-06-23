import discord
import anthropic
import httpx
import base64
import json
import os
import re
from datetime import date

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
ai = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}

def get_bettors():
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/bettors?select=id,name", headers=HEADERS)
    return r.json()

def external_id_exists(external_id):
    r = httpx.get(
        f"{SUPABASE_URL}/rest/v1/bets?external_id=eq.{external_id}&select=id",
        headers=HEADERS
    )
    return len(r.json()) > 0

def insert_bet(data):
    r = httpx.post(
        f"{SUPABASE_URL}/rest/v1/bets",
        headers={**HEADERS, "Prefer": "return=minimal"},
        json=data
    )
    return r.status_code in (200, 201)

def detect_image_type(img_bytes):
    if img_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        return "image/png"
    elif img_bytes[:3] == b'\xff\xd8\xff':
        return "image/jpeg"
    elif img_bytes[:4] == b'RIFF' and img_bytes[8:12] == b'WEBP':
        return "image/webp"
    elif img_bytes[:6] in (b'GIF87a', b'GIF89a'):
        return "image/gif"
    return "image/png"

def parse_bets_with_claude(image_b64, image_type, bettors, username):
    bettor_list = ", ".join([b["name"] for b in bettors])
    prompt = f"""You are parsing a sports bet slip screenshot. There may be one or multiple bets visible.

The Discord user who posted this is: {username}
The registered bettors in the system are: {bettor_list}

Match the Discord username to the closest bettor name. If unsure, use the first bettor.

Extract ALL bets visible in the image and return ONLY a valid JSON array with no extra text, markdown, or explanation.

Return this exact structure (array even if only one bet):
[
  {{
    "bettor_name": "matched name from the bettor list",
    "sportsbook": "William Hill or Bovada or Other",
    "bet_type": "straight or parlay or teaser",
    "sport": "NFL or NBA or MLB or MMA or Soccer or NHL or Other",
    "description": "brief description of the bet",
    "odds": integer like -110 or 250 (American odds, no plus sign needed for positive),
    "stake": number like 25.00,
    "payout": number like 47.50,
    "result": "win or loss or push or pending",
    "bet_date": "{date.today().isoformat()}",
    "external_id": "the bet ID shown on the slip if visible, otherwise null"
  }}
]

Important rules:
- bet_type must be exactly one of: straight, parlay, teaser
- result should reflect what the slip shows (win/loss/push) or "pending" if unsettled
- external_id: look for any alphanumeric ID code printed on the slip (often in small text at the bottom). Extract it exactly as shown.
- If any field cannot be determined, use null for that field.
- Always return a JSON array, even for a single bet."""

    response = ai.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image_type,
                        "data": image_b64
                    }
                },
                {"type": "text", "text": prompt}
            ]
        }]
    )

    raw = response.content[0].text.strip()
    raw = re.sub(r"```json|```", "", raw).strip()
    return json.loads(raw)

@client.event
async def on_ready():
    print(f"BetBot is online as {client.user}")

@client.event
async def on_message(message):
    if message.author.bot:
        return

    if not message.attachments:
        return

    images = [a for a in message.attachments if a.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif'))]

    if not images:
        return

    await message.add_reaction("⏳")

    try:
        bettors = get_bettors()
        if not bettors:
            await message.channel.send("❌ No bettors found in database. Please check your Supabase setup.")
            await message.remove_reaction("⏳", client.user)
            return

        for attachment in images:
            img_bytes = await attachment.read()
            img_b64 = base64.b64encode(img_bytes).decode("utf-8")
            img_type = detect_image_type(img_bytes)

            bets_data = parse_bets_with_claude(img_b64, img_type, bettors, message.author.name)

            saved = 0
            skipped = 0
            failed = 0

            for bet_data in bets_data:
                external_id = bet_data.get("external_id")
                if external_id and external_id_exists(external_id):
                    skipped += 1
                    continue

                bettor = next((b for b in bettors if b["name"].lower() == bet_data.get("bettor_name", "").lower()), bettors[0])
                bet_data["bettor_id"] = bettor["id"]
                bet_data.pop("bettor_name", None)

                for field in ["stake", "payout"]:
                    if bet_data.get(field) is not None:
                        try:
                            bet_data[field] = float(bet_data[field])
                        except (ValueError, TypeError):
                            bet_data[field] = None

                if bet_data.get("odds") is not None:
                    try:
                        bet_data["odds"] = int(bet_data["odds"])
                    except (ValueError, TypeError):
                        bet_data["odds"] = None

                success = insert_bet(bet_data)
                if success:
                    saved += 1
                else:
                    failed += 1

            parts = []
            if saved > 0:
                parts.append(f"✅ **{saved} bet{'s' if saved > 1 else ''} logged**")
            if skipped > 0:
                parts.append(f"⏭️ {skipped} duplicate{'s' if skipped > 1 else ''} skipped")
            if failed > 0:
                parts.append(f"❌ {failed} failed to save")

            await message.channel.send("\n".join(parts) if parts else "❌ No bets found in image.")

    except json.JSONDecodeError:
        await message.channel.send("❌ Couldn't read the bet slip. Try a clearer screenshot.")
    except Exception as e:
        print(f"Error: {e}")
        await message.channel.send("❌ Something went wrong. Please try again.")
    finally:
        await message.remove_reaction("⏳", client.user)

client.run(DISCORD_TOKEN)
