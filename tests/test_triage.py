# -*- coding: utf-8 -*-
"""智能分诊引擎离线用例（不依赖数据库）。

这些用例把「分诊算法」的行为固化成断言，同时充当知识库的回归网：
改同义词表、改科室关键词权重、改阈值，只要让任何一条样例的排序掉下来就会失败。

覆盖范围
--------
1. 归一化：全角转半角、标点清除、口语折叠成标准术语；
2. 分词：最长匹配、停用词前置剥离、**跨词边界假词防护**（回归用例）；
3. 向量与相似度：余弦的自反性、正交性、取值范围；
4. 排序准确率：64 条明确病例断言 Top1，10 条模糊主诉断言命中 Top3；
5. 结果结构：字段齐全、分数降序、置信度分档；
6. 安全边界：空输入、超长输入、纯寒暄不得给出高置信结论。
"""

import unittest

from tests.base import REPO_ROOT, WEB_APP_DIR  # noqa: F401  （导入即完成 sys.path 注入）

import triage


# ---------------------------------------------------------------------------
# 明确病例：主诉指向单一科室，断言 Top1
# ---------------------------------------------------------------------------
TOP1_CASES = [
    # 内科（8）
    ("我心慌胸闷三天了，晚上躺下更明显", "内科"),
    ("拉肚子拉了五天了，一天七八次", "内科"),
    ("发烧咳嗽有痰，嗓子疼", "内科"),
    ("血压高，头晕", "内科"),
    ("反酸烧心，吃完饭就难受", "内科"),
    ("最近老是睡不着，心里发慌", "内科"),
    ("头疼头晕还有点想吐", "内科"),
    ("浑身没劲，有点低烧", "内科"),
    # 外科（8）
    ("手上划了一道口子，出血不止", "外科"),
    ("肛门那里有个包，坐下就疼", "外科"),
    ("腿上静脉曲张，青筋鼓起来", "外科"),
    ("磕伤了膝盖，破皮了", "外科"),
    ("胳膊上长了个包块，摸着有点硬", "外科"),
    ("脚上扎了钉子，伤口化脓了", "外科"),
    ("痔疮又犯了，大便带血", "外科"),
    ("烫伤了，起了水泡", "外科"),
    # 儿科（8）
    ("孩子发烧38度5，一直哭", "儿科"),
    ("宝宝身上起红点，幼儿园好几个都这样", "儿科"),
    ("小孩手足口病", "儿科"),
    ("婴儿黄疸一直不退", "儿科"),
    ("我家孩子咳嗽老不好", "儿科"),
    ("儿童身高比同龄人矮很多", "儿科"),
    ("小朋友打完疫苗发烧了", "儿科"),
    ("小儿多动，注意力不集中", "儿科"),
    # 妇产科（8）
    ("月经不调，痛经很厉害", "妇产科"),
    ("怀孕两个月，想做产检", "妇产科"),
    ("白带异常有异味", "妇产科"),
    ("乳房胀痛，摸着有硬块", "妇产科"),
    ("更年期潮热出汗", "妇产科"),
    ("宫外孕，肚子疼", "妇产科"),
    ("想咨询避孕方式", "妇产科"),
    ("产检预约，孕24周", "妇产科"),
    # 骨科（8）
    ("腰疼得直不起来，弯不下", "骨科"),
    ("打篮球崴脚了，脚踝肿了", "骨科"),
    ("颈椎疼，脖子转不动", "骨科"),
    ("腰酸背痛", "骨科"),
    ("膝盖关节疼，上下楼费劲", "骨科"),
    ("老人骨质疏松，容易骨折", "骨科"),
    ("手腕腱鞘炎，一动就疼", "骨科"),
    ("摔了一跤，腿麻站不起来", "骨科"),
    # 眼科（8）
    ("眼睛红，流泪，怕光", "眼科"),
    ("最近视力下降，看东西模糊", "眼科"),
    ("眼前有飞蚊飘来飘去", "眼科"),
    ("眼睛干涩，看电脑久了难受", "眼科"),
    ("老人白内障，看不清", "眼科"),
    ("眼屎多，眼睛发痒", "眼科"),
    ("近视度数加深，想配眼镜", "眼科"),
    ("眼皮上长了个小疙瘩", "眼科"),
    # 皮肤科（8）
    ("脸上长痘，还有瘙痒", "皮肤科"),
    ("身上起疹子，特别痒", "皮肤科"),
    ("掉头发厉害，头皮屑多", "皮肤科"),
    ("皮肤过敏，起了荨麻疹", "皮肤科"),
    ("湿疹反复发作，越挠越痒", "皮肤科"),
    ("手上有癣，脱皮", "皮肤科"),
    ("脚趾甲变厚发黄，灰指甲", "皮肤科"),
    ("身上长了个痣，最近变大", "皮肤科"),
    # 口腔科（8）
    ("牙疼，晚上疼得睡不着", "口腔科"),
    ("牙龈出血，口臭", "口腔科"),
    ("口腔溃疡反复发作", "口腔科"),
    ("智齿发炎，脸都肿了", "口腔科"),
    ("蛀牙有个洞", "口腔科"),
    ("想拔牙，牙松了", "口腔科"),
    ("牙周炎，牙齿松动", "口腔科"),
    ("想做牙齿正畸", "口腔科"),
]

