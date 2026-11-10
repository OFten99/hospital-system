# -*- coding: utf-8 -*-
"""RAG 底座：医院私有知识库 + 本地词向量检索。

设计边界（三条，改代码前先读）
------------------------------------------------------------------
1. **只做检索，不做决策。** 本模块只负责「给定问题，从医院私有知识库找最相关的几段」，
   并把每段带上 ``source``（文档名 + 位置）返回。要不要引用、怎么组织答案，是
   Agent 层（triage_agent.py）的事。
2. **本地词向量，不调 embedding 模型。** 医疗数据不出机是硬约束，所以不调云 embedding。
   用「字符 bigram 哈希 + 症状词表加权」构成稀疏向量，零第三方依赖、纯 numpy 余弦，
   足够覆盖「科室 / 排班 / 就诊须知」这类结构化导诊知识的检索。
3. **可溯源。** 每个结果都带 ``source`` 与 ``text``，Agent 引用时必须能定位回原文，
   这是医疗场景「可解释、可审计」的底线。

为什么不用重量级 embedding（RAG 方案评估的一部分）
------------------------------------------------------------------
导诊知识是**结构化、短文本、专业词密集**的：科室名、检查项、注意事项都是高频专有词，
字符 n-gram + 词表加权就能达到不错的召回；而真 embedding 模型（bge-m3 等）需要额外
下载数 GB 权重、引入第三方依赖，而且在「本地可量化、数据不出域」这个约束下收益
不匹配。若未来知识库扩大到开放域长文本，再替换成真 embedding 也不迟——接口不变。
"""

import hashlib
import json
import math
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 知识库根目录：仓库根的 data/kb/ 下，一篇 .md 或 .txt 一篇文档
DEFAULT_KB_DIR = os.path.join(REPO_ROOT, "data", "kb")

# 检索参数
DEFAULT_TOP_K = 4          # 返回最相关的几段
DEFAULT_MIN_SCORE = 0.0    # 相似度下限（低于此分数的段直接丢弃）
_CHUNK_SIZE = 400          # 每段最大字符数（导诊知识一篇通常 100~400 字，直接按段落切）
_NGRAM = 2                 # 字符 bigram

# 导诊知识专有词表：覆盖「科室 / 检查 / 挂号 / 医保 / 就诊流程」等导诊场景高频词，
# 与 triage.VOCAB（症状词表）互补。这些词在知识库里是核心检索锚点，
# 用它们做最长匹配切词，能让「胃镜」「洗牙」「报销」「挂号」成为独立高权重特征。
# 口语 -> 标准词的同义归一化也在这里，解决「腰疼→腰痛」「腿麻→下肢放射」这类说法差异。
_KB_TERMS = (
    "胃镜", "肠镜", "洗牙", "拔牙", "种植", "挂号", "退号", "预约", "候诊",
    "报销", "医保", "缴费", "慢病", "体检", "急诊", "120", "抢救", "分诊",
    "抽血", "化验", "空腹", "憋尿", "CT", "MRI", "磁共振", "B超", "心电图",
    "脑电图", "肺功能", "耳鼻喉科", "眼科", "口腔科", "骨科", "心内科", "呼吸内科",
    "消化内科", "神经内科", "泌尿外科", "妇产科", "儿科", "皮肤科", "普外科",
    "内科", "外科", "门诊", "住院", "专家", "排班", "出诊", "复诊", "转诊",
    "首诊", "检查", "注意事项", "就诊", "须知", "用药", "服药", "停药",
)
_KB_SYNONYMS = (
    ("腰疼", "腰痛"), ("腿麻", "下肢麻木"), ("腿疼", "腿痛"), ("肚子疼", "腹痛"),
    ("头疼", "头痛"), ("头晕", "眩晕"), ("心口疼", "胸痛"), ("胸口疼", "胸痛"),
    ("咳血", "咯血"), ("便血", "大便出血"), ("尿血", "血尿"), ("牙疼", "牙痛"),
    ("嗓子疼", "咽痛"), ("眼睛疼", "眼痛"), ("耳朵疼", "耳痛"), ("孩子", "儿童"),
    ("宝宝", "儿童"), ("老人", "老年"), ("拉肚子", "腹泻"), ("便秘", "便秘"),
    ("发烧", "发热"), ("咳嗽", "咳嗽"), ("流鼻涕", "流涕"), ("感冒", "感冒"),
)

