"""The models AgentComputerUse thinks with, all free.

- Plans, re-plans and double-checks: Flash-Lite first (500 a day), then Gemma 4 26B and other free routes. No Flash.
- Steps: Flash-Lite (500 a day), then other free routes. Each step is one request.
- A second pair of eyes when stuck: gemma3:4b on the Mini (local, unlimited, about 25 s an answer). Measured
  2026-10-05 on screen questions: right on "seeing" questions, wrong at choosing and at dense grids. So it's
  only ever asked seeing questions, and its answers are hints the tool checks, never decisions.

Uses AlwaysOnAgent's model client (core/llm.py): its free-tier failover, cooldowns and usage log, with the
keys from the agent's .env. Every request is labelled "computer" in the agent's model-health numbers.
"""
import base64
import json
import os
import re
import sys
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

AOA_DIR = Path(os.environ.get("ACU_AOA_DIR") or r"C:\Projects\AlwaysOnAgent")
# Flash-Lite first, like the agent's chat (core/llm.py CHAT_MODELS), and no Flash at all: 2026-10-08, every live
# run lost minutes to 3.7/3.6 Flash being overloaded (503) or not answering within 45 s, and those ~20-a-day
# models are the chat's. Flash-Lite has 500 a day; a run uses about 15 requests.
PLAN_MODELS = ("gemini:gemini-3.5-flash-lite", "gemini:gemini-3.1-flash-lite", "gemini:gemma-4-26b-a4b-it",
               "groq:openai/gpt-oss-120b", "cloudflare:@cf/openai/gpt-oss-120b")
STEP_MODELS = ("gemini:gemini-3.5-flash-lite", "gemini:gemini-3.1-flash-lite", "gemini:gemma-4-26b-a4b-it",
               "groq:openai/gpt-oss-120b", "cloudflare:@cf/openai/gpt-oss-120b")
LOCAL_EYES = os.environ.get("ACU_LOCAL_EYES", "gemma3:4b")
OLLAMA = os.environ.get("ACU_OLLAMA", "http://127.0.0.1:11434")


class BrainError(Exception):
    pass


def parse_json(text: str) -> dict[str, Any]:
    """The JSON object in a model's reply (tolerates ```json fences and text around it)."""
    if not text:
        raise BrainError("empty reply")
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        out = json.loads(t)
    except ValueError:
        # Read the FIRST object only. A reply holding two objects back to back ({...}\n{...}) made json.loads
        # and the old greedy {.*} both raise "Extra data", which ended the whole run (task-231, 2026-10-26).
        decoder = json.JSONDecoder()
        out = None
        start = t.find("{")
        while start != -1 and out is None:
            try:
                out, _ = decoder.raw_decode(t, start)
            except ValueError:
                start = t.find("{", start + 1)
        if out is None:
            raise BrainError(f"no JSON in the reply: {text[:120]!r}")
    if not isinstance(out, dict):
        raise BrainError("the reply isn't a JSON object")
    return out


class Brain:
    def __init__(self, llm=None):
        self.requests = 0
        self.local_requests = 0
        self._llm = llm

    @property
    def llm(self):
        if self._llm is None:
            if str(AOA_DIR) not in sys.path:
                sys.path.insert(0, str(AOA_DIR))
            from core.llm import CloudLLM  # also loads the agent's .env (the keys)
            self._llm = CloudLLM()
            try:
                from core.database import DatabaseManager
                from core.llm_usage import UsageLog
                self._llm.usage_log = UsageLog(DatabaseManager())
            except Exception:
                pass                                 # usage numbers are nice to have, not required
        return self._llm

    def ask(self, messages: list[dict[str, str]], models: Sequence[str], max_tokens: int = 700) -> dict[str, Any]:
        self.requests += 1
        llm = self.llm                               # loads the agent's code first (sys.path)
        try:
            from core.llm_usage import usage_purpose
            ctx = usage_purpose("computer")
        except Exception:
            import contextlib
            ctx = contextlib.nullcontext()
        with ctx:
            reply = llm.chat(messages, max_tokens=max_tokens, temperature=0.2, json_mode=True, timeout=45,
                                  models=list(models))
        if reply is None:
            raise BrainError("no model answered (all free routes failed or are cooling down)")
        return parse_json(reply)

    def plan(self, messages):
        return self.ask(messages, PLAN_MODELS, max_tokens=900)

    def check(self, messages):
        """A double-check's verdict: a yes/no and one sentence, so a short reply."""
        return self.ask(messages, PLAN_MODELS, max_tokens=200)

    def step(self, messages):
        return self.ask(messages, STEP_MODELS, max_tokens=600)

    # ------------------------------------------------------------------ the second pair of eyes
    def local_look(self, question: str, png: bytes, timeout: float = 150) -> str | None:
        """A seeing question to the local vision model; None if it isn't available (e.g. on the laptop)."""
        self.local_requests += 1
        body = json.dumps({"model": LOCAL_EYES, "prompt": question, "images": [base64.b64encode(png).decode()],
                           "stream": False, "think": False, "keep_alive": "5m",
                           "options": {"temperature": 0, "num_predict": 80}}).encode()
        try:
            req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body, headers={"Content-Type": "application/json"})
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read()).get("response", "").strip() or None
        except Exception:
            return None
