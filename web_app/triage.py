# -*- coding: utf-8 -*-
"""门诊智能分诊引擎：把「症状描述」和「科室」放进同一个向量空间比距离。

对应「万物皆向量」的五层骨架，本模块的五个设计决策逐条落在下面：

一、向量是什么
    ``vectorize()`` 把一段症状文本变成一串有序浮点数。8 个科室 = 8 个向量，
    ``similarity()`` 只做点积与模长，不依赖任何几何直觉——高维算距离不需要"画得出"。

二、凭什么一串数字能表达意思
   视频里的假设是「一个词的意思由它周围常一起出现的词决定」。
   真正的 word2vec 要十亿级语料训练，课程设计不具备这个条件，所以这里用**共现的
   手工编码**替代训练：每个科室画像就是它的「上下文」，症状词在哪个科室的上下文里
   权重高，就说明它离那个科室近。这是分布式假设的**弱化实现**，不是训练出来的
   embedding——这一点必须说清楚，否则就是拿口号骗人（对应第五层）。

三、万物怎么都进来
   只要能为对象定义编码器，就能把它变成同一空间里的向量。这里为 8 个科室各写了一个
   关键词权重编码器，患者输入走同一套词表与归一化，于是「症状」和「科室」可比。

四、进来之后能干什么
   ``suggest()`` 做的就是把分诊变成**最近邻检索**：算 8 个科室的相似度，取 Top-K。
   推荐排序用的是融合分数，本质仍是内积类排序。

五、这句话哪里是夸张的（本模块的局限，写进代码避免自己忘）
   1. **有损压缩**：症状描述压成几十维权重向量，同义异形、病程、既往史全丢了。
   2. **相似由定义决定**：这里的"相似"来自我手工设的关键词权重，不是客观真理。
      换一套权重、换一个模型，同一句话的排序就会变。所以它只能做"分诊建议"，
      绝不能当诊断。
   3. **高维直觉失效**：词表维度一多，余弦值高不高和"真的像"之间没有必然关系，
      所以阈值只能靠样例实测校准（见 ``CONF_HIGH``/``CONF_MEDIUM``）。
"""

import math
import re
import unicodedata

# ---------------------------------------------------------------------------
# 一、口语 → 标准术语归一化表
# ---------------------------------------------------------------------------
# 患者说的词和教科书术语往往不一致（"拉肚子" / "腹泻"）。先把口语折叠成标准词，
# 词表只维护标准词，命中率明显高于直接拿原始文本匹配。
SYNONYMS = {
    # 全身与体温
    "发烧": "发热", "低烧": "发热", "高烧": "发热", "退了烧": "发热",
    "浑身没劲": "乏力", "没力气": "乏力", "身子虚": "乏力",
    # 呼吸
    "嗓子疼": "咽痛", "喉咙痛": "咽痛", "嗓子发炎": "咽痛",
    "喘不上气": "呼吸困难", "喘不过气": "呼吸困难", "憋气": "呼吸困难", "气促": "呼吸困难",
    "有痰": "咳痰", "吐痰": "咳痰",
    "流鼻涕": "鼻塞", "鼻子不通": "鼻塞",
    # 循环
    "心慌": "心悸", "心跳快": "心悸", "胸口闷": "胸闷", "心里发慌": "心悸", "发慌": "心悸",
    "血压偏高": "高血压", "血压高": "高血压",
    # 睡眠（内科常见主诉）
    "睡不着": "失眠", "睡不好": "失眠", "入睡困难": "失眠", "老是醒": "失眠",
    # 消化
    "肚子疼": "腹痛", "肚子痛": "腹痛", "肚子": "腹部",
    "胃疼": "胃痛", "胃不舒服": "胃痛",
    "拉肚子": "腹泻", "拉稀": "腹泻", "跑肚": "腹泻",
    "想吐": "恶心", "反胃": "恶心", "烧心": "反酸",
    "大便干": "便秘",
    # 神经
    "头疼": "头痛", "脑袋疼": "头痛", "头昏": "头晕", "脑袋昏": "头晕",
    # 骨骼肌肉
    "腰疼": "腰痛", "腰酸": "腰痛", "崴脚": "扭伤", "扭到": "扭伤",
    "脖子疼": "颈痛", "颈椎疼": "颈椎", "关节疼": "关节痛",
    "抽筋": "肌肉痉挛",
    # 眼
    "眼花": "视力模糊", "看不清": "视力模糊", "眼睛红": "眼红", "眼屎多": "眼屎",
    # 皮肤
    "发痒": "瘙痒", "起疹子": "皮疹", "长痘": "痤疮", "粉刺": "痤疮",
    "掉头发": "脱发", "牛皮癣": "癣", "红点": "红疹", "起红点": "红疹",
    # 口腔
    "牙疼": "牙痛", "虫牙": "龋齿", "蛀牙": "龋齿", "牙床": "牙龈",
    # 妇产
    "大姨妈": "月经", "来事": "月经", "怀孕了": "怀孕",
    # 儿科人群词（儿科靠人群词识别，不靠症状本身）
    "小孩": "儿童", "孩子": "儿童", "宝宝": "儿童", "婴儿": "儿童",
    "幼儿": "儿童", "小朋友": "儿童", "新生儿": "儿童", "小儿": "儿童",
    # 泌尿
    "尿尿疼": "尿痛", "总想上厕所": "尿频", "憋不住尿": "尿急",
    # 外科
    "磕伤": "外伤", "碰伤": "外伤", "划伤": "外伤", "破皮": "外伤",
    "鼓包": "包块", "长了个包": "包块", "有个包": "包块", "起了个包": "包块",
    "肛周": "肛门",
}

