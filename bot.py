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

def insert_bet(data):
    r = httpx.post(
        f"{SUPABASE_URL}/rest/v1/bets",
        headers={**HEADERS, "Prefer": "return=minimal"},
        json=data
    )
    return r.status_code in (200, 201)

def parse_bet_with_claude(image_b64, image_type, bettors, username):
    bettor_list = ", ".join([b["name"] for b in bettors])
    prompt = f"""You are parsing a sports bet slip screenshot.

The Discord user who posted this is: {username}
The registered bettors in the system are: {bettor_list}

Match the Discord username to the closest bettor name. If unsure, use the first bettor.

Extract all bet details and return ONLY a valid JSON object with no extra text, markdown, or explanation.

Return this exact structure:
{{
  "bettor_name": "matched name from the bettor list",
  "sportsbook": "William Hill or Bovada or Other",
  "bet_type": "straight or parlay or teaser",
  "sport": "NFL or NBA or MLB or MMA or Soccer or NHL or Other",
  "description": "brief description of the bet",
  "odds": integer like -110 or 250 (American odds, no plus sign needed for positive),
  "stake": number like 25.00,
  "payout": number like 47.50,
  "result": "pending",
  "bet_date": "{date.today().isoformat()}"
}}

If any field cannot be determined from the image, use null for that field.
bet_type must be exactly one of: straight, parlay, teaser
result should always be "pending" for new bets."""

    response = ai.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
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

    image_types = ("image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif")
    images = [a for a in message.attachments if a.content_type and a.content_type.split(";")[0] in image_types]

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
            img_type = attachment.content_type.split(";")[0]

            bet_data = parse_bet_with_claude(img_b64, img_type, bettors, message.author.name)

            bettor = next((b for b in bettors if b["name"].lower() == bet_data["bettor_name"].lower()), bettors[0])
            bet_data["bettor_id"] = bettor["id"]
            bet_data.pop("bettor_name", None)

            for field in ["odds", "stake", "payout"]:
                if bet_data.get(field) is not None:
                    try:
                        bet_data[field] = float(bet_data[field]) if field != "odds" else int(bet_data[field])
                    except (ValueError, TypeError):
                        bet_data[field] = None

            success = insert_bet(bet_data)

            if success:
                desc = bet_data.get("description") or "bet"
                stake = f"${bet_data['stake']:.2f}" if bet_data.get("stake") else "unknown stake"
                odds = f"{'+' if (bet_data.get('odds') or 0) > 0 else ''}{bet_data.get('odds')}" if bet_data.get("odds") else "unknown odds"
                await message.channel.send(
                    f"✅ **{bettor['name']}** — {desc}\n"
                    f"📊 {bet_data.get('bet_type', 'straight').capitalize()} · {bet_data.get('sport', '')} · {odds} · {stake}"
                )
            else:
                await message.channel.send("❌ Failed to save bet to database. Please try again.")

    except json.JSONDecodeError:
        await message.channel.send("❌ Couldn't read the bet slip. Try a clearer screenshot.")
    except Exception as e:
        print(f"Error: {e}")
        await message.channel.send("❌ Something went wrong. Please try again.")
    finally:
        await message.remove_reaction("⏳", client.user)

client.run(DISCORD_TOKEN)
