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

def parse_all_images(images_data, bettors, username):
    """
    Send ALL images from a message in a single API call so Claude can
    match PLACED cards to WIN/LOSS cards across multiple screenshots.
    images_data: list of (base64_str, media_type) tuples
    """
    bettor_list = ", ".join([b["name"] for b in bettors])

    # Build the content array — all images first, then the prompt
    content = []
    for i, (img_b64, img_type) in enumerate(images_data):
        content.append({
            "type": "text",
            "text": f"--- IMAGE {i+1} of {len(images_data)} ---"
        })
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img_type,
                "data": img_b64
            }
        })

    prompt = f"""You are parsing sports betting transaction screenshots. You have been given {len(images_data)} image(s) above.

The Discord user who posted these is: {username}
The registered bettors in the system are: {bettor_list}
Match the Discord username to the closest bettor name. If unsure, use the first bettor.

Your job is to extract every unique completed or pending bet across ALL images and return them as a single JSON array.

=== BOVADA FORMAT ===
Bovada transaction history shows two separate cards per bet:

PLACED card:
- Says "PLACED" and "TO WIN $X.XX" at top
- Shows bet description and game
- Amount = NEGATIVE number (e.g. -$3.00) = the stake
- This is what was wagered

WIN card:
- Says "WIN" in green
- Amount = POSITIVE number (e.g. +$8.20) = total payout received
- result = "win"
- stake = infer from payout and odds, OR find matching PLACED card

LOSS card:
- Says "LOSS" in red
- Amount = $0.00 always
- result = "loss"
- payout = 0
- stake MUST come from the matching PLACED card

CASH OUT card:
- Says "CASH OUT" 
- Amount = positive number = payout received
- result = "push" (treat cash outs as pushes)

MATCHING LOGIC — search across ALL images provided:
- Match a PLACED card to its WIN/LOSS card using the bet description (same team/game/odds)
- When matched: use PLACED card for stake and bet_date, use WIN/LOSS card for result and payout
- Only produce ONE bet entry per PLACED+WIN/LOSS pair
- If you see a PLACED card with no matching WIN/LOSS card anywhere in any image: result = "pending"
- If you see a LOSS card with no matching PLACED card anywhere in any image: set stake = null and skip it
- Never log both a PLACED card AND its WIN/LOSS card as separate bets

=== WILLIAM HILL FORMAT ===
Clean bet slip showing:
- Cash Wagered = stake
- Paid = payout  
- Result shown as WON/LOSS badge
- Log directly, no matching needed

=== OUTPUT ===
Return ONLY a valid JSON array, no markdown, no explanation, no extra text.
If no valid bets found, return []

[
  {{
    "bettor_name": "matched name from bettor list",
    "sportsbook": "Bovada or William Hill or Other",
    "bet_type": "straight or parlay or teaser",
    "sport": "NFL or NBA or MLB or MMA or Soccer or NHL or Other",
    "description": "brief description e.g. Texas Rangers +140 vs Boston Red Sox",
    "odds": integer e.g. 140 or -115,
    "stake": number e.g. 3.00,
    "payout": number e.g. 8.20 (0 for losses, null if unknown),
    "result": "win or loss or push or pending",
    "bet_date": "YYYY-MM-DD from the PLACED card date",
    "external_id": null
  }}
]

Rules:
- Never include a bet with stake = 0 or stake = null
- bet_type is "straight" unless clearly a parlay or teaser
- Always return an array even for one bet
- For parlays/SGPs, description should summarize the legs briefly"""

    content.append({"type": "text", "text": prompt})

    response = ai.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[{"role": "user", "content": content}]
    )

    raw = response.content[0].text.strip()
    raw = re.sub(r"```json|```", "", raw).strip()

    # Extract JSON array even if there's surrounding text
    match = re.search(r'\[.*\]', raw, re.DOTALL)
    if match:
        raw = match.group(0)

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
            await message.channel.send("❌ No bettors found in database.")
            await message.remove_reaction("⏳", client.user)
            return

        # Read all images first
        images_data = []
        for attachment in images:
            img_bytes = await attachment.read()
            img_b64 = base64.b64encode(img_bytes).decode("utf-8")
            img_type = detect_image_type(img_bytes)
            images_data.append((img_b64, img_type))

        # Send ALL images in one API call
        try:
            bets_data = parse_all_images(images_data, bettors, message.author.name)
        except (json.JSONDecodeError, Exception) as e:
            print(f"Parse error: {e}")
            await message.channel.send("❌ Couldn't parse the bet slips. Try uploading fewer images at once or clearer screenshots.")
            await message.remove_reaction("⏳", client.user)
            return

        if not bets_data:
            await message.channel.send("❌ No valid bets found. Make sure screenshots include PLACED cards alongside WIN/LOSS cards for Bovada.")
            await message.remove_reaction("⏳", client.user)
            return

        saved = 0
        skipped = 0
        failed = 0

        for bet_data in bets_data:
            # Skip bets with no stake
            if not bet_data.get("stake"):
                skipped += 1
                continue

            # Check for duplicates via external_id
            external_id = bet_data.get("external_id")
            if external_id and external_id_exists(external_id):
                skipped += 1
                continue

            bettor = next(
                (b for b in bettors if b["name"].lower() == bet_data.get("bettor_name", "").lower()),
                bettors[0]
            )
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
            parts.append(f"⏭️ {skipped} skipped (duplicate or no stake)")
        if failed > 0:
            parts.append(f"❌ {failed} failed to save")

        if not parts:
            parts.append("❌ No valid bets found.")

        # Add tip if multiple images were uploaded
        if len(images) > 1:
            parts.append(f"_(Processed {len(images)} images together)_")

        await message.channel.send("\n".join(parts))

    except Exception as e:
        print(f"Error: {e}")
        await message.channel.send("❌ Something went wrong. Please try again.")
    finally:
        await message.remove_reaction("⏳", client.user)

client.run(DISCORD_TOKEN)
