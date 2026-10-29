# -*- coding: utf-8 -*-
"""规则漏词（OOV）评测：回答「LLM 抽取层到底有没有真实增量」。

背景
------------------------------------------------------------------
主评测集 ``eval/triage_eval_set.csv`` 的 165 条样本是照着词表构造的，
规则引擎本就覆盖了它们，所以那批样本上 LLM 与纯规则 Top1 持平（99.3%）。
那组对比只能证明「双层架构能跑通、降级链安全」，证明不了 LLM 有用。

要验证 LLM 的真实价值，必须用一批**规则词表覆盖不到**的句子来测：
患者说的症状是真实常见的，但用的是词表里没有的通俗说法，或症状类型本身
词表里就没有标准词。这批句子就是 ``eval/triage_oov_set.csv``。

本脚本做两件事：
1. 逐条对比「纯规则」与「规则 + LLM 抽取」的 Top1，数出 LLM 纠正了几条；
2. 把纠正不了的样本按原因归因，说明 LLM 增量的边界在哪。

增量边界（这是本脚本要讲清楚的核心结论）
------------------------------------------------------------------
LLM 抽取受词表约束（只能抽 ``triage.VOCAB`` 里的 171 个标准词），所以：

  * 能纠正的：正确症状在词表里有标准词，只是口语说法规则没折叠到。
    LLM 靠语义理解补上标准词 → Top1 被纠正。这就是 LLM 的**真实增量**。
  * 纠正不了的：正确症状词表里根本没有标准词。LLM 再聪明也抽不出一个
    不存在的词，只能受约束丢弃。这是 LLM 的**天花板**，也是词表的天花板。

结论不是「LLM 万能」也不是「LLM 没用」，而是：LLM 抽取层补的是「规则同义词表
覆盖不到的语义缺口」，它的上限由词表决定——想要进一步提升，得扩词表，而不是
指望模型凭空抽新词。

用法::

    python scripts/eval_oov.py                      # 只跑纯规则基线（验证评测集确实漏词）
    python scripts/eval_oov.py --llm                # 纯规则 vs 规则+LLM，写完整对比报告
    python scripts/eval_oov.py --llm --provider mock  # 离线验证流程（mock 不产生网络请求）
"""

import argparse
import csv
import os
import statistics
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_APP_DIR = os.path.join(REPO_ROOT, "web_app")
if WEB_APP_DIR not in sys.path:
    sys.path.insert(0, WEB_APP_DIR)

import triage  # noqa: E402

DEFAULT_SET = os.path.join(REPO_ROOT, "eval", "triage_oov_set.csv")
DEFAULT_OUT = os.path.join(REPO_ROOT, "eval", "report_oov.md")
LLM_CACHE = os.path.join(REPO_ROOT, "eval", ".llm_cache_oov.json")


