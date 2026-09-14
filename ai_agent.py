"""
ai_agent.py -- AI-powered stuck-state recovery.
================================================

When the bot gets stuck (no progress for N seconds, repeated failures,
unknown screen state), this module lets an LLM look at the situation and
choose a recovery action from a fixed set of tools.

Supports two backends:
    - OpenAI (ChatGPT / GPT-4o) via the `openai` SDK
    - Google Gemini via the `google-generativeai` SDK

Only one backend needs to be installed.  The backend is selected by
config.AI_AGENT["provider"] ("openai" or "gemini").

How it plugs in
---------------
    1. main.py creates an AIAgent instance during session setup.
    2. During the common-case loop, a StuckDetector watches for stalled
       progress (no state changes for `stuck_timeout_seconds`).
    3. When the detector fires, it calls `agent.try_unstick(context)`.
    4. The agent grabs a screenshot, builds a prompt, lets the LLM pick
       a tool, executes it, and loops until the situation is resolved or
       the budget is exhausted.
    5. If the agent cannot resolve it, it escalates to a human via Discord.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import config

LOG = logging.getLogger("colourbot.ai_agent")


# ===========================================================================
# Stuck detector -- watches for stalled progress
# ===========================================================================

class StuckDetector:
    """Tracks bot activity and fires when progress stalls.

    "Progress" is any of:
        - target clicked
        - game event handled (smite, full invent, etc.)
        - state flag changed
        - valuable drop collected

    Call `heartbeat()` every time something meaningful happens.  When
    nothing fires for `timeout_seconds`, `is_stuck()` returns True.
    """

    def __init__(self, timeout_seconds: float = None, max_consecutive: int = 3):
        self.timeout = timeout_seconds or config.AI_AGENT["stuck_timeout_seconds"]
        self.max_consecutive = max_consecutive
        self._last_activity = time.monotonic()
        self._consecutive_stuck = 0
        self._actions_taken: List[str] = []
        self._intervention_history: List[str] = []  # persists across heartbeats

    def heartbeat(self) -> None:
        """Call when the bot makes real progress."""
        self._last_activity = time.monotonic()
        self._consecutive_stuck = 0
        self._actions_taken.clear()
        self._intervention_history.clear()

    def is_stuck(self) -> bool:
        """True when no activity for `timeout` seconds."""
        elapsed = time.monotonic() - self._last_activity
        return elapsed >= self.timeout

    def record_attempt(self, action: str) -> None:
        """Log that the AI agent tried something."""
        self._actions_taken.append(action)
        self._intervention_history.append(action)

    def bump_consecutive(self) -> int:
        """Increment consecutive stuck count; returns the new count."""
        self._consecutive_stuck += 1
        return self._consecutive_stuck

    def reset(self) -> None:
        self._last_activity = time.monotonic()
        self._consecutive_stuck = 0
        self._actions_taken.clear()

    @property
    def context_summary(self) -> str:
        """Human-readable summary of recent attempts."""
        parts = []
        if self._intervention_history:
            parts.append("Previous AI interventions this session: " +
                         " -> ".join(self._intervention_history[-5:]))
        if self._actions_taken:
            parts.append("This intervention: " +
                         ", ".join(self._actions_taken[-5:]))
        if self._consecutive_stuck > 1:
            parts.append(f"Consecutive stuck count: {self._consecutive_stuck}")
        return "; ".join(parts) if parts else "no AI recovery attempts yet"


# ===========================================================================
# Tool definitions
# ===========================================================================

# Each tool is a dict with:
#   name, description, parameters (JSON schema), handler (callable)

TOOL_SCHEMAS: List[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_game_state",
            "description": (
                "Get the current bot state: phase, flags, brew counter, "
                "detected regions (target, prayer, inventory, etc.), "
                "recent chat messages, and canvas dimensions. "
                "Use this FIRST to understand what's happening."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": (
                "Capture the game canvas and return it as a base64 image. "
                "Use this to visually inspect what's on screen."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click_at",
            "description": (
                "Click at specific canvas coordinates. Coordinates are "
                "relative to the game canvas (0,0 = top-left of the "
                "rendered game area). Use get_game_state to find known "
                "region positions first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {
                        "type": "integer",
                        "description": "X coordinate in canvas space",
                    },
                    "y": {
                        "type": "integer",
                        "description": "Y coordinate in canvas space",
                    },
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_key",
            "description": (
                "Press a keyboard key. Common keys: '2' (inventory tab), "
                "'4' (spellbook), 'j' (toggle run), 'insert' (screenshot), "
                "'`' (toggle chat), 'escape' (close interfaces), "
                "'shift' (hold for shift-clicks)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "The key to press",
                    },
                    "hold_seconds": {
                        "type": "number",
                        "description": "How long to hold the key (default 0.1)",
                        "default": 0.1,
                    },
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_color_blob",
            "description": (
                "Search the game canvas for the largest blob of a specific "
                "color. Returns center coordinates, bounds, and area. "
                "Available colors: red (target), yellow (prayer), "
                "blue (inventory anchor), orange (brews), cyan (pouch), "
                "white (necklaces), purple (player tile), black (teleport)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "color": {
                        "type": "string",
                        "description": "Color name to search for",
                        "enum": ["red", "yellow", "blue", "orange",
                                 "cyan", "white", "purple", "black"],
                    },
                },
                "required": ["color"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_chat",
            "description": (
                "OCR the chat area to read recent game messages. "
                "Returns any text visible in the chat prompt region."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait_and_observe",
            "description": (
                "Wait a few seconds and then describe what you see. "
                "Use this to check if the situation changed on its own."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "number",
                        "description": "Seconds to wait (max 10)",
                        "default": 3.0,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restart_session",
            "description": (
                "Abort the current session and restart the entire flow "
                "from the top (route replay + common case). Use this when "
                "the bot is in an unrecoverable state."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why the restart is needed",
                    },
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_to_human",
            "description": (
                "Send a Discord DM to the human operator and pause. "
                "Use this when you cannot determine what to do, or when "
                "the situation requires human judgment."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Message to send to the human",
                    },
                },
                "required": ["message"],
            },
        },
    },
]

# Gemini uses a different tool schema format
GEMINI_TOOL_SCHEMAS: List[dict] = [{
    "function_declarations": [
        {
            "name": tool["function"]["name"],
            "description": tool["function"]["description"],
            "parameters": tool["function"]["parameters"],
        }
        for tool in TOOL_SCHEMAS
    ]
}]


# ===========================================================================
# System prompt for the LLM
# ===========================================================================

SYSTEM_PROMPT = """\
You are an AI recovery agent for an Old School RuneScape (OSRS) color-based \
automation bot. Your job is to diagnose why the bot is stuck and choose the \
best recovery action.

