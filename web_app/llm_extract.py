# -*- coding: utf-8 -*-
"""LLM 抽取层：把患者口语主诉抽成结构化字段，交给 ``triage.suggest()`` 使用。

设计边界（三条，改代码前先读）
------------------------------------------------------------------
1. **只做理解，不做决策。** 科室打分、急症红旗、置信度路由全部留在
   ``triage.py`` 的规则侧。LLM 抽错最多让科室排序变差，**不会让急症病人被漏掉**——
   红旗永远基于原文的正则匹配，一个字都不经过 LLM。
2. **输出受词表约束。** 只允许从 ``triage.VOCAB``（170 个标准症状词）里选，
   词表外的词进 ``unknown`` 后丢弃。这样既压住幻觉，也保证下游能直接复用
   （打分是按标准词查权重的，自由文本没法打分）。
3. **失败即降级，绝不重试到卡住页面。** 超时、限流、返回不是 JSON、词全被过滤，
   一律返回 ``ok=False``，由调用方退回纯规则。挂号窗口前排队的人等不了第二次超时。

供应商可切换
------------------------------------------------------------------
  deepseek  官方 API（默认），需要 ``DEEPSEEK_API_KEY`` 或仓库根 ``.deepseek_key``
  ollama     本地模型，需本机装 ollama 并 pull 过模型（8G 显存可跑 7B Q4）
  mock       固定规则假实现，供单元测试与断网演示，不产生网络请求

无第三方依赖：只用标准库 ``urllib``，和项目其余部分保持一致。
"""

import json
import os
import re
import time
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"
DEFAULT_DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5:7b-instruct"

# 超时按「挂号窗口能等多久」定，不是按「模型多久能答完」定。
# 单次超过 4 秒，导诊员就会先手动处理，这次抽取等于白花钱。
DEFAULT_TIMEOUT = 4.0

SUBJECTS = ("self", "family", "child")

_KEY_FILES = (".deepseek_key", "my_deepseek.cnf")

_CACHE = {}


# ---------------------------------------------------------------------------
# 一、配置与可用性
# ---------------------------------------------------------------------------

def _repo_file(name):
    return os.path.join(REPO_ROOT, name)


def _read_key_from_file(path):
    """支持两种写法：文件里只有 key，或 ``key = sk-xxx``。"""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    name, _, value = line.partition("=")
                    if name.strip().lower() in ("key", "api_key", "deepseek_api_key"):
                        return value.strip().strip("'\"")
                    continue
                return line
    except OSError:
        return ""
    return ""


def deepseek_key():
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if key:
        return key
    for name in _KEY_FILES:
        key = _read_key_from_file(_repo_file(name))
        if key:
            return key
    return ""


def ollama_alive(host=None, timeout=0.6):
    """探一下本地推理服务在不在，用于决定默认走哪条路。"""
    host = host or os.environ.get("OLLAMA_HOST") or DEFAULT_OLLAMA_HOST
    try:
        with urllib.request.urlopen(host.rstrip("/") + "/api/tags", timeout=timeout):
            return True
    except Exception:
        return False


def resolve_provider(explicit=None):
    """确定用哪个供应商，返回 (名字, 说明)。

    优先级：显式指定 > 环境变量 > 有 key 走 deepseek > 本地有 ollama 走 ollama > 无。
    """
    name = (explicit or os.environ.get("TRIAGE_LLM_PROVIDER") or "").strip().lower()
    if name in ("deepseek", "ollama", "mock"):
        return name, "显式指定"
    if deepseek_key():
        return "deepseek", "检测到 DeepSeek API Key"
    if ollama_alive():
        return "ollama", "检测到本地 ollama 服务"
    return "", "未配置：既没有 DeepSeek Key，本地也没有 ollama"


def available(explicit=None):
    name, reason = resolve_provider(explicit)
    return bool(name), name, reason


# ---------------------------------------------------------------------------
# 二、提示词：把「自由抽取」改成「带词表的受约束抽取」
# ---------------------------------------------------------------------------

def _vocabulary():
    """延迟导入，避免 triage 反向依赖本模块造成循环。"""
    import triage
    return triage.VOCAB