# ---------------------------------------------------------------------------
# 模糊主诉：信息不足以唯一确定科室，只要求命中的科室进入 Top3
# ---------------------------------------------------------------------------
TOP3_CASES = [
    ("身上起红点，有点痒", "皮肤科"),
    ("头疼", "内科"),
    ("肚子不舒服", "内科"),
    ("关节疼", "骨科"),
    ("眼睛不舒服", "眼科"),
    ("牙齿不舒服", "口腔科"),
    ("小孩发烧", "儿科"),
    ("胸闷", "内科"),
    ("腰痛", "骨科"),
    ("皮肤痒", "皮肤科"),
]


class NormalizeTests(unittest.TestCase):
    def test_colloquial_folded_to_standard_term(self):
        self.assertIn("腹泻", triage.normalize("拉肚子"))
        self.assertIn("发热", triage.normalize("发烧"))
        self.assertIn("牙痛", triage.normalize("牙疼"))
        self.assertIn("儿童", triage.normalize("宝宝"))

    def test_fullwidth_and_punctuation_removed(self):
        self.assertEqual(triage.normalize("头痛，，，"), "头痛")
        self.assertEqual(triage.normalize("ＡＢＣ"), "abc")

    def test_empty_input_safe(self):
        self.assertEqual(triage.normalize(""), "")
        self.assertEqual(triage.normalize(None), "")


class TokenizeTests(unittest.TestCase):
    def test_longest_match_wins(self):
        # "口腔溃疡" 必须整体命中，不能被切成 "口腔"
        self.assertIn("口腔溃疡", triage.tokenize("口腔溃疡反复发作"))

    def test_no_cross_boundary_fake_word(self):
        """回归：'最近视力下降' 曾因停用词未前置剥离而切出 '近视'。

        "最" + "近视" 跨了词边界，最长正向匹配会误命中眼科术语。
        修复方式是切分前先剥离多字停用词（"最近"），并改用双向最大匹配。
        """
        tokens = triage.tokenize(triage.normalize("最近视力下降，看东西模糊"))
        self.assertNotIn("近视", tokens)
        self.assertIn("视力", tokens)

    def test_stopwords_removed(self):
        tokens = triage.tokenize(triage.normalize("我最近有点发热"))
        self.assertIn("发热", tokens)
        self.assertNotIn("最近", tokens)
        self.assertNotIn("有点", tokens)

    def test_unknown_text_yields_empty(self):
        self.assertEqual(triage.tokenize("asdfghjkl"), [])


class VectorAndSimilarityTests(unittest.TestCase):
    def test_cosine_of_identical_vector_is_one(self):
        vec = {"a": 1.0, "b": 2.0}
        self.assertAlmostEqual(triage.similarity(vec, vec), 1.0, places=6)

    def test_cosine_of_disjoint_vectors_is_zero(self):
        self.assertEqual(triage.similarity({"a": 1.0}, {"b": 1.0}), 0.0)

    def test_cosine_range(self):
        a, b = {"x": 1.0, "y": 2.0}, {"x": 2.0, "y": 1.0}
        value = triage.similarity(a, b)
        self.assertGreaterEqual(value, 0.0)
        self.assertLessEqual(value, 1.0)

    def test_empty_vector_safe(self):
        self.assertEqual(triage.similarity({}, {"a": 1.0}), 0.0)

    def test_vectorize_returns_sparse_vector(self):
        vec, norm, tokens = triage.vectorize("牙疼得睡不着")
        self.assertTrue(vec)
        self.assertIn("牙痛", norm)
        self.assertIn("牙痛", tokens)
        self.assertTrue(all(isinstance(v, float) for v in vec.values()))

    def test_idf_is_positive(self):
        self.assertTrue(all(v > 0 for v in triage.IDF.values()))


