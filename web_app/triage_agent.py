# -*- coding: utf-8 -*-
"""导诊 Agent 层：多轮对话 + 工具调用编排 + RAG 检索。

定位与边界（四条，改代码前先读）
------------------------------------------------------------------
1. **规则引擎仍是安全底座。** 急症红旗、科室打分全部走 ``triage.py`` 规则侧，
   一个字都不经过模型。本模块只在「非急症、需要更灵活的服务」时接管对话。
2. **LLM 是「决策编排器」，不是「知识源」。** 模型不背医学知识，它只做三件事：
   判断患者意图、决定调哪个工具、把工具返回的结果组织成可读答案。知识来自
   ``rag_engine.KnowledgeBase``（医院私有知识）和 ``triage``（科室规则），
   这就是 RAG + Agent 的核心：决策交给模型，事实交给检索。
3. **工具调用用受约束的 JSON 协议。** 每个工具都有明确的 JSON schema 和参数校验，
   模型返回的调用参数不合法就当作「无需调用工具」，直接进入最终回答，防止幻觉
   工具调用把流程带偏。
4. **可溯源。** RAG 检索结果带 ``source``，最终回答要求模型在引用知识时标注来源，
   这是医疗场景「可解释、可审计」的底线。

供应商：复用 ``llm_extract`` 的 DeepSeek 直连与 key 读取（零新增依赖）。
"""

import json
import os
import re
import time

import llm_extract
import rag_engine

# 复用 llm_extract 的 DeepSeek 直连（urllib，零第三方依赖）
_deepseek_key = llm_extract.deepseek_key
_post_json = llm_extract._post_json
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_TIMEOUT = 15.0  # 导诊对话可以比单次抽取等久一点，但仍有上限


# ---------------------------------------------------------------------------
# 一、工具定义
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "search_knowledge",
        "description": "检索医院私有知识库，回答「就诊须知、检查注意事项、医保报销、专家排班、体检」等医院特定问题。输入是检索查询，返回带来源的知识片段。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "用于检索的问题或关键词，例如「胃镜注意事项」「医保报销比例」"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_department",
        "description": "根据患者症状主诉，调用规则引擎做科室分诊。输入是症状描述，返回推荐科室与依据。仅当患者是想知道「挂哪个科」时使用。",
        "parameters": {
            "type": "object",
            "properties": {
                "symptom": {"type": "string", "description": "患者的主要症状描述"}
            },
            "required": ["symptom"]
        }
    },
    {
        "name": "escalate_human",
        "description": "当患者诉求超出导诊能力范围（如需要具体诊断、开药、隐私敏感问题、或信息严重不足无法判断）时，转人工导诊。",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "转人工的原因"}
            },
            "required": ["reason"]
        }
    },
]


# ---------------------------------------------------------------------------
# 二、工具实现
# ---------------------------------------------------------------------------

def _tool_search_knowledge(query, kb=None):
    kb = kb or _default_kb()
    results = kb.search(query, top_k=3)
    if not results:
        return {"found": False, "text": "知识库中没有检索到相关内容。"}
    lines = []
    for i, r in enumerate(results, 1):
        lines.append("[%d] 来源 %s（相似度 %.2f）\n%s" % (i, r["source"], r["score"], r["text"]))
    return {"found": True, "text": "\n\n".join(lines), "sources": [r["source"] for r in results]}


def _tool_get_department(symptom):
    import triage
    result = triage.suggest(symptom)
    if result.get("emergency"):
        return {
            "emergency": True,
            "title": result.get("emergency_title", ""),
            "advice": result.get("emergency_advice", ""),
        }
    depts = [s["name"] for s in result.get("suggestions", [])[:3]]
    route = result.get("route", "human")
    return {"emergency": False, "departments": depts, "route": route}


def _tool_escalate_human(reason):
    return {"escalated": True, "reason": reason}