# ---------------------------------------------------------------------------
# 二、科室画像（编码器的知识库）
# ---------------------------------------------------------------------------
# 权重含义：
#   3 = 高度特异，几乎只可能属于本科室（骨折 / 月经 / 牙痛）
#   2 = 中等特异，多数情况属于本科室（咳嗽 / 皮疹 / 腰痛）
#   1 = 泛化症状，多科室都可能出现（发热 / 乏力）
# 调高特异性词权重是提升准确率最有效的手段，比堆同义词更管用。
DEPARTMENTS = (
    {
        "code": "NK",
        "name": "内科",
        "scope": "心血管、呼吸、消化、内分泌等内科疾病",
        "keywords": {
            "发热": 1, "咳嗽": 2, "咳痰": 3, "咽痛": 2, "鼻塞": 2,
            "胸闷": 3, "胸痛": 2, "心悸": 3, "呼吸困难": 3,
            "头痛": 2, "头晕": 2,
            "腹痛": 2, "腹部": 2, "腹胀": 2, "腹泻": 3, "恶心": 2, "呕吐": 2,
            "反酸": 3, "胃痛": 2, "便秘": 2,
            "高血压": 3, "血糖高": 3, "糖尿病": 3,
            "感冒": 2, "乏力": 1, "水肿": 2, "尿频": 2, "尿急": 2, "尿痛": 3,
            "失眠": 2,
        },
    },
    {
        "code": "WK",
        "name": "外科",
        "scope": "普外科、创伤、体表肿物、肛肠等外科疾病",
        "keywords": {
            "外伤": 3, "伤口": 3, "化脓": 3, "出血": 2,
            "包块": 3, "肿块": 2, "囊肿": 2, "脓肿": 2,
            "阑尾炎": 3, "疝气": 3, "痔疮": 3, "肛裂": 3,
            "烫伤": 3, "烧伤": 3, "结石": 2, "静脉曲张": 3,
            "甲状腺": 2, "乳腺": 2, "换药": 2, "术后": 2,
            "肛门": 3,
        },
    },
    {
        "code": "EK",
        "name": "儿科",
        "scope": "儿童常见病、新生儿疾病、生长发育问题",
        # 儿科的关键识别信号是「人群词」而不是症状本身：
        # 同样是发热，成人归内科、儿童归儿科。所以这里只放人群词与儿科特有体征。
        "keywords": {
            "儿童": 3, "手足口": 3, "水痘": 3, "惊厥": 3, "夜啼": 3,
            "黄疸": 2, "发育": 2, "多动": 2, "厌食": 2, "疫苗": 1,
            "身高": 2, "体重不增": 3,
        },
    },
    {
        "code": "FK",
        "name": "妇产科",
        "scope": "妇科疾病、孕期保健、产科门诊",
        "keywords": {
            "月经": 3, "痛经": 3, "白带": 3, "阴道": 3, "子宫": 2, "卵巢": 3,
            "怀孕": 3, "妊娠": 3, "产检": 3, "孕检": 3, "流产": 3, "避孕": 2,
            "乳房胀痛": 3, "更年期": 2, "妇科": 3, "宫外孕": 3,
        },
    },
    {
        "code": "GK",
        "name": "骨科",
        "scope": "骨伤、关节、脊柱、运动系统疾病",
        "keywords": {
            "骨折": 3, "扭伤": 3, "脱臼": 3, "骨质疏松": 3,
            "腰痛": 3, "颈痛": 2, "颈椎": 3, "腰椎": 3, "脊柱": 3,
            "关节痛": 3, "膝盖": 2, "肩痛": 2, "坐骨神经": 3,
            "半月板": 3, "腱鞘炎": 3, "落枕": 2, "肌肉痉挛": 2, "腿麻": 2,
        },
    },
    {
        "code": "YK",
        "name": "眼科",
        "scope": "眼部疾病、视力问题、眼外伤",
        "keywords": {
            "眼睛": 3, "眼红": 3, "眼痛": 3, "眼屎": 2, "流泪": 3,
            "视力": 3, "视力模糊": 3, "近视": 3, "远视": 3, "散光": 2,
            "白内障": 3, "青光眼": 3, "飞蚊": 3, "眼皮": 2,
            "结膜炎": 3, "干眼": 3,
        },
    },
    {
        "code": "PFK",
        "name": "皮肤科",
        "scope": "皮肤、毛发、指甲常见病",
        "keywords": {
            "皮疹": 3, "瘙痒": 3, "痒": 3, "红疹": 3, "痤疮": 3, "湿疹": 3,
            "荨麻疹": 3, "脱发": 3, "疱疹": 2, "皮肤": 3, "癣": 3,
            "痣": 2, "过敏": 2, "斑": 2, "疣": 2, "灰指甲": 3,
            "头皮屑": 2, "皮肤过敏": 3,
        },
    },
    {
        "code": "KQK",
        "name": "口腔科",
        "scope": "牙齿、牙周、口腔黏膜疾病",
        "keywords": {
            "牙痛": 3, "牙齿": 3, "牙龈": 3, "牙龈出血": 3, "牙周": 3,
            "口腔溃疡": 3, "口腔": 2, "口臭": 3, "拔牙": 3, "龋齿": 3,
            "智齿": 3, "镶牙": 3, "正畸": 3, "舌头": 2, "咬合": 2,
        },
    },
)