class SuggestAccuracyTests(unittest.TestCase):
    def test_top1_accuracy_on_clear_cases(self):
        failures = []
        for text, expected in TOP1_CASES:
            result = triage.suggest(text)
            got = result["suggestions"][0]["name"]
            if got != expected:
                failures.append("%s -> 期望 %s，实际 %s（候选：%s）" % (
                    text, expected, got,
                    "、".join("%s %d%%" % (s["name"], s["percent"])
                             for s in result["suggestions"])))
        self.assertEqual(failures, [], "Top1 排序错误：\n" + "\n".join(failures))

    def test_expected_department_in_top3_for_vague_cases(self):
        failures = []
        for text, expected in TOP3_CASES:
            names = [s["name"] for s in triage.suggest(text)["suggestions"]]
            if expected not in names:
                failures.append("%s -> 期望 %s 在 Top3，实际 %s" % (text, expected, names))
        self.assertEqual(failures, [], "Top3 未覆盖：\n" + "\n".join(failures))

    def test_empty_input_reports_low_confidence(self):
        for text in ("", "   ", None):
            result = triage.suggest(text)
            self.assertEqual(result["confidence"], "low")
            self.assertEqual(result["tokens"], [])

    def test_meaningless_complaint_is_low_confidence(self):
        result = triage.suggest("不太舒服")
        self.assertEqual(result["confidence"], "low")

    def test_clear_case_is_not_low_confidence(self):
        self.assertIn(triage.suggest("牙疼，牙龈出血")["confidence"], ("medium", "high"))


class ResultShapeTests(unittest.TestCase):
    def setUp(self):
        self.result = triage.suggest("孩子发烧还咳嗽，吃不下饭")

    def test_top_level_keys(self):
        for key in ("query", "normalized", "tokens", "confidence", "advice", "suggestions", "all"):
            self.assertIn(key, self.result)

    def test_suggestion_keys(self):
        item = self.result["suggestions"][0]
        for key in ("code", "name", "scope", "score", "percent",
                    "keyword_score", "cosine", "hits"):
            self.assertIn(key, item)

    def test_top_k_limit(self):
        self.assertLessEqual(len(self.result["suggestions"]), 3)
        self.assertEqual(len(triage.suggest("发热", top_k=5)["suggestions"]), 5)

    def test_descending_scores(self):
        scores = [s["score"] for s in self.result["all"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_percent_within_range(self):
        for item in self.result["all"]:
            self.assertGreaterEqual(item["percent"], 0)
            self.assertLessEqual(item["percent"], 100)

    def test_all_departments_covered(self):
        """8 个科室都必须出现在 all 里，不能漏算。"""
        self.assertEqual(len(self.result["all"]), len(triage.DEPARTMENTS))

    def test_hits_are_real_keywords(self):
        vocab = set(triage.VOCAB)
        for item in self.result["all"]:
            for hit in item["hits"]:
                self.assertIn(hit, vocab)


class SimilarDepartmentTests(unittest.TestCase):
    def test_returns_other_departments_only(self):
        for dept in triage.DEPARTMENTS:
            sims = triage.similar_departments(dept["code"], 3)
            self.assertEqual(len(sims), 3)
            self.assertNotIn(dept["code"], [s["code"] for s in sims])

    def test_unknown_code_returns_empty(self):
        self.assertEqual(triage.similar_departments("XXX"), [])

    def test_similarity_not_all_zero(self):
        """描述文本余弦必须给出可区分的相对排序，不能全是 0。"""
        values = [s["similarity"] for s in triage.similar_departments("NK", 7)]
        self.assertTrue(any(v > 0 for v in values), "科室间相似度全为 0，排查工具失效")


if __name__ == "__main__":
    unittest.main(verbosity=2)