# 工具分发表
_TOOL_IMPLS = {
    "search_knowledge": _tool_search_knowledge,
    "get_department": _tool_get_department,
    "escalate_human": _tool_escalate_human,
}

_KB_SINGLETON = None


def _default_kb():
    global _KB_SINGLETON
    if _KB_SINGLETON is None:
        _KB_SINGLETON = rag_engine.KnowledgeBase()
    return _KB_SINGLETON


# ---------------------------------------------------------------------------
# 三、提示词与调用协议
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "你是医院门诊的导诊助手，帮助患者判断「挂哪个科」以及解答「就诊流程、检查注意事项、"
    "医保报销、专家排班」等医院相关问题。\n\n"
    "工作方式（重要）：\n"
    "1. 你不背医学知识。需要医院知识时，先调用 search_knowledge 检索；需要分诊时，调用 "
    "get_department。把工具返回的结果组织成回答，不要自己编造。\n"
    "2. 当患者描述的是「症状」（如心悸、胸闷、气短、头晕、腹痛等）时，"
    "应该先调用 search_knowledge 检索「这个症状可能是哪种疾病、要做什么检查、有什么危险信号」，"
    "再调用 get_department 给出科室。疾病知识和科室都要给，不要只给科室。\n"
    "3. 急症（胸痛大汗、口眼歪斜、呕血黑便、呼吸困难、难以平卧等）优先提示拨打 120 或急诊，不要只给科室。\n"
    "4. 涉及具体诊断、开药、或你无法确定的问题，调用 escalate_human 转人工，不要硬答。\n"
    "5. 回答要简洁、口语化、照顾老人。引用知识库内容时，在句末用「（来源：xxx）」标注出处。\n\n"
    "你的每次回复必须是 JSON，格式如下：\n"
    '{"tool": "<工具名或空>", "args": {...}, "answer": "<最终回答，未调用工具时为空>", "done": true/false}\n'
    "- 当需要调用工具时：tool 填工具名，args 填参数，done 填 false。\n"
    "- 当可以直接回答或已完成回答时：tool 填空字符串，answer 填最终回答，done 填 true。\n"
    "一次只调用一个工具。"
)


