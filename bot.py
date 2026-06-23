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
    prompt = f"""You are parsing a sports betting transaction history screenshot. Extract every completed bet and return them as a JSON array.

The Discord user who posted this is: {username}
The registered bettors in the system are: {bettor_list}
Match the Discord username to the closest bettor name. If unsure, use the first bettor.

=== BOVADA TRANSACTION HISTORY LAYOUT ===
Bovada's transaction history shows cards in a multi-column grid (2-3 columns side by side).
Each bet appears as TWO separate cards that must be combined into ONE bet entry:

CARD TYPE 1 - PLACED card (when bet was placed):
  - Shows "PLACED" and "TO WIN $X.XX" at the top
  - Shows the bet description (e.g. "Texas Rangers (+140)", "Over 2.5 (+122)")
  - Shows the game/match (e.g. "Texas Rangers @ Boston Red Sox")
  - Has Date and Time fields
  - Amount field shows a NEGATIVE number (e.g. -$2.00, -$8.00) = the stake
  - Total Balance field shows account balance after placing

CARD TYPE 2 - RESULT card (when bet settled):
  - Shows the bet name and result: e.g. "Texas Rangers (+140) Loss" or "Draw (+230) Loss"
  - Shows "LOSS" or "WIN" prominently
  - Has Date and Time fields
  - Amount field shows $0.00 for LOSS, or a positive dollar amount for WIN (= payout received)
  - Total Balance field shows account balance after settlement

HOW TO COMBINE THEM INTO ONE BET:
- Match a PLACED card to its RESULT card by the bet description (they describe the same bet)
- stake = the absolute value of the negative Amount from the PLACED card (e.g. -$2.00 → stake = 2.00)
- payout = the Amount from the WIN result card (e.g. +$2.80 → payout = 2.80). For LOSS = 0 payout.
- result = "win" if WIN card, "loss" if LOSS card
- bet_date = use the date from the PLACED card
- description = the bet description (e.g. "Texas Rangers (+140) vs Boston Red Sox")
- odds = extract from the bet name in parentheses e.g. (+140) → 140, (-115) → -115

PLACED-only cards with no matching RESULT card = result is "pending"
RESULT cards with no visible PLACED card = try to infer stake from context, or set stake=null and skip

ALSO VISIBLE IN THE SCREENSHOT may be partial cards, cut-off cards, or cards from different bets.
Carefully scan the ENTIRE image left to right, top to bottom, column by column.

=== WILLIAM HILL FORMAT ===
Shows a clean bet slip with:
- Cash Wagered = stake
- Paid = payout
- Result shown as WON/LOSS badge

=== OUTPUT FORMAT ===
Return ONLY a valid JSON array, no extra text, no markdown, no explanation.
If no valid bets can be extracted, return []

[
  {{
    "bettor_name": "matched name from bettor list",
    "sportsbook": "Bovada",
    "bet_type": "straight or parlay or teaser",
    "sport": "NFL or NBA or MLB or MMA or Soccer or NHL or Other",
    "description": "brief description e.g. Texas Rangers +140 vs Boston Red Sox",
    "odds": integer e.g. 140 or -115 (no plus sign needed for positive),
    "stake": number e.g. 2.00,
    "payout": number e.g. 2.80 (0 or null for losses),
    "result": "win or loss or push or pending",
    "bet_date": "YYYY-MM-DD from the PLACED card date",
    "external_id": null
  }}
]

Rules:
- Never include a bet with stake = 0 or stake = null (skip it)
- bet_type is almost always "straight" unless you can clearly see it is a parlay or teaser
- One JSON object per bet (PLACED + RESULT = one object, not two)
- Always return an array even for one bet"""

    response = ai.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
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
            await message.channel.send("❌ No bettors found in database. Please check your Supabase setup.")
            await message.remove_reaction("⏳", client.user)
            return

        total_saved = 0
        total_skipped = 0
        total_failed = 0

        for attachment in images:
            img_bytes = await attachment.read()
            img_b64 = base64.b64encode(img_bytes).decode("utf-8")
            img_type = detect_image_type(img_bytes)

            try:
                bets_data = parse_bets_with_claude(img_b64, img_type, bettors, message.author.name)
            except (json.JSONDecodeError, Exception) as e:
                print(f"Parse error on {attachment.filename}: {e}")
                total_failed += 1
                continue

            if not bets_data:
                total_skipped += 1
                continue

            for bet_data in bets_data:
                # Skip bets with no stake
                if not bet_data.get("stake"):
                    total_skipped += 1
                    continue

                # Check for duplicates via external_id
                external_id = bet_data.get("external_id")
                if external_id and external_id_exists(external_id):
                    total_skipped += 1
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
                    total_saved += 1
                else:
                    total_failed += 1

        parts = []
        if total_saved > 0:
            parts.append(f"✅ **{total_saved} bet{'s' if total_saved > 1 else ''} logged**")
        if total_skipped > 0:
            parts.append(f"⏭️ {total_skipped} skipped (duplicate, no stake, or unclear)")
        if total_failed > 0:
            parts.append(f"❌ {total_failed} image{'s' if total_failed > 1 else ''} couldn't be read — try uploading fewer bets per screenshot")

        if not parts:
            parts.append("❌ No valid bets found — make sure the screenshot shows full bet cards including the PLACED entries")

        await message.channel.send("\n".join(parts))

    except Exception as e:
        print(f"Error: {e}")
        await message.channel.send("❌ Something went wrong. Please try again.")
    finally:
        await message.remove_reaction("⏳", client.user)

client.run(DISCORD_TOKEN)