# 词汇级特征：从 导诊词表 + triage.VOCAB 做最长匹配切词，让「胃镜」「洗牙」这类专有词
# 成为独立的高权重特征（字符 bigram 只能捕捉局部相邻，跨词语义要靠词级特征补）。
_VOCAB_CACHE = None


def _vocab():
    """加载切词词表：导诊专有词 + triage 症状词，合并去重后最长词在前。"""
    global _VOCAB_CACHE
    if _VOCAB_CACHE is None:
        terms = list(_KB_TERMS)
        try:
            import triage
            terms.extend(triage.VOCAB)
        except Exception:
            pass
        _VOCAB_CACHE = sorted(set(terms), key=len, reverse=True)
    return _VOCAB_CACHE


def _normalize_kb(text):
    """导诊口语 -> 标准词归一化（先做，保证「腰疼」能对上知识库里的「腰痛」）。"""
    t = (text or "").lower()
    for oral, standard in _KB_SYNONYMS:
        if oral in t:
            t = t.replace(oral, standard)
    return t


# ---------------------------------------------------------------------------
# 一、向量化：字符 n-gram 哈希 + 症状词表加权
# ---------------------------------------------------------------------------

def _char_ngrams(text, n=_NGRAM):
    """把一段文本切成字符 n-gram（含 padding，保证首尾字符也入向量）。"""
    t = re.sub(r"\s+", "", text or "")
    t = "<" + t + ">"
    return [t[i:i + n] for i in range(len(t) - n + 1)]


def _word_tokens(text):
    """从文本中抽出「词级特征」：
    - 词表最长匹配切出的专业词（胃镜 / 洗牙 / 报销 / 胸痛 …）
    - 连续的英文 / 数字串（CT / MRI / 120 / 24 小时 …）

    这些词用 ``w:`` 前缀哈希，和字符 bigram 分属不同特征空间，互不污染。
    """
    t = (text or "").lower()
    tokens = []
    vocab = _vocab()
    # 1) 词表最长匹配
    i = 0
    while i < len(t):
        matched = None
        for w in vocab:
            if t.startswith(w, i):
                matched = w
                break
        if matched:
            tokens.append(matched)
            i += len(matched)
        else:
            i += 1
    # 2) 英文/数字串（长度 >= 2 才算，避免单字符噪声）
    for m in re.findall(r"[a-z0-9]{2,}", t):
        tokens.append(m)
    return tokens


def _hash_token(token, dim=8192):
    """把一个特征稳定哈希到 [0, dim) 的桶下标（feature hashing，免维护词典）。"""
    h = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16)
    return h % dim


def _weight(token):
    """词表加权：词级专业词权重更高，字符 bigram 权重低。

    词级 token 用 ``w:`` 前缀区分，权重 3.0（专业词更有区分度）；
    字符 bigram 权重 1.0（提供局部字形匹配的兜底）。
    """
    return 3.0 if token.startswith("w:") else 1.0


def embed(text, dim=8192):
    """把文本转成稀疏向量（用 dict 表示，非零下标 -> 权重）。

    特征 = 词级专业词（``w:`` 前缀，权重 3）+ 字符 bigram（权重 1），
    二者相加后 L2 归一化，可直接做余弦相似度。
    """
    text = _normalize_kb(text)  # 先归一化口语，保证「腰疼」能对上「腰痛」
    vec = {}
    # 词级特征
    for tok in _word_tokens(text):
        idx = _hash_token("w:" + tok, dim)
        vec[idx] = vec.get(idx, 0.0) + _weight("w:" + tok)
    # 字符 bigram 特征
    for tok in _char_ngrams(text):
        idx = _hash_token(tok, dim)
        vec[idx] = vec.get(idx, 0.0) + _weight(tok)
    # L2 归一化
    norm = math.sqrt(sum(w * w for w in vec.values())) or 1.0
    return {idx: w / norm for idx, w in vec.items()}


def cosine(a, b):
    """两个稀疏向量的余弦相似度（已 L2 归一化，所以点积即余弦）。"""
    if not a or not b:
        return 0.0
    # 取较小的一方遍历，省时间
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    return sum(w * large.get(idx, 0.0) for idx, w in small.items())