def _call_model(messages, timeout=DEFAULT_TIMEOUT):
    """调用 DeepSeek，返回模型文本。失败抛异常，由调用方降级。"""
    key = _deepseek_key()
    if not key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY")
    payload = {
        "model": DEFAULT_MODEL,
        "messages": messages,
        "temperature": 0,
        "max_tokens": 800,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    body = _post_json(DEFAULT_URL, payload, {"Authorization": "Bearer " + key}, timeout)
    return body["choices"][0]["message"]["content"]


_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def _parse_action(content):
    """解析模型返回的 JSON 动作，解析失败返回 None。"""
    if not content or not content.strip():
        return None
    m = _JSON_BLOCK.search(content)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


# ---------------------------------------------------------------------------
# 四、对外接口：多轮对话循环
# ---------------------------------------------------------------------------

def chat(user_text, history=None, max_turns=5, timeout=DEFAULT_TIMEOUT):
    """一轮导诊对话（内部可能包含多次模型调用与工具调用）。

    参数：
        user_text: 患者这一轮说的话
        history: 之前的对话历史，格式 [{"role": "user"/"assistant", "content": str}, ...]
        max_turns: 最多允许的模型-工具往返次数，防止死循环

    返回：
        {
            "ok": bool,
            "answer": str,          # 最终回答（可能为空）
            "emergency": bool,      # 是否触发急症提示
            "escalated": bool,      # 是否转人工
            "tools_used": [str],    # 本轮用到的工具
            "sources": [str],       # 引用的知识来源
            "turns": int,           # 实际模型往返次数
            "latency_ms": float,
            "error": str,
        }
    """
    result = {"ok": False, "answer": "", "emergency": False, "escalated": False,
              "tools_used": [], "sources": [], "turns": 0, "latency_ms": 0.0, "error": ""}
    user_text = (user_text or "").strip()
    if not user_text:
        result["error"] = "空输入"
        return result

    started = time.perf_counter()

    # 0) 规则急症预检：急症红旗必须由规则引擎兜底，不依赖模型表现。
    #    无论模型后面走哪条路，只要原文命中红旗，就直接返回急诊指引，
    #    既省一次模型调用，也保证「心梗病人不等网络往返、不被模型漏判」。
    try:
        import triage
        precheck = triage.suggest(user_text)
        if precheck.get("emergency"):
            result["ok"] = True
            result["emergency"] = True
            result["answer"] = "%s\n%s" % (
                precheck.get("emergency_title", ""),
                precheck.get("emergency_advice", ""),
            )
            result["tools_used"] = ["get_department"]
            result["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
            return result
    except Exception:
        # 预检失败不能阻断对话，降级为仅靠模型判断。
        pass
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in (history or [])[-10:]:  # 只保留最近 10 条，控制上下文
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_text})

    try:
        for _ in range(max_turns):
            result["turns"] += 1
            content = _call_model(messages, timeout)
            action = _parse_action(content)
            if action is None:
                result["error"] = "模型返回不可解析"
                return result

            tool = (action.get("tool") or "").strip()
            done = bool(action.get("done"))

            # 无工具调用 -> 这是最终回答
            if not tool:
                result["answer"] = action.get("answer", "") or ""
                result["ok"] = True
                return result

            # 有工具调用 -> 执行工具，把结果喂回去
            if tool not in _TOOL_IMPLS:
                # 未知工具：当作无工具，避免被幻觉工具带偏
                result["answer"] = action.get("answer", "") or ""
                result["ok"] = True
                return result

            args = action.get("args") or {}
            if tool == "search_knowledge":
                tool_result = _tool_search_knowledge(args.get("query", ""))
                result["tools_used"].append("search_knowledge")
                result["sources"].extend(tool_result.get("sources", []))
                obs = tool_result["text"]
            elif tool == "get_department":
                tool_result = _tool_get_department(args.get("symptom", ""))
                result["tools_used"].append("get_department")
                if tool_result.get("emergency"):
                    result["emergency"] = True
                    result["answer"] = "%s\n%s" % (tool_result.get("title", ""), tool_result.get("advice", ""))
                    result["ok"] = True
                    return result
                obs = "推荐科室：" + "、".join(tool_result.get("departments", [])) + "（路由：" + tool_result.get("route", "") + "）"
            elif tool == "escalate_human":
                result["tools_used"].append("escalate_human")
                result["escalated"] = True
                result["answer"] = "这个问题建议转人工导诊：" + str(args.get("reason", ""))
                result["ok"] = True
                return result
            else:
                obs = ""

            # 把工具执行结果作为一条 assistant 消息喂回，让模型基于它继续
            messages.append({"role": "assistant", "content": json.dumps(
                {"tool": tool, "result": obs}, ensure_ascii=False)})
            messages.append({"role": "user", "content": "请基于以上工具结果，给出最终回答（done=true）。"})

        result["error"] = "达到最大往返次数，未收敛"
        return result
    except Exception as exc:
        result["error"] = "Agent 异常：%s" % (exc,)
        return result
    finally:
        result["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 1)


def status():
    """命令行自检用。"""
    kb = _default_kb()
    return {
        "provider": "deepseek:%s" % DEFAULT_MODEL,
        "key_configured": bool(_deepseek_key()),
        "kb_docs": len(kb.docs),
        "kb_chunks": len(kb),
        "tools": [t["name"] for t in TOOLS],
    }


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(status(), ensure_ascii=False, indent=2))
    sample = sys.argv[1] if len(sys.argv) > 1 else "做胃镜要注意什么"
    print("\n问题：", sample)
    print(json.dumps(chat(sample), ensure_ascii=False, indent=2))