The bot works by:
1. Replaying recorded routes (mouse/keyboard sequences)
2. Then entering a "common case" loop that clicks red highlight targets \
and handles game events (smite, full inventory, dodgy necklace breaks, \
shadow veil fading, valuable drops).

You have access to tools to inspect the screen state and take actions. \
Always follow this strategy:

1. Read the initial message carefully — it contains the bot state. \
If the state already tells you what's wrong, ACT IMMEDIATELY.
2. Only use screenshot/get_game_state/find_color_blob if the initial \
message doesn't explain the problem.
3. After taking an action, verify with one check, then either fix it \
or restart.
4. NEVER spend more than 3 rounds investigating before taking action.

CRITICAL RULES:
- If no red target is mentioned as missing AND prayer_active=True, you likely \
just need to restart_session. Do NOT waste rounds checking.
- If the initial message says "no red target" or the state shows the \
target is gone, call restart_session IMMEDIATELY on round 1.
- Do NOT call get_game_state if the initial message already contains \
the state. It wastes a round.
- Do NOT call screenshot unless you genuinely need to see the screen.

Key positions (canvas coordinates, approximate):
- Inventory tab: key '2'
- Spellbook tab: key '4'
- Prayer orb: varies, use find_color_blob("yellow")
- Inventory anchor: use find_color_blob("blue")
- Red target: use find_color_blob("red")

