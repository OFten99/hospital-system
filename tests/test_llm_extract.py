# -*- coding: utf-8 -*-
"""LLM 抽取层离线用例（不发任何网络请求）。

这一组用例守的不是「模型准不准」，而是三条工程约束：
  1. 抽出词表外的词必须被丢掉（压幻觉），否则下游打分查不到权重；
  2. 模型抽错、超时、抛异常，都必须退回纯规则，不能让挂号流程中断；
  3. 急症红旗先跑，且不依赖模型——模型完全不参与，也不该被调用。

用 ``provider="mock"`` 走离线假实现，因此这些用例在断网、没配 Key 的机器上也能跑。
"""

import json
import os
import unittest

from tests.base import REPO_ROOT, WEB_APP_DIR  # noqa: F401  （导入即完成 sys.path 注入）

import llm_extract
import triage


class ParseContentTest(unittest.TestCase):
    """返回体解析与词表约束。"""

    def setUp(self):
        self.vocab = triage.VOCAB

    def test_丢弃词表外的词(self):
        content = json.dumps({"terms": ["发热", "外星人综合征"], "negated": [],
                              "subject": "self"}, ensure_ascii=False)
        parsed = llm_extract.parse_content(content, self.vocab)
        self.assertEqual(parsed["terms"], ["发热"])
        self.assertIn("外星人综合征", parsed["dropped"])

    def test_否定优先于肯定(self):
        content = json.dumps({"terms": ["发热", "咳嗽"], "negated": ["发热"],
                              "subject": "self"}, ensure_ascii=False)
        parsed = llm_extract.parse_content(content, self.vocab)
        self.assertEqual(parsed["terms"], ["咳嗽"])
        self.assertEqual(parsed["negated"], ["发热"])

    def test_容忍代码块包裹(self):
        content = "```json\n" + json.dumps({"terms": ["头痛"], "subject": "family"},
                                           ensure_ascii=False) + "\n```"
        parsed = llm_extract.parse_content(content, self.vocab)
        self.assertEqual(parsed["terms"], ["头痛"])
        self.assertEqual(parsed["subject"], "family")

    def test_非法主体归一为已知值(self):
        parsed = llm_extract.parse_content('{"terms": [], "subject": "宠物"}', self.vocab)
        self.assertEqual(parsed["subject"], "")

    def test_非JSON直接报错由上层降级(self):
        with self.assertRaises(ValueError):
            llm_extract.parse_content("我觉得是内科", self.vocab)
        with self.assertRaises(ValueError):
            llm_extract.parse_content("", self.vocab)


class ExtractWithMockTest(unittest.TestCase):
    """离线假实现的端到端抽取（只验证流程与字段，不代表真实模型效果）。"""

    def test_否定词被抽到negated(self):
        result = llm_extract.extract("不发烧 也不咳嗽 就是头晕", provider="mock")
        self.assertTrue(result["ok"])
        self.assertEqual(result["terms"], ["头晕"])
        self.assertIn("发热", result["negated"])
        self.assertIn("咳嗽", result["negated"])

    def test_家属代述被识别(self):
        result = llm_extract.extract("我妈发烧三天了", provider="mock")
        self.assertEqual(result["subject"], "family")

    def test_空输入不报错(self):
        result = llm_extract.extract("   ", provider="mock")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "空输入")

    def test_未配置供应商时返回失败而非异常(self):
        result = llm_extract.extract("头痛", provider="deepseek", use_cache=False)
        # 本机没配 Key 时应给出明确的失败原因；配了 Key 就跳过这条断言
        if not llm_extract.deepseek_key():
            self.assertFalse(result["ok"])
            self.assertIn("DEEPSEEK_API_KEY", result["error"])


