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



PROMPTS = {
    "static_analysis": "You are a static analysis tool. Review this git diff for code complexity issues, unused variables, and poor naming. Return only a JSON array. Each item must have keys: file, line, severity (info/warning/error), message.",
    "security": "You are a security scanner. Review this git diff for OWASP Top 10 vulnerabilities, hardcoded secrets, and SQL injection risks. Return only a JSON array. Each item must have keys: file, line, severity, message.",
    "architecture": "You are an architecture reviewer. Review this git diff for separation of concerns violations, missing error handling, and improper dependency usage. Return only a JSON array. Each item must have keys: file, line, severity, message.",
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
        return {"raw_findings": items}
    
    return node


def _style_prompt(state: GraphState) -> str:
    patterns_str = "\n".join(state["patterns"]) if state["patterns"] else "None"
    return f"You are a code style reviewer. Review this git diff for formatting, readability, and consistency issues. Common patterns this team has had before: {patterns_str}. Return only a JSON array. Each item must have keys: file, line, severity, message."

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





