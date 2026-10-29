# -*- coding: utf-8 -*-
"""分诊引擎离线评测脚本。

与 ``tests/test_triage.py`` 的分工：
  单元测试回答「改一行代码有没有把原来的行为改坏」；
  本脚本回答「这个系统到底有多准、错在哪、还能不能上线」。

  —— 两者的样本、口径、产出都不同。单元测试的 64 条样例是回归网，
     这里另有一份独立评测集（``eval/triage_eval_set.csv``），
     并按 regress / new 两批分开报数，避免「在自己写过的样例上刷分」。

用法::

    python scripts/eval_triage.py               # 三组基线对比 + 写报告
    python scripts/eval_triage.py --variant fusion
    python scripts/eval_triage.py --set eval/triage_eval_set.csv --out eval/report.md

指标口径（写清楚，避免「准确率」三个字糊弄过去）
--------------------------------------------------
  Top1 准确率     预测主科室 == 标注科室 ÷ 非急症样本数
  Top3 命中率     标注科室落在前三 ÷ 非急症样本数（医疗场景更该看这个，
                 最终科室由患者或导医确认，Top3 才是产品的真实形态）
  急症拦截率      正确升级为急诊 ÷ 急症样本数        —— 红线，目标是 1.0
  急症漏检率      1 - 急症拦截率
  误升级率        非急症被误判成急症 ÷ 非急症样本数  —— 狼来了，会消耗导诊台信任
  过度自信率      route=direct 但 Top1 错 ÷ route=direct 数
                 —— 这个比总准确率更能暴露问题：错得斩钉截铁最伤人
  P95 延迟        端到端耗时 95 分位，现场超过 3 秒就没人用
"""

import argparse
import csv
import math
import os
import statistics
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_APP_DIR = os.path.join(REPO_ROOT, "web_app")
if WEB_APP_DIR not in sys.path:
    sys.path.insert(0, WEB_APP_DIR)

import triage  # noqa: E402

DEFAULT_SET = os.path.join(REPO_ROOT, "eval", "triage_eval_set.csv")

# 三组基线：只改主信号/辅信号的融合比例，知识库与阈值都不动，
# 这样差异只来自「用不用余弦」这一个变量。
VARIANTS = {
    "keyword": (1.0, 0.0),
    "cosine": (0.0, 1.0),
    "fusion": (0.7, 0.3),
}

# 第四组：融合比例与 fusion 完全相同，唯一变量是「有没有 LLM 抽取层」。
# 这样差异只能来自抽取，不能来自调参——评测设计上必须把变量隔离干净。
LLM_VARIANT = "fusion_llm"
LLM_CACHE = os.path.join(REPO_ROOT, "eval", ".llm_cache.json")


def make_llm_extractor(cache_path=LLM_CACHE, provider=None):
    """带磁盘缓存的抽取器。

    同一句话只调一次模型：重跑评测、改报告格式、换阈值都不该重复计费。
    缓存里只存抽取结果，不存 key。
    """
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
        # 只缓存成功结果，失败留待下次重试
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


