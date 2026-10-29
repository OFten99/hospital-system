# -*- coding: utf-8 -*-
"""规则漏词（OOV）评测集与脚本的回归用例（离线，不发网络请求）。

这组用例守的是「漏词评测集的纯度」：
  1. 评测集里的每条样本，纯规则都必须 Top1 判错（或无结果）——否则它就不是「漏词」样本，
     拿它测 LLM 增量就是自欺欺人；
  2. 评测脚本的 classify 归因逻辑（规则就对 / LLM 纠正 / 都错）要正确。

一旦有人改了 triage.py 的词表，让某些样本变成「规则能对了」，这些用例会立刻报错，
提醒维护者重新审视评测集是否还成立。
"""

import os
import sys
import unittest

from tests.base import REPO_ROOT, WEB_APP_DIR  # noqa: F401

import triage

EVAL_DIR = os.path.join(REPO_ROOT, "eval")
OOV_SET = os.path.join(EVAL_DIR, "triage_oov_set.csv")


def _load_oov():
    import csv
    with open(OOV_SET, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


class OovSetPurityTest(unittest.TestCase):
    """漏词集必须「漏词」：纯规则对每一条都判不对。"""

    def test_评测集存在且非空(self):
        rows = _load_oov()
        self.assertGreater(len(rows), 0, "漏词评测集为空")

    def test_每条纯规则都判错或无结果(self):
        leaked = []
        for row in _load_oov():
            result = triage.suggest(row["input_text"])
            top = result["suggestions"][0]["name"] if result["suggestions"] else ""
            if top == row["gold_dept"]:
                leaked.append((row["case_id"], row["input_text"], top))
        self.assertEqual(
            leaked, [],
            "以下样本纯规则已经能判对，不再是「漏词」样本，评测集失去意义：%s" % leaked)

    def test_标注科室合法(self):
        legal = {d["name"] for d in triage.DEPARTMENTS}
        for row in _load_oov():
            self.assertIn(row["gold_dept"], legal,
                          "%s 的标注科室 %s 不在科室表里" % (row["case_id"], row["gold_dept"]))

    def test_hard_tag只能是三类(self):
        legal = {"oov_llm_fix", "oov_ceiling", "oov_subject"}
        for row in _load_oov():
            self.assertIn(row["hard_tag"], legal,
                          "%s 的 hard_tag %s 非法" % (row["case_id"], row["hard_tag"]))


class OovClassifyTest(unittest.TestCase):
    """classify 归因逻辑：规则就对 / LLM 纠正 / 都错 三桶互斥且全覆盖。"""

    def test_三桶互斥全覆盖(self):
        # 复用 scripts/eval_oov.py 的 classify，不重复实现
        sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
        import eval_oov

        base = [
            {"case": {"gold_dept": "内科"}, "pred": "内科"},  # 规则对
            {"case": {"gold_dept": "内科"}, "pred": ""},       # LLM 纠正
            {"case": {"gold_dept": "内科"}, "pred": "外科"},   # 都错
        ]
        llm = [
            {"case": {"gold_dept": "内科"}, "pred": "内科"},
            {"case": {"gold_dept": "内科"}, "pred": "内科"},
            {"case": {"gold_dept": "内科"}, "pred": "外科"},
        ]
        already, fixed, still = eval_oov.classify(base, llm)
        self.assertEqual(len(already), 1)
        self.assertEqual(len(fixed), 1)
        self.assertEqual(len(still), 1)
        self.assertEqual(len(already) + len(fixed) + len(still), len(base))


if __name__ == "__main__":
    unittest.main(verbosity=2)