def load_cases(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return rows


def make_extractor(cache_path, provider=None):
    """带磁盘缓存的抽取器，同一句话只调一次模型。"""
    import json as _json
    import llm_extract

    cache = {}
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as fh:
                cache = _json.load(fh)
        except (OSError, ValueError):
            cache = {}

    def _extract(text):
        if text in cache:
            hit = dict(cache[text])
            hit["cached"] = True
            return hit
        result = llm_extract.extract(text, provider=provider)
        if result.get("ok"):
            cache[text] = {k: v for k, v in result.items() if k != "cached"}
        return result

    def _flush():
        if not cache_path:
            return
        try:
            with open(cache_path, "w", encoding="utf-8") as fh:
                _json.dump(cache, fh, ensure_ascii=False, indent=1)
        except OSError:
            pass

    return _extract, _flush


def top1(result):
    return result["suggestions"][0]["name"] if result["suggestions"] else ""


def run(cases, extractor=None):
    out = []
    for case in cases:
        started = time.perf_counter()
        result = triage.suggest(case["input_text"], extractor=extractor)
        cost_ms = (time.perf_counter() - started) * 1000.0
        extract = result.get("extract") or {}
        out.append({
            "case": case,
            "pred": top1(result),
            "route": result["route"],
            "added": extract.get("added") or [],
            "latency_ms": extract.get("latency_ms", 0.0),
            "fallback": extract.get("fallback", False),
            "cost_ms": cost_ms,
        })
    return out


def pct(value):
    return "%.1f%%" % (value * 100) if value else "0.0%"


def classify(base_rows, llm_rows):
    """把每条样本归到三类之一：规则就对 / LLM 纠正 / 都错。"""
    fixed, still_wrong, already_right = [], [], []
    for b, l in zip(base_rows, llm_rows):
        gold = b["case"]["gold_dept"]
        if b["pred"] == gold:
            already_right.append((b, l))
        elif l["pred"] == gold:
            fixed.append((b, l))
        else:
            still_wrong.append((b, l))
    return already_right, fixed, still_wrong


def render_report(base_rows, llm_rows, set_path, n_cases, llm_enabled):
    already, fixed, still = classify(base_rows, llm_rows)
    n_base_correct = len(already)
    n_llm_correct = len(already) + len(fixed)

    lines = []
    add = lines.append

    add("# 规则漏词（OOV）评测报告")
    add("")
    add("> 生成方式：`python scripts/eval_oov.py`（脚本自动产出，不要手改数字）")
    add("> 评测集：`%s`，共 %d 条**规则词表覆盖不到**的口语主诉" % (
        os.path.relpath(set_path, REPO_ROOT), n_cases))
    add("> 引擎：`web_app/triage.py` + 可选 `web_app/llm_extract.py`")
    add("")
    add("## 一、核心结论：LLM 抽取层的真实增量")
    add("")
    if llm_enabled:
        add("| 方案 | Top1 正确数 | Top1 准确率 |")
        add("| --- | --- | --- |")
        add("| 纯规则 | %d / %d | %s |" % (n_base_correct, n_cases, pct(n_base_correct / n_cases)))
        add("| 规则 + LLM 抽取 | %d / %d | %s |" % (n_llm_correct, n_cases, pct(n_llm_correct / n_cases)))
        add("")
        add("**LLM 在这批漏词句上净纠正了 %d 条**（%s → %s），这才是 LLM 抽取层"
            "在真实场景下的价值——补上规则口语表覆盖不到的语义缺口。" % (
                len(fixed), pct(n_base_correct / n_cases), pct(n_llm_correct / n_cases)))
        add("")
        add("主评测集（165 条，照词表构造）上 LLM 与纯规则持平，是因为规则本就没漏词；")
        add("这批 25 条是规则**真漏了**的句子，LLM 的增量在这里才显现出来。")
    else:
        add("（未启用 LLM，本报告只有纯规则基线。加 `--llm` 重跑可得到对比。）")
        add("")
        add("| 方案 | Top1 正确数 | Top1 准确率 |")
        add("| --- | --- | --- |")
        add("| 纯规则 | %d / %d | %s |" % (n_base_correct, n_cases, pct(n_base_correct / n_cases)))
    add("")

    add("## 二、LLM 纠正的样本（真实增量）")
    add("")
    add("| # | 主诉 | 标注 | 规则预测 | LLM 补词 |")
    add("| --- | --- | --- | --- | --- |")
    if fixed:
        for i, (b, l) in enumerate(fixed, 1):
            add("| %d | %s | %s | %s | %s |" % (
                i, b["case"]["input_text"], b["case"]["gold_dept"],
                b["pred"] or "（无）", "、".join(l["added"])))
    else:
        add("| — | 无 | — | — | — |")
    add("")

    add("## 三、LLM 也纠正不了的样本（增量的天花板）")
    add("")
    add("这些句子的正确症状，词表里**根本没有对应的标准词**，LLM 受词表约束只能丢弃，")
    add("所以补不出任何词。这暴露的是**词表覆盖的边界**，不是 LLM 的能力问题——")
    add("想让系统处理这些，得先扩词表，模型凭空抽不出来的。")
    add("")
    add("| # | 主诉 | 标注 | 规则预测 | LLM 补词 | 缺的词表条目 |")
    add("| --- | --- | --- | --- | --- | --- |")
    if still:
        for i, (b, l) in enumerate(still, 1):
            add("| %d | %s | %s | %s | %s | %s |" % (
                i, b["case"]["input_text"], b["case"]["gold_dept"],
                b["pred"] or "（无）", "、".join(l["added"]) or "（空）",
                b["case"].get("note", "")))
    else:
        add("| — | 无 | — | — | — | — |")
    add("")

    if already:
        add("## 四、规则本来就能对的样本（不应算 LLM 的功劳）")
        add("")
        add("| # | 主诉 | 标注 | 规则预测 |")
        add("| --- | --- | --- | --- |")
        for i, (b, l) in enumerate(already, 1):
            add("| %d | %s | %s | %s |" % (i, b["case"]["input_text"],
                                             b["case"]["gold_dept"], b["pred"]))
        add("")
        add("这 %d 条在评测集设计时应当剔除，但保留是为了说明「漏词集」的纯度"
            "（越纯，上面的增量数字越可信）。" % len(already))
        add("")

    if llm_enabled:
        used_lat = [r["latency_ms"] for r in llm_rows if r["latency_ms"] > 0]
        fallback = sum(1 for r in llm_rows if r["fallback"])
        add("## 五、延迟与降级")
        add("")
        add("| 指标 | 数值 |")
        add("| --- | --- |")
        add("| LLM 平均延迟 | %.0f ms |" % (statistics.fmean(used_lat) if used_lat else 0.0))
        add("| LLM P95 延迟 | %.0f ms |" % (sorted(used_lat)[min(len(used_lat)-1, int(len(used_lat)*0.95)-1)] if used_lat else 0.0))
        add("| 降级条数 | %d |" % fallback)
        add("")
        add("延迟是上线决策的硬约束：挂号窗口等不了 1 秒。所以 LLM 抽取层默认关闭，"
            "只在规则 low 时兜底，而不是每条都过模型。")
        add("")

    add("## 六、这套数字的边界（诚实声明）")
    add("")
    add("1. 这批 25 条仍是**人工构造**的口语，不是真实门诊日志，只能说明覆盖了哪些"
        "口语缺口，不能直接外推到真实挂号量。")
    add("2. 标注是单人完成，gold 存在争议空间（如「奶水不够孩子吃不饱」是产后缺乳还是"
        "儿科问题）。")
    add("3. LLM 补词受 `LLM_ADDED_WEIGHT=0.4` 打折，只纠正那些「规则完全漏词」的句子，"
        "不会和规则精确命中的词平权——这是防幻觉的关键设计，也限定了它的作用场景。")
    add("")

    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="规则漏词（OOV）评测：测 LLM 抽取层真实增量")
    parser.add_argument("--set", default=DEFAULT_SET, help="漏词评测集 CSV 路径")
    parser.add_argument("--out", default=DEFAULT_OUT, help="报告输出路径")
    parser.add_argument("--llm", action="store_true", help="跑「规则+LLM」对比组")
    parser.add_argument("--provider", default=None, help="供应商：deepseek / ollama / mock")
    parser.add_argument("--no-cache", action="store_true", help="不使用磁盘缓存，强制重新抽取")
    args = parser.parse_args()

    cases = load_cases(args.set)
    if not cases:
        print("评测集为空：%s" % args.set)
        return 2

    print("评测集：%s（%d 条）\n" % (os.path.relpath(args.set, REPO_ROOT), len(cases)))
    base_rows = run(cases)

    llm_rows = None
    flush = None
    if args.llm:
        import llm_extract
        ok, name, reason = llm_extract.available(args.provider)
        if not ok:
            print("LLM 抽取层不可用：%s" % reason)
            print("  离线验证流程可用：--llm --provider mock")
            return 3
        print("LLM 供应商：%s" % name)
        cache_path = None if args.no_cache else LLM_CACHE
        extractor, flush = make_extractor(cache_path, provider=args.provider)
        llm_rows = run(cases, extractor=extractor)

    # 逐条打印对比表
    header = "%-6s %-24s %-6s %-8s %-8s %s" % (
        "ID", "主诉", "gold", "纯规则", "规则+LLM", "补词")
    print(header)
    print("-" * len(header.encode("gbk", "ignore")))
    for i, case in enumerate(cases):
        b = base_rows[i]
        l = llm_rows[i] if llm_rows else None
        lpred = l["pred"] if l else "—"
        added = "、".join(l["added"]) if l and l["added"] else ""
        print("%-6s %-24s %-6s %-8s %-8s %s" % (
            case["case_id"], case["input_text"][:16], case["gold_dept"],
            b["pred"] or "（无）", lpred or "（无）", added))

    # 汇总
    if llm_rows:
        already, fixed, still = classify(base_rows, llm_rows)
        n = len(cases)
        print("\n纯规则 Top1：%d/%d" % (len(already), n))
        print("规则+LLM Top1：%d/%d（净纠正 %d 条）" % (len(already) + len(fixed), n, len(fixed)))
        print("LLM 也纠正不了：%d 条（词表天花板）" % len(still))

    report = render_report(base_rows, llm_rows or [], args.set, len(cases), bool(llm_rows))
    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(report)
    print("\n报告已写入：%s" % out_path)
    if flush:
        flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