# 主信号（词命中）与辅信号（bigram 余弦）的融合比例
W_KEYWORD = 0.70
W_COSINE = 0.30
# 饱和常数：命中权重累加到该值时主信号得 0.5，防止长文本靠堆词取胜。
# 取 2.0 而不是 3.0：实测 3.0 时单个高特异词（如"牙痛"权重 3）只能得 0.5，
# 导致明确病例的匹配度普遍压在 40% 以下，展示上过于保守。
SATURATION = 2.0

# 置信度阈值：基于 tests/test_triage.py 里 64 条样例实测校准
CONF_HIGH = 0.55
CONF_MEDIUM = 0.30

# 无信息量的口语词，不参与打分
STOPWORDS = frozenset({
    "本人", "最近", "一直", "有点", "有点儿", "感觉", "觉得", "请问", "帮忙",
    "看看", "看一下", "怎么", "怎么办", "什么", "样子", "情况", "现在", "已经",
    "几天", "多天", "好久", "反复", "经常", "偶尔", "还有", "伴随",
})

_SYNONYM_ITEMS = sorted(SYNONYMS.items(), key=lambda kv: len(kv[0]), reverse=True)

_PUNCT_RE = re.compile(
    r"[\s,，。、;；:：!！?？\.\-—~～/\\|\(\)（）\[\]【】\"'“”‘’《》<>+*=#@%^&…·]+"
)


# ---------------------------------------------------------------------------
# 三、归一化与分词
# ---------------------------------------------------------------------------