def build_messages(text, vocab):
    system = (
        "你是门诊分诊的信息抽取器，只输出 JSON，不解释、不闲聊、不下诊断。\n"
        "任务：从患者口语主诉中抽取「已确定的症状」，并识别被否定的症状与陈述主体。\n"
        "硬性规则：\n"
        "1. terms 与 negated 只能从给定词表中原样选取，词表里没有的写进 unknown，不要自造词。\n"
        "2. 「不发烧」「没有咳嗽」「否认胸痛」这类被排除的症状放 negated，不要放 terms。\n"
        "3. 家长代述（我妈/孩子/老人）时 subject 填 family 或 child；本人填 self。\n"
        "4. 不要推断患者没说的症状，不要因为「肚子疼」就补上「腹泻」。\n"
        "5. 拿不准就不填，宁可少抽，不要猜。\n\n"
        "输出格式：\n"
        '{"terms": [], "negated": [], "subject": "self", "duration_days": null, "unknown": []}\n\n'
        "词表：\n" + "、".join(vocab)
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "主诉：" + text},
    ]


# ---------------------------------------------------------------------------
# 三、各供应商的具体调用
# ---------------------------------------------------------------------------

def _post_json(url, payload, headers=None, timeout=DEFAULT_TIMEOUT):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _call_deepseek(messages, timeout):
    key = deepseek_key()
    if not key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY（或仓库根 .deepseek_key）")
    payload = {
        "model": os.environ.get("DEEPSEEK_MODEL") or DEFAULT_DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": 0,
        "max_tokens": 300,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    body = _post_json(DEFAULT_DEEPSEEK_URL, payload,
                      {"Authorization": "Bearer " + key}, timeout)
    return body["choices"][0]["message"]["content"]


def _call_ollama(messages, timeout):
    host = (os.environ.get("OLLAMA_HOST") or DEFAULT_OLLAMA_HOST).rstrip("/")
    payload = {
        "model": os.environ.get("OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0, "num_predict": 300},
    }
    body = _post_json(host + "/api/chat", payload, None, timeout)
    return body["message"]["content"]


def _call_mock(messages, timeout):
    """离线假实现：复用规则侧的否定识别与分词，用来测流程，不代表真实效果。"""
    import triage
    text = messages[-1]["content"].replace("主诉：", "", 1)
    norm = triage.normalize(text)
    tokens = triage.tokenize(norm)
    norm_bar = triage.demote_local_side(triage.normalize_barriered(text))
    positive, negated = triage.split_by_negation(norm_bar, tokens, triage.negation_spans(norm_bar))
    subject = "self"
    for word in ("我妈", "我爸", "我母亲", "我父亲", "老人", "奶奶", "爷爷"):
        if word in text:
            subject = "family"
            break
    for word in ("孩子", "宝宝", "小孩", "儿子", "女儿"):
        if word in text:
            subject = "child"
            break
    return json.dumps({"terms": list(positive), "negated": list(negated),
                       "subject": subject, "duration_days": None, "unknown": []},
                      ensure_ascii=False)


_CALLERS = {
    "deepseek": _call_deepseek,
    "ollama": _call_ollama,
    "mock": _call_mock,
}


# ---------------------------------------------------------------------------
# 四、输出解析与校验
# ---------------------------------------------------------------------------

_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def parse_content(content, vocab):
    """把模型返回解析成受词表约束的结构；解析不出来抛 ValueError。"""
    if not content or not content.strip():
        raise ValueError("空返回")
    match = _JSON_BLOCK.search(content)
    if not match:
        raise ValueError("返回中没有 JSON")
    obj = json.loads(match.group(0))
    if not isinstance(obj, dict):
        raise ValueError("JSON 顶层不是对象")

    allowed = set(vocab)
    unknown = []

    def pick(values):
        kept = []
        for value in values if isinstance(values, list) else []:
            term = str(value).strip()
            if term in allowed:
                if term not in kept:
                    kept.append(term)
            elif term:
                unknown.append(term)
        return kept

    terms = pick(obj.get("terms"))
    negated = pick(obj.get("negated"))
    # 同时出现在两边的词以否定为准：患者明确否认过，就不该算肯定症状。
    terms = [t for t in terms if t not in negated]

    subject = str(obj.get("subject") or "").strip().lower()
    if subject not in SUBJECTS:
        subject = ""

    duration = obj.get("duration_days")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool):
        duration = None

    return {
        "terms": terms,
        "negated": negated,
        "subject": subject,
        "duration_days": duration,
        "unknown": (obj.get("unknown") or []) if isinstance(obj.get("unknown"), list) else [],
        "dropped": unknown,
    }


# ---------------------------------------------------------------------------
# 五、对外接口
# ---------------------------------------------------------------------------

def extract(text, provider=None, timeout=DEFAULT_TIMEOUT, use_cache=True):
    """抽取一句话，返回结构见下方 ``ok`` 字段。

    永远不抛异常：调用方只需要看 ``ok`` 判断是否降级。

        {"ok": bool, "provider": "deepseek:deepseek-chat", "terms": [...],
         "negated": [...], "subject": "self"/"family"/"child"/"",
         "unknown": [...], "dropped": [...], "latency_ms": 812,
         "error": "", "cached": False}
    """
    text = (text or "").strip()
    result = {"ok": False, "provider": "", "terms": [], "negated": [],
              "subject": "", "duration_days": None, "unknown": [], "dropped": [],
              "latency_ms": 0.0, "error": "", "cached": False}

    if not text:
        result["error"] = "空输入"
        return result

    name, _ = resolve_provider(provider)
    if not name:
        result["error"] = "未配置 LLM 供应商"
        return result

    model = ""
    if name == "deepseek":
        model = os.environ.get("DEEPSEEK_MODEL") or DEFAULT_DEEPSEEK_MODEL
    elif name == "ollama":
        model = os.environ.get("OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL
    result["provider"] = name if name == "mock" else "%s:%s" % (name, model)

    cache_key = (name, model, text)
    if use_cache and cache_key in _CACHE:
        cached = dict(_CACHE[cache_key])
        cached["cached"] = True
        return cached

    started = time.perf_counter()
    try:
        vocabulary = _vocabulary()
        content = _CALLERS[name](build_messages(text, vocabulary), timeout)
        parsed = parse_content(content, vocabulary)
    except urllib.error.HTTPError as exc:
        result["error"] = "HTTP %s（%s）" % (exc.code, _http_hint(exc.code))
    except urllib.error.URLError as exc:
        result["error"] = "网络不可达：%s" % (exc.reason,)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        result["error"] = "返回不可用：%s" % (exc,)
    except Exception as exc:  # 兜底：抽取层故障不能把挂号页面带崩
        result["error"] = "异常：%s" % (exc,)
    else:
        result.update(parsed)
        result["ok"] = True
    finally:
        result["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 1)

    # 只缓存成功结果：失败多半是临时的，缓存下来会让一次网络抖动永久生效。
    if result["ok"] and use_cache:
        _CACHE[cache_key] = dict(result)
    return result


def _http_hint(code):
    return {
        401: "Key 无效或已过期",
        402: "余额不足",
        429: "触发限流，请降低并发",
        500: "服务端错误",
        503: "服务过载",
    }.get(code, "详见返回体")


def status():
    """给 /api 或命令行自检用：当前会走哪条路。"""
    ok, name, reason = available()
    key = deepseek_key()
    return {
        "available": ok,
        "provider": name,
        "reason": reason,
        "deepseek_key": ("已配置（%s…%s）" % (key[:6], key[-4:])) if len(key) > 12 else ("已配置" if key else "未配置"),
        "ollama_alive": ollama_alive(),
        "model": (os.environ.get("DEEPSEEK_MODEL") or DEFAULT_DEEPSEEK_MODEL) if name == "deepseek"
        else (os.environ.get("OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL if name == "ollama" else ""),
    }


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(status(), ensure_ascii=False, indent=2))
    sample = sys.argv[1] if len(sys.argv) > 1 else "不发烧 也不咳嗽 就是头晕"
    print("\n样例：", sample)
    print(json.dumps(extract(sample), ensure_ascii=False, indent=2))