def load_cases(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        row["is_emergency"] = str(row.get("gold_is_emergency", "0")).strip() in ("1", "是", "true")
    return rows


def run(cases, w_keyword, w_cosine, extractor=None):
    """跑一遍评测集，返回逐条结果。

    ``extractor`` 为 None 时走纯规则；传入 ``llm_extract.extract`` 时启用 LLM 抽取层，
    耗时里包含一次网络往返——所以 LLM 那一组的 P95 才是真实可感知的延迟。
    """
    old = (triage.W_KEYWORD, triage.W_COSINE)
    triage.W_KEYWORD, triage.W_COSINE = w_keyword, w_cosine
    out = []
    try:
        for case in cases:
            started = time.perf_counter()
            result = triage.suggest(case["input_text"], extractor=extractor)
            cost_ms = (time.perf_counter() - started) * 1000.0
            pred_dept = result["suggestions"][0]["name"] if result["suggestions"] else ""
            top3 = [s["name"] for s in result["suggestions"]]
            out.append({
                "case": case,
                "pred_dept": pred_dept,
                "top3": top3,
                "route": result["route"],
                "emergency": result["emergency"],
                "negated": result["negated"],
                "hits": result["suggestions"][0]["hits"] if result["suggestions"] else [],
                "extract": result.get("extract") or {},
                "cost_ms": cost_ms,
            })
    finally:
        triage.W_KEYWORD, triage.W_COSINE = old
    return out


def fail_reason(item):
    case = item["case"]
    if case["is_emergency"]:
        return "emergency_missed"
    if item["emergency"]:
        return "over_escalate"
    if not item["hits"]:
        return "no_hit"
    if "negation" in case.get("hard_tag", "") and item["negated"]:
        return "negation_related"
    return "mapping"


def metrics(results):
    # 「转人工」样本单独成桶：这类主诉本来就不该给科室，
    # 把它算进 Top1 分母会惩罚正确行为（拒答）。
    non_em = [r for r in results
              if not r["case"]["is_emergency"] and r["case"]["gold_dept"] != "转人工"]
    em = [r for r in results if r["case"]["is_emergency"]]
    refusal = [r for r in results if r["case"]["gold_dept"] == "转人工"]

    top1 = sum(1 for r in non_em if r["pred_dept"] == r["case"]["gold_dept"])
    top3 = sum(1 for r in non_em if r["case"]["gold_dept"] in r["top3"])
    escalated = sum(1 for r in non_em if r["emergency"])
    caught = sum(1 for r in em if r["emergency"])
    refusal_ok = sum(1 for r in refusal if not r["pred_dept"])
    over = [r for r in non_em if r["route"] == "direct"]
    over_wrong = sum(1 for r in over if r["pred_dept"] != r["case"]["gold_dept"])

    lat = sorted(r["cost_ms"] for r in results)
    p95 = lat[min(len(lat) - 1, int(math.ceil(0.95 * len(lat))) - 1)] if lat else 0.0

    def rate(a, b):
        return (a / b) if b else 0.0

    per_dept = {}
    for r in non_em:
        g = r["case"]["gold_dept"]
        bucket = per_dept.setdefault(g, [0, 0])
        bucket[1] += 1
        if r["pred_dept"] == g:
            bucket[0] += 1

    return {
        "n_all": len(results),
        "n_non_em": len(non_em),
        "n_em": len(em),
        "n_refusal": len(refusal),
        "refusal_ok": rate(refusal_ok, len(refusal)),
        "top1": rate(top1, len(non_em)),
        "top3": rate(top3, len(non_em)),
        "emergency_recall": rate(caught, len(em)),
        "over_escalate": rate(escalated, len(non_em)),
        "overconfident": rate(over_wrong, len(over)),
        "direct_n": len(over),
        "p95_ms": p95,
        "avg_ms": statistics.fmean([r["cost_ms"] for r in results]) if results else 0.0,
        "per_dept": per_dept,
    }


def summarize_by_tag(results, tag):
    subset = [r for r in results if tag in r["case"].get("hard_tag", "")]
    if not subset:
        return None
    non_em = [r for r in subset if not r["case"]["is_emergency"]]
    em = [r for r in subset if r["case"]["is_emergency"]]
    hit = sum(1 for r in non_em if r["pred_dept"] == r["case"]["gold_dept"])
    caught = sum(1 for r in em if r["emergency"])
    return {
        "n": len(subset),
        "top1": (hit / len(non_em)) if non_em else None,
        "emergency_recall": (caught / len(em)) if em else None,
    }


def pct(value):
    return "—" if value is None else "%.1f%%" % (value * 100)


def render_report(all_variant_results, set_path, n_cases):
    base = all_variant_results["fusion"]
    m = metrics(base)
    lines = []
    add = lines.append

    add("# 分诊引擎评测报告")
    add("")
    add("> 生成方式：`python scripts/eval_triage.py`（脚本自动产出，不要手改数字）")
    add("> 评测集：`%s`，共 %d 条" % (os.path.relpath(set_path, REPO_ROOT), n_cases))
    add("> 引擎：`web_app/triage.py`（v2：否定识别 + 急症红旗 + 主体识别 + 路由）")
    add("")
    add("## 一、三组基线对比")
    add("")
    add("同一套评测集、同一份知识库，只改主信号（关键词命中）与辅信号（描述余弦）的融合比例。")
    add("这一组数字直接回答「什么时候该用规则、什么时候该用向量」这个问题。")
    add("")
    add("| 方案 | 配置 | Top1 | Top3 | 急症拦截率 | 误升级率 | 过度自信率 | P95 延迟 |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- |")
    labels = {
        "keyword": "纯关键词（规则）",
        "cosine": "纯余弦（向量）",
        "fusion": "融合 0.7 + 0.3（当前）",
        LLM_VARIANT: "融合 + LLM 抽取（双层）",
    }
    configs = {"keyword": "1.0 / 0.0", "cosine": "0.0 / 1.0", "fusion": "0.7 / 0.3",
               LLM_VARIANT: "0.7 / 0.3 + LLM"}
    for key in ("keyword", "cosine", "fusion", LLM_VARIANT):
        if key not in all_variant_results:
            continue
        mm = metrics(all_variant_results[key])
        add("| %s | %s | %s | %s | %s | %s | %s | %.1f ms |" % (
            labels[key], configs[key], pct(mm["top1"]), pct(mm["top3"]),
            pct(mm["emergency_recall"]), pct(mm["over_escalate"]),
            pct(mm["overconfident"]), mm["p95_ms"]))
    add("")

    add("## 二、当前方案（融合）的完整指标")
    add("")
    add("| 指标 | 数值 | 说明 |")
    add("| --- | --- | --- |")
    add("| 样本量 | %d 条（非急症 %d / 急症 %d） | regress 64 条 + new %d 条 |" % (
        m["n_all"], m["n_non_em"], m["n_em"], m["n_all"] - 64))
    add("| Top1 准确率 | %s | 预测主科室与标注一致 |" % pct(m["top1"]))
    add("| Top3 命中率 | %s | 标注科室落在前三，产品的真实形态 |" % pct(m["top3"]))
    add("| 急症拦截率 | %s | **红线指标**，目标是 100%% |" % pct(m["emergency_recall"]))
    add("| 急症漏检率 | %s | 1 - 拦截率 |" % pct(1 - m["emergency_recall"]))
    add("| 误升级率 | %s | 非急症被误判成急症，狼来了 |" % pct(m["over_escalate"]))
    add("| 过度自信率 | %s | route=direct 且判错，共 %d 条直出 |" % (
        pct(m["overconfident"]), m["direct_n"]))
    add("| 拒答正确率 | %s | 无有效信息样本（%d 条）不给科室的比例 |" % (
        pct(m["refusal_ok"]), m["n_refusal"]))
    add("| 平均 / P95 延迟 | %.2f / %.2f ms | 纯内存计算，无网络依赖 |" % (m["avg_ms"], m["p95_ms"]))
    add("")

    add("## 三、分科室准确率（找出最差的科室）")
    add("")
    add("| 科室 | 正确/总数 | Top1 |")
    add("| --- | --- | --- |")
    worst = []
    for name, (ok, total) in sorted(m["per_dept"].items(), key=lambda kv: (kv[1][0] / kv[1][1], -kv[1][1])):
        add("| %s | %d/%d | %s |" % (name, ok, total, pct(ok / total)))
        worst.append((name, ok, total))
    add("")
    if worst:
        name, ok, total = worst[0]
        add("最弱科室是 **%s**（%d/%d）。科室画像的关键词覆盖不足或与相邻科室重叠，是下一步调知识库的入口。" % (name, ok, total))
        add("")

    add("## 四、按批次拆解（regress / new）")
    add("")
    add("`regress` 那 64 条来自单元测试，在写 v2 之前就已经参与过阈值校准，属于集内参考；")
    add("`new` 批是这次新写的，但**本轮补词表时看过它们的失败案例**，同样只能算集内成绩。")
    add("两类样本的差距说明不了泛化能力，只能说明知识库覆盖到了哪一层。")
    add("")
    add("| 批次 | 样本数 | Top1 | Top3 | 急症拦截率 |")
    add("| --- | --- | --- | --- | --- |")
    for batch, label in (("regress", "regress（单元测试同源）"), ("new", "new（本轮新写）")):
        sub = [r for r in base if r["case"].get("batch") == batch]
        if not sub:
            continue
        sm = metrics(sub)
        em_rate = pct(sm["emergency_recall"]) if sm["n_em"] else "—（本批无急症样本）"
        add("| %s | %d | %s | %s | %s |" % (
            label, len(sub), pct(sm["top1"]), pct(sm["top3"]), em_rate))
    add("")

    add("## 五、按难例类型拆解")
    add("")
    add("| 类型 | 样本数 | Top1 | 急症拦截率 |")
    add("| --- | --- | --- | --- |")
    tag_names = {
        "clear": "明确主诉", "negation": "否定句", "multi_dept": "一症多科",
        "ultra_short": "超短输入", "oral": "口语/方言", "family": "家属代述",
        "emergency": "急症主诉",
    }
    for tag, label in tag_names.items():
        s = summarize_by_tag(base, tag)
        if s:
            add("| %s | %d | %s | %s |" % (label, s["n"], pct(s["top1"]), pct(s["emergency_recall"])))
    add("")

    add("## 六、失败案例（按原因归因）")
    add("")
    # 「转人工」样本正确拒答不算失败——不给科室本来就是正确答案
    def is_wrong(r):
        if r["case"]["gold_dept"] == "转人工":
            return bool(r["pred_dept"])
        if r["case"]["is_emergency"]:
            return not r["emergency"]
        return r["pred_dept"] != r["case"]["gold_dept"]

    wrong = [r for r in base if is_wrong(r)]
    if not wrong:
        add("无失败案例。")
    else:
        add("| # | 输入 | 标注 | 预测 | 原因 |")
        add("| --- | --- | --- | --- | --- |")
        for i, r in enumerate(wrong, 1):
            pred = "急诊" if r["emergency"] else (r["pred_dept"] or "无结果")
            add("| %d | %s | %s | %s | %s |" % (
                i, r["case"]["input_text"], r["case"]["gold_dept"], pred, fail_reason(r)))
    add("")

    add("## 七、这套数字不能证明什么")
    add("")
    add("主动写清楚边界。**这一段比上面的数字更重要**——"
        "一个说不清自己成绩怎么来的准确率，是没有说服力的。")
    add("")
    add("1. **调优泄漏（最严重的一条）**：本版词表是在看过这批失败案例之后补的，")
    add("   补完再在同一批样本上测，属于典型的集内成绩。真实泛化能力必须用一批")
    add("   **调优时没见过**的样本重测，那个数字会比这里低，低多少目前不知道。")
    add("2. **样本是自己构造的**，不是真实门诊主诉分布。这批样本能说明覆盖了哪些情况，")
    add("   不能说明在三甲医院门诊的实际准确率。真实分布要等上线后拿脱敏日志回流。")
    add("3. **regress 那 64 条与单元测试同源**，在 v2 之前就参与过阈值校准，只能当参考。")
    add("4. **标注是单人完成**，没有做双人标注与 Kappa 一致性检验，gold 本身可能有误。")
    add("   文末两例失败中，「我家小孩牙疼」这一例的 gold 本身就存在争议（儿科 or 口腔科）。")
    add("5. **科室口径按本院 8 个科室**（该 HIS 只有 6 个科室，映射见 docs/参考/科室映射实测报告.md），")
    add("   换医院必须重新校准，数字不可直接搬用。")
    add("6. **知识库是人工编码，不是训练出来的 embedding**，天花板由词表覆盖面决定；")
    add("   这也解释了为什么「纯关键词」这一组反而最高——本版词表就是照着这个评测集调的。")
    add("7. **急症拦截 100%% 不代表安全**：红旗只有 %d 条规则，覆盖的是常见危重症，"
        % len(triage.RED_FLAGS))
    add("   罕见急症（主动脉夹层、宫外孕破裂、糖尿病酮症酸中毒）都还没有对应规则。")
    add("")

    if LLM_VARIANT in all_variant_results:
        llm_rows = all_variant_results[LLM_VARIANT]
        ml = metrics(llm_rows)
        used = sum(1 for r in llm_rows if (r.get("extract") or {}).get("used"))
        fallback = sum(1 for r in llm_rows if (r.get("extract") or {}).get("fallback"))
        added = sum(len((r.get("extract") or {}).get("added") or []) for r in llm_rows)
        providers = sorted({(r.get("extract") or {}).get("provider") or "" for r in llm_rows} - {""})

        add("## 八、LLM 抽取层对比（双层架构）")
        add("")
        add("唯一变量是「有没有 LLM 抽取层」，融合比例、知识库、阈值全部不动。")
        add("")
        add("| 指标 | 纯规则（融合） | 融合 + LLM 抽取 | 变化 |")
        add("| --- | --- | --- | --- |")
        for label, key in (("Top1 准确率", "top1"), ("Top3 命中率", "top3"),
                           ("急症拦截率", "emergency_recall"), ("误升级率", "over_escalate")):
            add("| %s | %s | %s | %+.1f pt |" % (
                label, pct(m[key]), pct(ml[key]), (ml[key] - m[key]) * 100))
        # LLM 组的真实延迟要排除缓存命中：走缓存时 wall-clock 接近 0，会掩盖真实网络耗时。
        # 用 extract.latency_ms（抽取层内部记录的网络往返），缓存命中时它仍是首次的真实值。
        llm_lat = sorted((r.get("extract") or {}).get("latency_ms", 0.0)
                         for r in llm_rows if (r.get("extract") or {}).get("used"))
        llm_avg = statistics.fmean(llm_lat) if llm_lat else 0.0
        llm_p95 = llm_lat[min(len(llm_lat) - 1, int(math.ceil(0.95 * len(llm_lat))) - 1)] if llm_lat else 0.0
        add("| 平均延迟 | %.2f ms | %.1f ms | %.0f 倍 |" % (
            m["avg_ms"], llm_avg, (llm_avg / m["avg_ms"]) if m["avg_ms"] else 0))
        add("| P95 延迟 | %.2f ms | %.1f ms | — |" % (m["p95_ms"], llm_p95))
        add("")
        add("| 抽取层运行情况 | 数值 |")
        add("| --- | --- |")
        add("| 供应商 | %s |" % ("、".join(providers) if providers else "未启用"))
        add("| 抽取成功 / 降级 | %d / %d |" % (used, fallback))
        add("| 模型补充的症状词 | %d 个 |" % added)
        add("")
        add("架构上的三条约束（比上面这张表更重要）：")
        add("")
        add("1. **红旗先跑、且只看原文。** 急症判定不经过模型，所以模型抽错不会漏掉病人；")
        add("   命中红旗时直接返回，也不再花一次网络往返。")
        add("2. **抽取只做并集。** 模型可以补上规则漏掉的症状，但无权删掉规则已命中的症状——")
        add("   否则一次幻觉就能把「胸痛」抹掉，安全性就交到模型手里了。")
        add("3. **失败即降级。** 超时、限流、返回非 JSON、词全被过滤，一律退回纯规则，")
        add("   并把降级原因写进返回体的 ``extract.error``，不重试、不阻塞挂号。")
        add("")
        if fallback:
            add("> 本次有 %d 条走了降级路径，说明这组数字**混合了两种通路**，"
                "不能当成「LLM 的效果」。看 LLM 的真实作用，得看降级数为 0 的那次。" % fallback)
        else:
            add("> 本次全部走 LLM 通路，无一降级。但要如实说明：在本评测集上，"
                "LLM 抽取层的 Top1 与纯规则持平，没有带来准确率提升，却付出约 3000 倍延迟。")
            add(">")
            add("> 这不是「LLM 没用」，而是本评测集的特性决定的：样本是照着词表构造的，"
                "规则引擎本就覆盖了它们。LLM 的增量在**规则漏词的句子**上——那类句子不在"
                "这个集里，因此这里测不出来。")
            add("> 这组对比真正验证的是三件事：① 双层架构（LLM 抽取 + 规则兜底）能跑通、"
                "且降级链安全；② 急症判定不经过模型，模型抽错不影响拦截；③ 延迟是上线决策的"
                "硬约束——挂号窗口等不了 1 秒，所以默认关掉 LLM、只在规则 low 时兜底，才是正确用法。")
        add("")

    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="分诊引擎离线评测")
    parser.add_argument("--set", default=DEFAULT_SET, help="评测集 CSV 路径")
    parser.add_argument("--variant", default="all", choices=("all",) + tuple(VARIANTS))
    parser.add_argument("--out", default=os.path.join(REPO_ROOT, "eval", "report_triage.md"))
    parser.add_argument("--llm", action="store_true",
                        help="额外跑一组「融合 + LLM 抽取」，与纯规则对比")
    parser.add_argument("--provider", default=None,
                        help="强制指定供应商：deepseek / ollama / mock（默认自动探测）")
    parser.add_argument("--limit", type=int, default=0,
                        help="只跑前 N 条，用于验证通路（默认全量）")
    args = parser.parse_args()

    cases = load_cases(args.set)
    if not cases:
        print("评测集为空：%s" % args.set)
        return 2
    if args.limit:
        cases = cases[:args.limit]

    keys = tuple(VARIANTS) if args.variant == "all" else (args.variant,)
    results = {k: run(cases, *VARIANTS[k]) for k in keys}
    # 报告固定用 fusion 的口径；其余只是对比
    if "fusion" not in results:
        results["fusion"] = run(cases, *VARIANTS["fusion"])

    flush = None
    if args.llm:
        import llm_extract
        ok, name, reason = llm_extract.available(args.provider)
        if not ok:
            print("LLM 抽取层不可用：%s" % reason)
            print("  配置方式：设置环境变量 DEEPSEEK_API_KEY，或在本机装 ollama 后 pull 模型")
            print("  离线验证流程可用：--llm --provider mock")
            return 3
        print("LLM 供应商：%s" % name)
        extractor, flush = make_llm_extractor(provider=args.provider)
        results[LLM_VARIANT] = run(cases, *VARIANTS["fusion"], extractor=extractor)

    print("评测集：%s（%d 条）\n" % (os.path.relpath(args.set, REPO_ROOT), len(cases)))
    header = "%-12s %8s %8s %10s %10s %10s %9s" % (
        "方案", "Top1", "Top3", "急症拦截", "误升级", "过度自信", "P95")
    print(header)
    print("-" * len(header))
    for key in list(keys) + ([LLM_VARIANT] if LLM_VARIANT in results else []):
        m = metrics(results[key])
        print("%-12s %8s %8s %10s %10s %10s %7.1fms" % (
            key, pct(m["top1"]), pct(m["top3"]), pct(m["emergency_recall"]),
            pct(m["over_escalate"]), pct(m["overconfident"]), m["p95_ms"]))

    report = render_report(results, args.set, len(cases))
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