def normalize(text):
    """全角转半角、去标点、把口语折叠成标准术语。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text)).lower()
    for oral, standard in _SYNONYM_ITEMS:
        if oral in text:
            text = text.replace(oral, standard)
    return _PUNCT_RE.sub("", text)


def _build_vocab():
    vocab = set()
    for dept in DEPARTMENTS:
        vocab.update(dept["keywords"])
    vocab.update(SYNONYMS.values())
    return sorted(vocab, key=len, reverse=True)


VOCAB = _build_vocab()


# 停用词中长度 >= 2 的部分，必须在**切分之前**剥离。
# 踩过的坑：原文「最近视力下降」里「最近」+「视力」拼接后跨词边界正好形成「近视」，
# 最长正向匹配会把「最近」吃掉一个字、切出「近视」，误判成眼科术语。
# 只在切分之后过滤停用词是堵不住的，必须先把它们抹掉。
_STRIP_ITEMS = sorted((w for w in STOPWORDS if len(w) >= 2), key=len, reverse=True)


def _strip_stopwords(text):
    for word in _STRIP_ITEMS:
        if word in text:
            text = text.replace(word, "")
    return text


def _forward_match(text):
    """正向最长匹配，返回 (词列表, 命中词覆盖的字符数)。"""
    tokens, covered = [], 0
    i, n = 0, len(text)
    while i < n:
        for word in VOCAB:
            if text.startswith(word, i):
                tokens.append(word)
                covered += len(word)
                i += len(word)
                break
        else:
            i += 1
    return tokens, covered


def _backward_match(text):
    """逆向最长匹配：从右往左找以当前位置结尾的最长词。

    中文歧义里正向匹配容易把前缀吞错（"最近视" -> "近视"），逆向往回切通常更保守。
    """
    tokens, covered = [], 0
    pos = len(text)
    while pos > 0:
        for word in VOCAB:
            if len(word) <= pos and text.endswith(word, 0, pos):
                tokens.append(word)
                covered += len(word)
                pos -= len(word)
                break
        else:
            pos -= 1
    tokens.reverse()
    return tokens, covered


def tokenize(text):
    """双向最大匹配分词。

    裁决规则（与中文分词通行做法一致）：
      1. 命中词数更多者优先（覆盖率更高，避免漏词）；
      2. 词数相同，取覆盖字符更多者；
      3. 仍然相同，取逆向结果（正向更容易跨边界合并出假词）。
    未命中词表的单字直接丢弃——分诊场景下它们基本无信息量。
    """
    if not text:
        return []
    text = _strip_stopwords(text)
    forward, f_cover = _forward_match(text)
    backward, b_cover = _backward_match(text)
    if len(forward) > len(backward):
        tokens = forward
    elif len(backward) > len(forward):
        tokens = backward
    elif b_cover > f_cover:
        tokens = backward
    else:
        tokens = backward  # 覆盖相同或正向覆盖更少时，逆向更不容易切出跨边界的假词
    return [t for t in tokens if t not in STOPWORDS]


def _bigrams(text):
    """字符二元组：词表没覆盖到的新说法靠它兜底。"""
    if len(text) < 2:
        return [text] if text else []
    return [text[i:i + 2] for i in range(len(text) - 1)]


# ---------------------------------------------------------------------------
# 四、向量化与相似度
# ---------------------------------------------------------------------------

def _idf_table():
    """IDF：出现在越少科室里的词区分度越高。语料就是这 8 个科室画像。"""
    total = len(DEPARTMENTS)
    df = {}
    for dept in DEPARTMENTS:
        for word in dept["keywords"]:
            df[word] = df.get(word, 0) + 1
    for word in SYNONYMS.values():
        df.setdefault(word, 1)
    return {w: math.log((total + 1) / (c + 1)) + 1.0 for w, c in df.items()}


IDF = _idf_table()


def _tfidf(counts):
    if not counts:
        return {}
    total = float(sum(counts.values()))
    return {w: (c / total) * IDF.get(w, 1.0) for w, c in counts.items()}


# 科室的 bigram 向量（辅信号，只建一次）
_DEPT_BIGRAM_VECTORS = {}
for _d in DEPARTMENTS:
    _counts = {}
    _text = normalize(_d["scope"] + "".join(_d["keywords"]))
    for _g in _bigrams(_text):
        _counts[_g] = _counts.get(_g, 0) + 1
    _DEPT_BIGRAM_VECTORS[_d["code"]] = _tfidf(_counts)


def similarity(vec_a, vec_b):
    """余弦相似度：只依赖点积与模长，不需要把高维空间画出来。"""
    if not vec_a or not vec_b:
        return 0.0
    if len(vec_a) > len(vec_b):
        vec_a, vec_b = vec_b, vec_a
    dot = 0.0
    for key, val in vec_a.items():
        other = vec_b.get(key)
        if other:
            dot += val * other
    if dot == 0.0:
        return 0.0
    norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
    norm_b = math.sqrt(sum(v * v for v in vec_b.values()))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def vectorize(text):
    """把一段症状文本变成向量：(特征字典, 标准化文本, 命中词列表)。

    返回的字典就是「一串有顺序的数字」的朴素形态——键是维度名，值是该维度的分量。
    这里不展开成定长数组，因为分诊场景下稀疏向量更好读、也更好解释。
    """
    norm = normalize(text)
    tokens = tokenize(norm)
    counts = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    return _tfidf(counts), norm, tokens


# ---------------------------------------------------------------------------
# 五、对外接口
# ---------------------------------------------------------------------------

def suggest(text, top_k=3):
    """给定症状描述，返回按相似度降序的科室建议。

    返回结构（全部字段都可解释，前端逐条展示即可）::

        {
          "query":      原始输入,
          "normalized": 归一化后的文本,
          "tokens":     命中的症状词,
          "confidence": "high" / "medium" / "low",
          "advice":     给挂号员的一句话提示,
          "suggestions": [
             {"code","name","scope","score","percent",
              "keyword_score","cosine","hits":[...]}
          ]
        }
    """
    raw = (text or "").strip()
    _, norm, tokens = vectorize(raw)

    # 查询的 bigram 向量（辅信号）
    bigram_counts = {}
    for gram in _bigrams(norm):
        bigram_counts[gram] = bigram_counts.get(gram, 0) + 1
    query_vec = _tfidf(bigram_counts)

    results = []
    for dept in DEPARTMENTS:
        weights = dept["keywords"]
        hits = sorted({t for t in tokens if t in weights})
        hit_weight = float(sum(weights[t] for t in hits))
        # 饱和归一化：命中权重越大越接近 1，但增速递减，避免长文本靠堆词取胜
        keyword_score = hit_weight / (hit_weight + SATURATION)
        cosine = similarity(query_vec, _DEPT_BIGRAM_VECTORS[dept["code"]])
        score = W_KEYWORD * keyword_score + W_COSINE * cosine
        results.append({
            "code": dept["code"],
            "name": dept["name"],
            "scope": dept["scope"],
            "score": round(score, 4),
            "percent": int(round(score * 100)),
            "keyword_score": round(keyword_score, 4),
            "cosine": round(cosine, 4),
            "hits": hits,
        })

    results.sort(key=lambda item: (-item["score"], item["code"]))

    if results:
        top_score = results[0]["score"]
        if not tokens and top_score <= 0.0:
            confidence = "low"
            advice = "没有识别出有效症状描述，请补充具体不适部位与主要症状，或到导诊台人工分诊。"
        elif top_score >= CONF_HIGH:
            confidence = "high"
            advice = "症状指向明确，建议挂 %s。" % results[0]["name"]
        elif top_score >= CONF_MEDIUM:
            confidence = "medium"
            advice = "存在多个可能科室，建议挂 %s；若医生判断不属于本科室，可现场转诊。" % results[0]["name"]
        else:
            confidence = "low"
            advice = "症状信息不足，以下结果仅供参考，建议到导诊台人工确认。"
    else:
        confidence = "low"
        advice = "科室库为空，请先维护科室数据。"

    return {
        "query": raw,
        "normalized": norm,
        "tokens": tokens,
        "confidence": confidence,
        "advice": advice,
        "suggestions": results[:top_k],
        "all": results,
    }


def similar_departments(dept_code, top_k=3):
    """科室之间的相似度（聚类视角）：用于发现「容易混淆的科室对」。

    注意：科室画像的关键词集合是人工设计成互不重叠的，直接拿关键词向量算余弦
    会全部得到 0（交集为空）。所以这里用**描述文本的 bigram 向量**做余弦——
    它反映的是"两个科室在语义描述上有多接近"，这才是排查混淆时真正关心的东西。
    共享关键词仍一并返回，供人工核对。
    """
    target = next((d for d in DEPARTMENTS if d["code"] == dept_code), None)
    if target is None:
        return []
    base_words = set(target["keywords"])
    base_vec = _DEPT_BIGRAM_VECTORS[dept_code]
    scored = []
    for dept in DEPARTMENTS:
        if dept["code"] == dept_code:
            continue
        scored.append({
            "code": dept["code"],
            "name": dept["name"],
            "similarity": round(similarity(base_vec, _DEPT_BIGRAM_VECTORS[dept["code"]]), 4),
            "shared": sorted(base_words & set(dept["keywords"])),
        })
    scored.sort(key=lambda item: -item["similarity"])
    return scored[:top_k]
