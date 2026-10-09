"""Natural-language question -> tool calls -> grounded answer.

1. PLAN    a fast LLM turns the question (plus recent chat) into JSON: which tools to call, with what arguments.
2. EXECUTE the code validates each call against the fixed tool registry and runs real SQL (query_tools.py).
3. ANSWER  a chat LLM writes the reply using ONLY the tool results.

The LLM never sees the database and never writes SQL. Because the plan is plain JSON, any provider works,
including the local Ollama model.
"""
import json
import re
from dataclasses import dataclass, field
from typing import Callable

from app.config import GEMINI_CHAT_MODEL
from app.llm import client, prompts
from app.llm.query_tools import Ctx, call_tool, catalog_text, load_candidates, resolve_names
from app.services.candidate_service import get_job
from app.services.skill_normalizer import load_aliases
from app.db import PgConnection

LLMFn = Callable[[str, str], str]
MAX_CALLS = 4
HISTORY_TURNS = 6


@dataclass
class Answer:
    text: str
    calls: list[dict] = field(default_factory=list)       # what was executed (shown in the UI for transparency)
    results: list[dict] = field(default_factory=list)
    error: str = ""


def _plan_llm(system: str, user: str) -> str:
    return client.complete_with_fallback(system, user, [("groq", None), ("gemini", None)])


def _answer_llm(system: str, user: str) -> str:
    return client.complete_with_fallback(
        system, user, [("gemini", GEMINI_CHAT_MODEL), ("groq", client.GROQ_MODEL)], json_mode=False)


def _history(conn: PgConnection, job_id: int) -> list[dict]:
    rows = conn.execute("SELECT role, content FROM chat_history WHERE job_description_id=? ORDER BY id DESC LIMIT ?",
                        (job_id, HISTORY_TURNS)).fetchall()
    return [dict(r) for r in reversed(rows)]


def _save(conn, job_id, role, content):
    with conn:
        conn.execute("INSERT INTO chat_history (job_description_id, role, content) VALUES (?,?,?)", (job_id, role, content))


def _parse_plan(raw: str) -> dict:
    data = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()))
    if not isinstance(data, dict):
        raise ValueError("plan must be a JSON object")
    calls = data.get("calls") or []
    if not isinstance(calls, list) or not all(isinstance(c, dict) and "tool" in c for c in calls):
        raise ValueError("calls must be a list of {tool, args}")
    return {"calls": calls[:MAX_CALLS], "direct_reply": data.get("direct_reply")}


def plan_question(question: str, ctx: Ctx, history: list[dict], llm: LLMFn) -> dict:
    names = [c["name"] for c in load_candidates(ctx)]
    convo = "\n".join(f"{h['role']}: {h['content'][:500]}" for h in history) or "(none)"
    user = (f"JOB: {ctx.job.title}; required skills: {ctx.job.required_skills}\nCANDIDATES: {names}\n"
            f"RECENT CONVERSATION:\n{convo}\n\nQUESTION: {question}")
    error = ""
    for attempt in range(2):
        try:
            return _parse_plan(llm(prompts.PLAN_SYSTEM.replace("{catalog}", catalog_text()),
                                   user if attempt == 0 else user + prompts.RETRY_SUFFIX.format(error=error)))
        except (json.JSONDecodeError, ValueError) as exc:
            error = str(exc)[:200]
    raise ValueError(error)


NAME_ARGS = ("names", "first", "second", "name")


def keep_ambiguity(plan: dict, question: str, ctx: Ctx) -> dict:
    """Stop the planner from silently choosing between people who share a name.

    If the user typed only "Ananya" but the planner expanded it to "ANANYA IYER", the tool would never learn the name
    was ambiguous. When the words the user actually typed match several candidates, hand the tool those words instead.
    """
    q_words = set(re.findall(r"[a-z]+", question.lower()))
    cands = load_candidates(ctx)

    def fix(name):
        if not isinstance(name, str):
            return name
        words = re.findall(r"[a-z]+", name.lower())
        typed = [w for w in words if w in q_words]
        if not typed or len(typed) == len(words):
            return name                                   # nothing was added by the planner (or nothing to go on)
        _, problems = resolve_names(cands, [" ".join(typed)])
        return " ".join(typed) if problems and problems[0]["status"] == "ambiguous" else name

    for call in plan["calls"]:
        args = call.get("args")
        if not isinstance(args, dict):
            continue
        for key in NAME_ARGS:
            if key in args:
                args[key] = [fix(n) for n in args[key]] if isinstance(args[key], list) else fix(args[key])
    return plan


def ask(conn: PgConnection, job_id: int, question: str, *, plan_llm: LLMFn | None = None,
        answer_llm: LLMFn | None = None, remember: bool = True, embed_fn=None) -> Answer:
    question = question.strip()
    job = get_job(conn, job_id)
    if not question:
        return Answer("Please type a question about the candidates.")
    if job is None:
        return Answer("That job description does not exist.", error="unknown job")
    ctx = Ctx(conn, job_id, load_aliases(conn), job, embed_fn=embed_fn)
    history = _history(conn, job_id) if remember else []

    try:
        plan = keep_ambiguity(plan_question(question, ctx, history, plan_llm or _plan_llm), question, ctx)
    except client.LLMError as exc:
        return Answer("The AI service is temporarily unavailable (rate limit or network). Please try again in a minute.", error=str(exc))
    except ValueError as exc:
        return Answer("Sorry, I could not work out how to answer that. Could you rephrase the question?", error=str(exc))

    calls, results = [], []
    for c in plan["calls"]:
        args = c.get("args") if isinstance(c.get("args"), dict) else {}
        calls.append({"tool": c["tool"], "args": args})
        results.append(call_tool(ctx, c["tool"], args))

    if not calls and plan["direct_reply"]:
        text = str(plan["direct_reply"])
    else:
        user = f"QUESTION: {question}\n\nTOOL RESULTS (ground truth):\n{json.dumps(results, default=str)[:14000]}"
        if history:
            user = "RECENT CONVERSATION:\n" + "\n".join(f"{h['role']}: {h['content'][:400]}" for h in history) + "\n\n" + user
        try:
            text = (answer_llm or _answer_llm)(prompts.ANSWER_SYSTEM, user).strip()
        except client.LLMError as exc:
            return Answer("I found the data but the AI service could not phrase the answer right now. Please retry.",
                          calls, results, str(exc))
    if remember:
        _save(conn, job_id, "user", question)
        _save(conn, job_id, "assistant", text)
    return Answer(text, calls, results)
