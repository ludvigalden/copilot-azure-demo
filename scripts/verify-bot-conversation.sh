#!/usr/bin/env bash
# Prove the registered bot end to end, headlessly: fetch the Direct Line
# site key for the bot, hold a real conversation over Direct Line, and
# assert the knowledge answer carries a citation. This is the same
# assertion a Teams or Web Chat conversation would show, and it is the
# scripted stand-in for the interactive check.
#
# Requires an az login with Microsoft.BotService/botServices/channels/
# listChannelWithKeys/action on the bot's resource group (Contributor or
# owner; Reader is not enough):
#
#   az login            # as demo@ (subscription Owner)
#   scripts/verify-bot-conversation.sh
#
# Set EXPECTED_CITATION to a source file name (e.g. printer.md) to
# additionally require that source to appear in the answer; unset, the
# generic citation check stands alone.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

command -v az >/dev/null || { echo "az is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

subscription="${AZURE_SUBSCRIPTION_ID:-654085ac-1de7-4f78-9a90-473a05012c34}"
resource_group="${BOT_RESOURCE_GROUP:-copaz-rg}"
bot_name="${BOT_NAME:-copaz-bot}"
question="${BOT_QUESTION:-How do I fix the office printer?}"
expected_citation="${EXPECTED_CITATION:-}"

az account show >/dev/null 2>&1 || { echo "run az login first" >&2; exit 1; }

echo "fetching the Direct Line key for $bot_name..."
key_json="$(az rest --method POST \
  --url "https://management.azure.com/subscriptions/$subscription/resourceGroups/$resource_group/providers/Microsoft.BotService/botServices/$bot_name/channels/DirectLineChannel/listChannelWithKeys?api-version=2023-09-15-preview")"
direct_line_key="$(printf '%s' "$key_json" | python3 -c '
import json
import sys

def find_keys(node):
    if isinstance(node, dict):
        if "key" in node and isinstance(node["key"], str):
            yield node["key"]
        for value in node.values():
            yield from find_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from find_keys(item)

keys = list(find_keys(json.load(sys.stdin)))
if not keys:
    sys.exit("no Direct Line key found in listChannelWithKeys response")
print(keys[0])
')"

DIRECT_LINE_KEY="$direct_line_key" BOT_QUESTION="$question" \
  EXPECTED_CITATION="$expected_citation" python3 <<'PY'
import json
import os
import sys
import time
import urllib.error
import urllib.request

key = os.environ["DIRECT_LINE_KEY"]
question = os.environ["BOT_QUESTION"]
expected_citation = os.environ.get("EXPECTED_CITATION", "")
base = "https://directline.botframework.com/v3/directline"


def request(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        },
    )
    # The container app scales to zero; the first delivery after an idle
    # period can outlive the Bot Framework connector's patience and come
    # back 502/503 while the app cold-starts. Retry those with backoff.
    for attempt in range(8):
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in (502, 503, 504) and attempt < 7:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    raise RuntimeError("unreachable")


def say(conversation_id, text):
    request(
        "POST",
        f"/conversations/{conversation_id}/activities",
        {
            "type": "message",
            "from": {"id": "verify-script", "name": "verify-script"},
            "text": text,
        },
    )


def collect(conversation_id, watermark, want, timeout=180):
    """Poll for activities past the watermark until want() holds or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = request("GET", f"/conversations/{conversation_id}/activities?watermark={watermark}")
        watermark = payload.get("watermark", watermark)
        replies = [
            a
            for a in payload.get("activities", [])
            if a.get("from", {}).get("id") != "verify-script" and a.get("text")
        ]
        if replies:
            return watermark, replies
        time.sleep(3)
    return watermark, []


print("starting a conversation...")
conversation = request("POST", "/conversations", {})
conversation_id = conversation["conversationId"]
watermark = 0

print("warm-up: hello (scales the container app from zero)")
say(conversation_id, "hello")
watermark, replies = collect(conversation_id, watermark, lambda r: True, timeout=300)
print("  bot:", replies[0]["text"][:100].replace("\n", " "))

print("knowledge:", question)
say(conversation_id, question)
watermark, replies = collect(conversation_id, watermark, lambda r: True, timeout=300)
answer = "\n".join(a["text"] for a in replies)
print("  bot:", answer[:400].replace("\n", " "))

cited = "Sources:" in answer or "[1]" in answer
if expected_citation:
    cited = cited and expected_citation in answer
print()
if expected_citation:
    print("cited answer (with %s):" % expected_citation,
          "PASS" if cited else "FAIL")
else:
    print("cited answer:", "PASS" if cited else "FAIL")
sys.exit(0 if cited else 1)
PY