Common stuck scenarios and their fixes:
- Chat box open: press '`' to close it, then restart if red still missing
- Wrong interface open: press 'escape', then restart if red still missing
- Red target gone (no red blob found): restart_session IMMEDIATELY
- Player in wrong location: restart_session to replay the route
- Out of resources: escalate_to_human

Be decisive. The bot loses money every second it's stuck. \
A restart is almost always better than more investigation. \
If the intervention history shows a restart was already tried and the \
bot is stuck again, try a DIFFERENT action (e.g. press_key, click_at) \
or escalate_to_human instead of repeating the same thing.
"""


# ===========================================================================
# Tool executor -- bridges LLM tool calls to real bot actions
# ===========================================================================

class ToolExecutor:
    """Executes tool calls from the LLM against the live bot.

    This holds references to the live Vision/InputController/BotState and
    translates the AI's tool calls into real actions.
    """

    def __init__(self, vision, input_ctrl, state, clock, service=None):
        self.vision = vision
        self.input = input_ctrl
        self.state = state
        self.clock = clock
        self.service = service
        self._restart_requested = False
        self._escalation_message = None

    def execute(self, name: str, args: dict) -> str:
        """Run a tool and return a text description of the result."""
        try:
            handler = getattr(self, f"_tool_{name}", None)
            if handler is None:
                return f"Unknown tool: {name}"
            return handler(**args)
        except Exception as exc:
            LOG.error("tool %s failed: %s", name, exc)
            return f"Error executing {name}: {exc}"

    # -- individual tools ---------------------------------------------------

    def _tool_get_game_state(self) -> str:
        snap = self.state.snapshot()
        # Add canvas info if vision is available
        canvas_info = ""
        if self.vision is not None:
            w = self.vision.window.canvas
            canvas_info = f"\ncanvas: ({w.x},{w.y}) {w.w}x{w.h}"
        lines = [f"{k}: {v}" for k, v in snap.items()]
        return "\n".join(lines) + canvas_info

    def _tool_screenshot(self) -> str:
        """Capture the canvas and return it as a base64 image string."""
        if self.vision is None:
            return "Vision not available - cannot capture screenshot"
        img = self.vision.capture()
        # Convert to PIL then to base64 JPEG
        from PIL import Image
        pil_img = Image.fromarray(img)
        buf = io.BytesIO()
        pil_img.save(buf, format="JPEG", quality=80)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"[Screenshot captured: {img.shape[1]}x{img.shape[0]} pixels, base64 length: {len(b64)}]"

    def _tool_click_at(self, x: int, y: int) -> str:
        if self.input is None:
            return "InputController not available"
        self.input.move_and_click(x, y)
        return f"Clicked at canvas ({x}, {y})"

    def _tool_press_key(self, key: str, hold_seconds: float = 0.1) -> str:
        if self.input is None:
            return "InputController not available"
        self.input.tap(key, hold=f"common.after_target_click",
                       after="common.after_target_click",
                       note=f"AI agent pressed '{key}'")
        return f"Pressed key '{key}'"

    def _tool_find_color_blob(self, color: str) -> str:
        if self.vision is None:
            return "Vision not available"
        region = self.vision.largest_solid(color)
        if region is None:
            return f"No {color} blob found on screen"
        return (f"Found {color} blob: center={region.center}, "
                f"bounds=x{region.x_bounds} y{region.y_bounds}, "
                f"area={region.area}")

    def _tool_read_chat(self) -> str:
        if self.vision is None:
            return "Vision not available"
        from vision import ocr_mask, ocr_available
        if not ocr_available():
            return "Tesseract OCR not available"
        from core import Rect
        cfg = config.CHAT
        rect = Rect(*cfg["prompt_rect"])
        img = self.vision.capture()
        strip = self.vision.crop(img, rect)
        if strip.size == 0:
            return "Chat area is empty or not visible"
        mask = (strip.min(axis=2) >= cfg["prompt_white_threshold"])
        text = ocr_mask(mask, upscale=3, dilate=True, psm=7)
        return f"Chat text: {text!r}" if text else "No chat text visible"

    def _tool_wait_and_observe(self, seconds: float = 3.0) -> str:
        seconds = min(max(0.5, seconds), 10.0)
        self.clock.sleep(seconds)
        # After waiting, check what changed
        state_str = self._tool_get_game_state()
        return f"Waited {seconds:.1f}s. Current state:\n{state_str}"

    def _tool_restart_session(self, reason: str = "") -> str:
        LOG.warning("AI agent requested restart: %s", reason)
        self._restart_requested = True
        return f"Restart requested: {reason}"

    def _tool_escalate_to_human(self, message: str = "") -> str:
        LOG.warning("AI agent escalating to human: %s", message)
        self._escalation_message = message
        if self.service:
            self.service.notify_dm(f"AI agent escalation: {message}")
        return f"Escalated to human: {message}"

    @property
    def restart_requested(self) -> bool:
        return self._restart_requested

    @property
    def escalation_message(self) -> Optional[str]:
        return self._escalation_message


# ===========================================================================
# AIAgent -- the main entry point
# ===========================================================================

class AIAgent:
    """AI-powered stuck-state recovery.

    Usage:
        agent = AIAgent(state, clock, vision, input_ctrl, service)
        result = agent.try_unstick(context_description)

    Returns:
        "restarted"  - the session should restart
        "escalated"  - human was notified, wait for intervention
        "resolved"   - the agent believes the situation improved
        "gave_up"    - exhausted attempts without resolution
        "disabled"   - AI agent is turned off in config
    """

    def __init__(self, state, clock, vision=None, input_ctrl=None,
                 service=None, detector: StuckDetector = None):
        self.state = state
        self.clock = clock
        self.vision = vision
        self.input = input_ctrl
        self.service = service
        self.detector = detector or StuckDetector()
        self.cfg = config.AI_AGENT
        self.enabled = self.cfg["enabled"]
        self.max_tool_rounds = self.cfg["max_tool_rounds"]
        self._client = None
        self._provider = self.cfg["provider"]

    def _init_client(self):
        """Lazy-init the API client (only when first needed)."""
        if self._client is not None:
            return

        if self._provider == "openai":
            try:
                import openai
                self._client = openai.OpenAI(
                    api_key=self.cfg.get("openai_api_key"),
                )
                LOG.info("AI agent: OpenAI client initialized (model: %s)",
                         self.cfg["openai_model"])
            except ImportError:
                LOG.error("AI agent: openai package not installed. "
                          "Run: pip install openai")
                self.enabled = False
            except Exception as exc:
                LOG.error("AI agent: failed to init OpenAI: %s", exc)
                self.enabled = False

        elif self._provider == "gemini":
            try:
                from google import genai
                api_key = self.cfg.get("gemini_api_key", "")
                if not api_key:
                    raise ValueError("gemini_api_key is empty - set GEMINI_API_KEY in .env")
                self._client = genai.Client(api_key=api_key)
                LOG.info("AI agent: Gemini client initialized (model: %s)",
                         self.cfg["gemini_model"])
            except ImportError:
                LOG.error("AI agent: google-genai not installed. "
                          "Run: pip install google-genai")
                self.enabled = False
            except Exception as exc:
                LOG.error("AI agent: failed to init Gemini: %s", exc)
                self.enabled = False
        else:
            LOG.error("AI agent: unknown provider %r", self._provider)
            self.enabled = False

    def try_unstick(self, context: str = "") -> str:
        """Main entry point.  Diagnose and attempt to fix a stuck state.

        Args:
            context: Optional description of what triggered this (e.g.
                     "no activity for 60 seconds")

        Returns:
            One of: "resolved", "restarted", "escalated", "gave_up", "disabled"
        """
        if not self.enabled:
            return "disabled"

        self._init_client()
        if not self.enabled:
            return "disabled"

        consecutive = self.detector.bump_consecutive()
        if consecutive > self.cfg.get("max_consecutive_interventions", 5):
            LOG.warning("AI agent: %d consecutive stuck events, escalating",
                        consecutive)
            if self.service:
                self.service.notify_dm(
                    f"AI agent gave up after {consecutive} consecutive "
                    f"stuck events. Context: {context}")
            return "escalated"

        LOG.warning("AI agent: attempting recovery (attempt %d). %s",
                     consecutive, context or "no context")

        executor = ToolExecutor(
            self.vision, self.input, self.state, self.clock, self.service)

        # Build initial user message
        user_msg = self._build_initial_prompt(context)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ]

        exhausted = True
        for round_num in range(1, self.max_tool_rounds + 1):
            LOG.info("AI agent: round %d/%d", round_num, self.max_tool_rounds)

            response_text, tool_calls = self._call_llm(messages)
            if response_text:
                LOG.info("AI agent says: %s", response_text[:200])

            if not tool_calls:
                # LLM chose to just talk -- it might have concluded
                LOG.info("AI agent: no tool calls, ending interaction")
                exhausted = False
                break

            # Execute each tool call and build the tool response message
            tool_results = []
            for tc in tool_calls:
                name = tc["name"]
                args = tc.get("arguments", {})
                LOG.info("AI agent: calling tool %s(%s)", name,
                         json.dumps(args, default=str)[:100])
                result = executor.execute(name, args)
                tool_results.append({
                    "tool_call_id": tc.get("id", f"call_{name}"),
                    "name": name,
                    "result": result,
                })
                LOG.info("AI agent: tool %s -> %s", name, result[:150])

                self.detector.record_attempt(f"{name}({json.dumps(args, default=str)[:50]})")

                # Check for terminal actions
                if executor.restart_requested:
                    self.state.request_restart("AI agent decision")
                    return "restarted"
                if executor.escalation_message:
                    return "escalated"

            # Add the assistant message and tool results to the conversation
            assistant_msg = {
                "role": "assistant",
                "content": response_text or "",
                "tool_calls": [
                    {"id": tc.get("id", ""), "type": "function",
                     "function": {"name": tc["name"],
                                  "arguments": json.dumps(tc.get("arguments", {}))}}
                    for tc in tool_calls
                ],
            }
            # Stash raw Gemini parts to preserve thought_signature
            if hasattr(self, '_last_gemini_parts'):
                assistant_msg["_gemini_parts"] = self._last_gemini_parts
            messages.append(assistant_msg)
            for tr in tool_results:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tr["tool_call_id"],
                    "name": tr["name"],
                    "content": tr["result"],
                })

        if exhausted:
            LOG.warning("AI agent: exhausted all %d rounds without resolution",
                         self.max_tool_rounds)
            return "gave_up"
        LOG.info("AI agent: intervention complete")
        return "resolved"

    def _build_initial_prompt(self, context: str) -> str:
        """Build the first user message with current bot state."""
        parts = [f"The bot appears to be stuck. {context}" if context
                 else "The bot appears to be stuck."]

        # Add state snapshot
        try:
            snap = self.state.snapshot()
            parts.append(f"\nBot state:\n{json.dumps(snap, indent=2)}")
        except Exception:
            pass

        # Add canvas info
        if self.vision is not None:
            try:
                w = self.vision.window.canvas
                parts.append(f"\nCanvas: ({w.x},{w.y}) {w.w}x{w.h}")
            except Exception:
                pass

        # Add recent stuck history
        parts.append(f"\n{self.detector.context_summary}")

        parts.append("\nDiagnose the issue and take the minimum action to "
                     "recover. Start with get_game_state or screenshot.")
        return "\n".join(parts)

    def _call_llm(self, messages: List[dict]) -> Tuple[str, List[dict]]:
        """Call the LLM and return (text, tool_calls)."""
        if self._provider == "openai":
            return self._call_openai(messages)
        elif self._provider == "gemini":
            return self._call_gemini(messages)
        return "", []

    def _call_openai(self, messages: List[dict]) -> Tuple[str, List[dict]]:
        """OpenAI API call with function calling."""
        try:
            response = self._client.chat.completions.create(
                model=self.cfg["openai_model"],
                messages=messages,
                tools=TOOL_SCHEMAS,
                tool_choice="auto",
                max_tokens=500,
            )
            choice = response.choices[0]
            message = choice.message

            tool_calls = []
            if message.tool_calls:
                for tc in message.tool_calls:
                    args = {}
                    if tc.function.arguments:
                        try:
                            args = json.loads(tc.function.arguments)
                        except json.JSONDecodeError:
                            args = {}
                    tool_calls.append({
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": args,
                    })

            return message.content or "", tool_calls

        except Exception as exc:
            LOG.error("AI agent: OpenAI API error: %s", exc)
            return f"API error: {exc}", []

    def _call_gemini(self, messages: List[dict]) -> Tuple[str, List[dict]]:
        """Gemini API call with function calling (google-genai SDK)."""
        try:
            from google.genai import types

            # Build contents from message history, preserving raw Gemini objects
            # where available (needed for thought_signature on function calls).
            contents = []
            for msg in messages:
                role = msg["role"]
                if role == "system":
                    continue
                elif role == "user":
                    contents.append(types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=msg["content"])],
                    ))
                elif role == "assistant":
                    # Check if raw Gemini parts were stashed on the message
                    raw_parts = msg.get("_gemini_parts")
                    if raw_parts is not None:
                        contents.append(types.Content(role="model", parts=raw_parts))
                    else:
                        parts = []
                        if msg.get("content"):
                            parts.append(types.Part.from_text(text=msg["content"]))
                        for tc in msg.get("tool_calls", []):
                            fn = tc.get("function", {})
                            args = fn.get("arguments", {})
                            if isinstance(args, str):
                                args = json.loads(args) if args else {}
                            parts.append(types.Part.from_function_call(
                                name=fn.get("name", ""),
                                args=args,
                            ))
                        if parts:
                            contents.append(types.Content(role="model", parts=parts))
                elif role == "tool":
                    name = msg.get("name", "")
                    content = msg.get("content", "")
                    contents.append(types.Content(
                        role="user",
                        parts=[types.Part.from_function_response(
                            name=name,
                            response={"result": content},
                        )],
                    ))

            response = self._client.models.generate_content(
                model=self.cfg["gemini_model"],
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    tools=GEMINI_TOOL_SCHEMAS,
                ),
            )

            text = ""
            tool_calls = []
            raw_response_parts = []
            if response.candidates:
                candidate = response.candidates[0]
                for part in candidate.content.parts:
                    raw_response_parts.append(part)
                    if hasattr(part, "text") and part.text:
                        text += part.text
                    if hasattr(part, "function_call") and part.function_call:
                        fc = part.function_call
                        tool_calls.append({
                            "name": fc.name,
                            "arguments": dict(fc.args) if fc.args else {},
                        })

            # Stash raw parts so next round preserves thought_signature
            self._last_gemini_parts = raw_response_parts

            return text, tool_calls

        except Exception as exc:
            LOG.error("AI agent: Gemini API error: %s", exc)
            return f"API error: {exc}", []
