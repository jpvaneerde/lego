"""Original build ideas from Claude, grounded in the reviewed inventory."""
import base64

import anthropic

from . import config

IDEAS_TOOL = {
    "name": "submit_build_ideas",
    "description": "Return build ideas that can be made from the given parts.",
    "input_schema": {
        "type": "object",
        "properties": {
            "ideas": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "difficulty": {"type": "string", "enum": ["easy", "medium", "hard"]},
                        "description": {"type": "string", "description": "One or two sentences."},
                        "uses": {
                            "type": "array",
                            "description": "Parts from the inventory this build uses.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "part": {"type": "string"},
                                    "color": {"type": "string"},
                                    "qty": {"type": "integer"},
                                },
                                "required": ["part", "qty"],
                            },
                        },
                        "steps": {"type": "array", "items": {"type": "string"},
                                  "description": "4-10 short building steps."},
                    },
                    "required": ["title", "difficulty", "description", "uses", "steps"],
                },
            }
        },
        "required": ["ideas"],
    },
}

SYSTEM = (
    "You are an experienced LEGO builder helping someone build from a pile of loose bricks. "
    "Only use parts that are in the inventory, never more than the quantity available. "
    "Prefer builds that are recognisable and fun, and that use a good share of the pile. "
    "Steps should be concrete (which parts go where), short, and in build order."
)


def available() -> bool:
    return bool(config.ANTHROPIC_API_KEY)


def generate(parts: list[dict], photo_jpeg: bytes | None, audience: str = "", count: int = 4) -> list[dict]:
    lines = [f"- {p['qty']}x {p.get('part_name') or p['part_num']} ({p['part_num']})"
             f", {p.get('color_name') or 'unknown colour'}" for p in parts]
    text = (f"Inventory ({sum(int(p['qty']) for p in parts)} pieces):\n" + "\n".join(lines) +
            f"\n\nSuggest {count} different things to build, from quick to more ambitious.")
    if audience:
        text += f"\nBuilder: {audience[:200]}"
    content = []
    if photo_jpeg:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(photo_jpeg).decode()}})
        text += "\nThe photo shows the pile; the inventory list is authoritative."
    content.append({"type": "text", "text": text})

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=4000,
        system=SYSTEM,
        tools=[IDEAS_TOOL],
        tool_choice={"type": "tool", "name": IDEAS_TOOL["name"]},
        messages=[{"role": "user", "content": content}],
    )
    for block in msg.content:
        if block.type == "tool_use":
            return block.input.get("ideas", [])
    return []