# ---------------------------------------------------------------------------
# 二、文档切片与索引
# ---------------------------------------------------------------------------

def _split_chunks(text, size=_CHUNK_SIZE):
    """按段落切，超长段落再按 size 硬切，保证每段可独立引用。

    标题处理规则：
    - 一个段落若「只有标题行、没有正文」（如 ``# 常见检查注意事项``），
      就把它合并进下一段正文——避免孤立的短标题段抢走检索分。
    - 一个段落若「标题 + 正文列表项在同一段」（如 ``## 内科\\n- 王主任…``），
      这是正常段，直接保留，不做合并。
    """
    paras = re.split(r"\n\s*\n", (text or "").strip())
    chunks = []
    pending = ""
    for para in paras:
        para = para.strip()
        if not para:
            continue
        # 去掉标题行后还剩内容吗？剩 -> 标题和正文同段，是正常段；不剩 -> 纯标题段
        body = re.sub(r"^#{1,6}\s.*(?:\n|$)", "", para).strip()
        if not body:
            # 纯标题段：暂存，等下一条正文一起拼
            pending = para + "\n"
            continue
        if pending:
            para = pending + para
            pending = ""
        if len(para) <= size:
            chunks.append(para)
        else:
            for i in range(0, len(para), size):
                chunks.append(para[i:i + size])
    return chunks


def load_kb(kb_dir=None):
    """递归加载知识库，返回文档列表。

    支持子目录分类（如 ``diseases/`` 放疾病知识卡），文档名保留相对路径，
    便于在 ``source`` 里区分来源。每篇文档：
    ``{"name": 相对路径, "chunks": [(文本, 向量, 段内序号), ...]}``
    """
    kb_dir = kb_dir or DEFAULT_KB_DIR
    docs = []
    if not os.path.isdir(kb_dir):
        return docs
    for root, _dirs, files in os.walk(kb_dir):
        for fname in sorted(files):
            if not fname.lower().endswith((".md", ".txt")):
                continue
            path = os.path.join(root, fname)
            rel = os.path.relpath(path, kb_dir)
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                continue
            chunks = []
            for i, chunk_text in enumerate(_split_chunks(text)):
                chunks.append((chunk_text, embed(chunk_text), i))
            if chunks:
                docs.append({"name": rel, "chunks": chunks})
    return docs


class KnowledgeBase:
    """可复用的知识库对象：一次加载，多次检索（避免每次查都重读文件）。"""

    def __init__(self, kb_dir=None):
        self.kb_dir = kb_dir or DEFAULT_KB_DIR
        self.docs = load_kb(self.kb_dir)
        self._flat = self._flatten()

    def _flatten(self):
        """把所有 chunk 摊平成一张表，便于批量打分。"""
        flat = []
        for doc in self.docs:
            for text, vec, idx in doc["chunks"]:
                flat.append({"doc": doc["name"], "idx": idx, "text": text, "vec": vec})
        return flat

    def __len__(self):
        return len(self._flat)

    def search(self, query, top_k=DEFAULT_TOP_K, min_score=DEFAULT_MIN_SCORE):
        """检索最相关的 top_k 段，返回带 source 的结果列表。

        返回：[{"source": "消化内科.md#0", "doc": "消化内科.md", "idx": 0,
                "score": 0.83, "text": "..."}]
        """
        if not self._flat or not (query or "").strip():
            return []
        qvec = embed(query)
        scored = []
        for item in self._flat:
            s = cosine(qvec, item["vec"])
            if s >= min_score:
                scored.append((s, item))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {
                "source": "%s#%d" % (item["doc"], item["idx"]),
                "doc": item["doc"],
                "idx": item["idx"],
                "score": round(s, 4),
                "text": item["text"],
            }
            for s, item in scored[:top_k]
        ]


# ---------------------------------------------------------------------------
# 三、命令行自检
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    kb = KnowledgeBase()
    print("知识库文档数：%d 篇，共 %d 段" % (len(kb.docs), len(kb)))
    query = sys.argv[1] if len(sys.argv) > 1 else "做胃镜要注意什么"
    print("\n问题：", query)
    for r in kb.search(query):
        print("\n[%s] 相似度 %.3f\n%s" % (r["source"], r["score"], r["text"][:120]))
