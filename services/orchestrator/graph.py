import json
import operator
import re
from typing import TypedDict, List, Optional,Annotated

from anthropic import Anthropic
from langfuse import observe, get_client
from langgraph.graph  import StateGraph, END
from langgraph.constants import Send

client = Anthropic()
langfuse = get_client()



OUTPUT_FORMAT = (
    "Return only a JSON array, with no text before or after it. "
    "Each item must have keys: file, line, severity, message. "
    'severity must be exactly one of "info", "warning", "error". '
    "Return [] only if there are genuinely no issues in your area."
)

PROMPTS = {
    "static_analysis": (
        "You are a static analysis tool reviewing a git diff. "
        "Check every added line for issues in your area: code complexity, unused variables "
        "(assigned but never read), and poor naming (vague or single-letter names for functions, "
        "parameters, and variables). "
        "Report every issue in your area, even on a line that also has a security, architecture, "
        "or style problem: the boundaries between reviewers are about issue type, not about lines. "
        "For example, a variable that holds a hardcoded secret must still be reported if it is "
        "assigned but never used. "
        "Leave other issue types to the other reviewers: security (OWASP Top 10, hardcoded secrets, "
        "injection, eval/exec), architecture (error handling, input validation, separation of "
        "concerns, dependencies), and style (formatting, docstrings, readability). "
        "Only the security reviewer reports hardcoded secrets and eval/exec, so never mention "
        "secret values or eval/exec in your messages. "
        + OUTPUT_FORMAT
    ),
    "security": (
        "You are a security scanner reviewing a git diff. "
        "Your area is security: OWASP Top 10 vulnerabilities, hardcoded secrets, "
        "injection (SQL, command, etc.), and eval/exec risks. "
        "You are the only reviewer that reports hardcoded secrets and eval/exec risks. "
        "The boundaries between reviewers are about issue type, not about lines: report every "
        "security issue, even on a line that also has an issue belonging to another reviewer. "
        "Do not report issues of other types: code complexity, unused variables, or naming; "
        "architecture (separation of concerns, missing error handling or input validation, "
        "dependency usage); or style (formatting, readability, docstrings, consistency). "
        + OUTPUT_FORMAT
    ),
    "architecture": (
        "You are an architecture reviewer reviewing a git diff. "
        "Your area is missing input validation, missing error handling, separation of concerns "
        "violations, and improper dependency usage. "
        "For every added function, check whether it validates its parameters before using them and "
        "whether it handles exceptions its operations can raise, and report each gap, even if the "
        "same line also has a security problem: the boundaries between reviewers are about issue "
        "type, not about lines. "
        "Missing input validation and missing error handling are your issues even when the "
        "unvalidated input also creates a security risk: the security reviewer reports the risk, "
        "and you report the missing validation and error handling. "
        # Naming broader security categories (OWASP Top 10, injection) here makes the model
        # treat missing validation as a security issue and return [], so keep this list short.
        "The security reviewer reports hardcoded secrets and eval/exec risks; you do not comment "
        "on their security impact, and you never mention secret values or eval/exec by name. "
        "When a gap involves such a call, refer to it by line number "
        "instead, e.g. \"the operation on line <n> can raise and is not wrapped in error handling\". "
        "Do not report static analysis issues (complexity, unused variables, naming) or style "
        "issues (formatting, docstrings, readability). "
        + OUTPUT_FORMAT
    ),
}

SEVERITY_MAP = {
    "critical": "error",
    "high": "error",
    "major": "error",
    "error": "error",
    "medium": "warning",
    "warning": "warning",
    "low": "info",
    "minor": "info",
    "info": "info",
}


def parse_json_response(raw: str) -> list:
    raw = raw.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if match:
        raw = match.group(1).strip()
    try:
        return json.loads(raw)
    except Exception:
        return []
    
class GraphState(TypedDict):
    diff: str
    patterns: list[str]
    raw_findings: Annotated[list[dict], operator.add]
    findings: list[dict]


def make_node(agent_name: str, get_prompt):
    @observe( as_type = "generation", name = agent_name )
    def node(state: GraphState) -> dict:
        prompt = get_prompt(state) if callable(get_prompt) else get_prompt
        response = client.messages.create(
             model="claude-sonnet-4-6",
             max_tokens = 4096,
             system = prompt,
             messages = [
                 {"role" : "user", "content" : state["diff"]}
             ],
             output_config={"effort": "low"},
        )
        # Guard against empty content (refusals, unusual stop conditions)
        # and non-text blocks so an empty response yields zero findings
        # instead of crashing the graph.
        raw = ""
        for block in response.content:
            if block.type == "text":
                raw = block.text
                break

        items = parse_json_response(raw)
        for item in items:
            item["agent"] = agent_name
            # Safety net: map any non-standard label onto info/warning/error.
            severity = str(item.get("severity", "")).strip().lower()
            item["severity"] = SEVERITY_MAP.get(severity, "warning")
        return {"raw_findings": items}
    
    return node


def _style_prompt(state: GraphState) -> str:
    patterns_str = "\n".join(state["patterns"]) if state["patterns"] else "None"
    return (
        "You are a code style reviewer reviewing a git diff. "
        "Check every added line for issues in your area: formatting (such as spacing after commas "
        "and around operators), readability (including missing docstrings), and consistency. "
        f"Common patterns this team has had before: {patterns_str}. "
        "Report every issue in your area, even on a line that also has a security, static analysis, "
        "or architecture problem: the boundaries between reviewers are about issue type, not about "
        "lines. "
        "Leave other issue types to the other reviewers: security (OWASP Top 10, hardcoded secrets, "
        "injection, eval/exec), static analysis (complexity, unused variables, naming), and "
        "architecture (error handling, input validation, separation of concerns, dependencies). "
        "Only the security reviewer reports hardcoded secrets and eval/exec, so never mention "
        "secret values or eval/exec in your messages. "
        + OUTPUT_FORMAT
    )

def merge_node(state: GraphState) -> dict:
    seen = set()
    merged = []
    for finding in state["raw_findings"]:
        key = (finding.get("file"), finding.get("line"), finding.get("agent"), finding.get("message"))
        if key not in seen:
            seen.add(key)
            merged.append(finding)
    return {"findings": merged}

def fan_out(state: GraphState):
    return [
        Send("static_analysis", state),
        Send("security", state),
        Send("style", state),
        Send("architecture", state)
    ]

def build_graph() -> StateGraph:
    builder = StateGraph(GraphState)
    builder.add_node("static_analysis", make_node("static_analysis", PROMPTS["static_analysis"]))
    builder.add_node("security", make_node("security", PROMPTS["security"]))
    builder.add_node("style", make_node("style", _style_prompt))
    builder.add_node("architecture", make_node("architecture", PROMPTS["architecture"]))
    builder.add_node("merge", merge_node)
    builder.set_conditional_entry_point(fan_out)
    for name in  ("static_analysis", "security", "style", "architecture"):
        builder.add_edge(name, "merge")
    builder.add_edge("merge", END)
    return builder.compile()





