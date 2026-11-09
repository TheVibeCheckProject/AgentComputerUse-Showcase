"""Procedural Recipe Replay for AgentComputerUse.

Enables deterministic, zero-token replay of routine, repetitive tasks (weather lookups,
order tracking, routine web searches) with visual landmark verification and seamless
fallback to the full LLM reasoning loop if the page layout changes.
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import actions
import screen
import task as tasks
from hands import Hands, Stopped

RECIPES_DIR = Path(__file__).resolve().parent / "data" / "recipes"


@dataclass
class RecipeStep:
    action: str                        # open_url | click | type | wait_landmark | extract
    url: str | None = None
    target_kind: str | None = None
    target_name: str | None = None
    text: str | None = None
    submit: bool = False
    pattern: str | None = None      # for wait_landmark or extract
    timeout: float = 8.0


@dataclass
class Recipe:
    name: str
    goal_pattern: str                  # regex or template e.g. r"weather\s+in\s+(?P<loc>.+)"
    steps: list[RecipeStep] = field(default_factory=list)

    def match_goal(self, goal: str) -> dict[str, str] | None:
        """Matches a user goal against the recipe's goal pattern, extracting parameters."""
        m = re.search(self.goal_pattern, goal.strip(), re.IGNORECASE)
        if m:
            return m.groupdict()
        return None


def save_recipe(recipe: Recipe, folder: Path | None = None) -> Path:
    target_dir = folder or RECIPES_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    p = target_dir / f"{recipe.name}.json"
    data = {
        "name": recipe.name,
        "goal_pattern": recipe.goal_pattern,
        "steps": [step.__dict__ for step in recipe.steps]
    }
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


def load_recipes(folder: Path | None = None) -> list[Recipe]:
    target_dir = folder or RECIPES_DIR
    if not target_dir.is_dir():
        return []
    recipes = []
    for f in target_dir.glob("*.json"):
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
            steps = [RecipeStep(**s) for s in raw.get("steps", [])]
            recipes.append(Recipe(raw["name"], raw["goal_pattern"], steps))
        except Exception:
            continue
    return recipes


def find_matching_recipe(goal: str, folder: Path | None = None) -> tuple[Recipe, dict[str, str]] | None:
    """Finds a matching recipe for the given goal and returns (recipe, parameters)."""
    for r in load_recipes(folder):
        params = r.match_goal(goal)
        if params is not None:
            return r, params
    return None


def execute_recipe(recipe: Recipe, params: dict[str, str], win, hands: Hands,
                   task_obj, emit=lambda event, **kw: None) -> dict[str, Any] | None:
    """Executes a recipe deterministically.

    Returns dict with answer and evidence on success, or None on any divergence (triggering fallback).
    """
    emit("progress", message=f"running procedural recipe '{recipe.name}' (0 API tokens)")
    extracted_answer = None
    evidence_list = []

    for idx, step in enumerate(recipe.steps, 1):
        emit("progress", step=idx, recipe_action=step.action)

        if step.action == "open_url":
            url = step.url or ""
            for k, v in params.items():
                url = url.replace(f"{{{k}}}", urllib_quote(v))
            try:
                actions.open_url(url, win, hands)
            except Stopped:
                raise                          # kill switch, the owner's hand, the time limit: stop, don't fall back
            except Exception:
                return None

        elif step.action == "type":
            text = step.text or ""
            for k, v in params.items():
                text = text.replace(f"{{{k}}}", v)
            view = screen.look(win, use_ocr=False, wait=False)
            target = None
            for i in view.items:
                if (not step.target_kind or i.kind == step.target_kind) and \
                   (not step.target_name or re.search(step.target_name, i.name or "", re.IGNORECASE)):
                    target = i
                    break
            if not target:
                return None
            # The same rules as the loop's own "type": only into the task's own windows; Edge's autocomplete
            # (selected after a typed address) is deleted before Enter.
            tasks.check_may_type_here(task_obj, win)
            address = target.name == screen.EDGE_ADDRESS_BAR
            try:
                actions.type_into(target, text, win, hands, address=address)
                if step.submit:
                    if address:
                        hands.press("delete")
                    hands.press("enter")
            except Stopped:
                raise
            except Exception:
                return None

        elif step.action == "wait_landmark":
            pat = re.compile(step.pattern or "", re.IGNORECASE)
            view = screen.look(win, use_ocr=True, wait=True)
            matched = any(pat.search(i.name or "") for i in view.items if i.name)
            if not matched:
                return None

        elif step.action == "extract":
            pat = re.compile(step.pattern or "", re.IGNORECASE)
            view = screen.look(win, use_ocr=True, wait=True)
            for i in view.items:
                if i.name and pat.search(i.name):
                    extracted_answer = i.name
                    evidence_list.append(i.name)
                    break
            if not extracted_answer:
                return None

    if extracted_answer:
        return {"answer": extracted_answer, "evidence": evidence_list, "recipe": recipe.name}
    return None


def urllib_quote(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote_plus(s)