class SuggestIntegrationTest(unittest.TestCase):
    """抽取层接入 suggest() 后的降级与并集行为。"""

    def test_不传extractor时行为与纯规则一致(self):
        plain = triage.suggest("肚子疼")
        with_none = triage.suggest("肚子疼", extractor=None)
        self.assertEqual(plain["route"], with_none["route"])
        self.assertFalse(with_none["extract"]["used"])
        self.assertEqual(with_none["extract"]["error"], "")

    def test_抽取层抛异常时退回纯规则(self):
        def broken(_text):
            raise RuntimeError("模型服务挂了")

        degraded = triage.suggest("肚子疼 有点反酸", extractor=broken)
        plain = triage.suggest("肚子疼 有点反酸")
        self.assertTrue(degraded["extract"]["fallback"])
        self.assertIn("模型服务挂了", degraded["extract"]["error"])
        self.assertEqual(degraded["route"], plain["route"])
        self.assertEqual(degraded["suggestions"][0]["name"], plain["suggestions"][0]["name"])

    def test_抽取失败结构体也走降级(self):
        def failing(_text):
            return {"ok": False, "error": "HTTP 429（触发限流，请降低并发）"}

        result = triage.suggest("头痛三天", extractor=failing)
        self.assertTrue(result["extract"]["fallback"])
        self.assertIn("429", result["extract"]["error"])
        self.assertTrue(result["suggestions"])

    def test_抽取只做并集不删减规则命中(self):
        """模型说「没有头痛」也不能把规则命中的头痛抹掉。"""
        def deny_all(_text):
            return {"ok": True, "provider": "stub", "terms": [],
                    "negated": ["头痛"], "subject": "self", "latency_ms": 1.0}

        result = triage.suggest("头痛三天了", extractor=deny_all)
        self.assertTrue(result["extract"]["used"])
        self.assertIn("头痛", result["tokens"] or result["suggestions"][0]["hits"])
        self.assertTrue(result["suggestions"])

    def test_模型补充的症状会进入打分(self):
        """规则漏词时，模型补的词应当进入打分，而不是被丢弃。"""
        text = "脑袋嗡嗡响 天旋地转"
        plain = triage.suggest(text)
        plain_top = plain["suggestions"][0]["score"] if plain["suggestions"] else 0.0

        def add_term(_text):
            return {"ok": True, "provider": "stub", "terms": ["耳鸣"], "negated": [],
                    "subject": "self", "latency_ms": 1.0}

        boosted = triage.suggest(text, extractor=add_term)
        self.assertEqual(boosted["extract"]["added"], ["耳鸣"])
        self.assertIn("耳鸣", boosted["suggestions"][0]["hits"])
        self.assertGreater(boosted["suggestions"][0]["score"], plain_top)

    def test_急症命中时不调用抽取层(self):
        """红旗先跑：既不给科室，也不花一次网络往返。"""
        calls = []

        def spy(text):
            calls.append(text)
            return {"ok": True, "provider": "stub", "terms": [], "negated": [],
                    "subject": "self", "latency_ms": 1.0}

        result = triage.suggest("胸口疼 出冷汗 喘不上气", extractor=spy)
        self.assertTrue(result["emergency"])
        self.assertEqual(calls, [])
        self.assertFalse(result["extract"]["used"])

    def test_模型给出的主体在规则判为本人时才生效(self):
        def says_family(_text):
            return {"ok": True, "provider": "stub", "terms": ["发热"], "negated": [],
                    "subject": "family", "latency_ms": 1.0}

        result = triage.suggest("发烧三天了", extractor=says_family)
        self.assertEqual(result["subject"]["type"], "family")
        self.assertIn("家属代述", result["advice"])


class StatusTest(unittest.TestCase):
    def test_状态自检字段齐全(self):
        state = llm_extract.status()
        for key in ("available", "provider", "reason", "deepseek_key", "ollama_alive", "model"):
            self.assertIn(key, state)
        # 不应把完整 key 暴露出去：脱敏后只保留前 6 位 + 末 4 位
        full = llm_extract.deepseek_key()
        if full:
            self.assertNotIn(full, json.dumps(state))
            self.assertIn(full[:6], state["deepseek_key"])
            self.assertIn(full[-4:], state["deepseek_key"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
