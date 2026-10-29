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

六、v2 补上的三层（2026-10 追加，五个安全/正确性缺口）
   一版只把"出现了哪些症状"当成全部信息，于是有三个真实错误：

   1. **否定句全错**："不发烧也不咳嗽，就是头晕" 会把发热、咳嗽当成主症状，
      还给到 58% 高置信度。医疗 NLP 的第一类事故就是这个。
      → 现在在归一化阶段保留标点作为「作用域屏障」，否定词只向后作用到屏障为止，
        命中的否定症状从打分里剔除，并单独回传（``negated``）。
   2. **没有任何急症拦截**："胸口疼 出冷汗 喘不上气" 会输出内科 45%。
      这是心梗的教科书表现，任何"分诊建议"都不该给它一个普通科室。
      → 现在 ``RED_FLAGS`` 走在打分之前，命中即返回急诊指引，不给科室排名。
   3. **主体错位**："我妈发烧三天了" 当成说话人本人，年龄分流逻辑失效。
      → 现在识别家属代述并回传 ``subject``，提示确认就诊人。

   剩下的两个缺口属于"证据不足"而非"功能错误"，单独放到评测脚本里解决：
   没有独立评测集、没有 A/B 基线、没有急症漏检率这类指标（见 ``scripts/eval_triage.py``）。
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
    # 「肚子」的多义陷阱：小腿肚子 / 腿肚子指腓肠肌，不是腹部，必须先于「肚子→腹部」折叠
    "小腿肚子": "小腿", "腿肚子": "小腿", "小腿肚": "小腿",
    "胃疼": "胃痛", "胃不舒服": "胃痛",
    "拉肚子": "腹泻", "拉稀": "腹泻", "跑肚": "腹泻",
    "想吐": "恶心", "反胃": "恶心", "烧心": "反酸",
    "大便干": "便秘",
    # 神经
    "头疼": "头痛", "脑袋疼": "头痛", "头昏": "头晕", "脑袋昏": "头晕",
    "脑壳疼": "头痛", "脑壳": "头", "脑门疼": "头痛",
    # 骨骼肌肉
    "腰疼": "腰痛", "腰酸": "腰痛", "崴脚": "扭伤", "扭到": "扭伤", "崴": "扭伤",
    "脖子疼": "颈痛", "颈椎疼": "颈椎", "关节疼": "关节痛",
    "抽筋": "肌肉痉挛",
    "脚脖子": "脚踝", "脚后跟": "足跟", "腿疼": "腿痛", "腿痛": "腿痛",
    "肩膀": "肩痛", "青筋": "静脉曲张", "摔了一跤": "跌倒", "摔伤": "跌倒",
    "砸伤": "外伤", "砸了": "外伤", "砸": "外伤",
    # 眼
    "眼花": "视力模糊", "看不清": "视力模糊", "眼睛红": "眼红", "眼屎多": "眼屎",
    # 皮肤
    "发痒": "瘙痒", "起疹子": "皮疹", "长痘": "痤疮", "粉刺": "痤疮",
    "掉头发": "脱发", "牛皮癣": "癣", "红点": "红疹", "起红点": "红疹",
    # 口腔
    "牙疼": "牙痛", "虫牙": "龋齿", "蛀牙": "龋齿", "牙床": "牙龈",
    # 评测集暴露的其余口语缺口（这些映射本身没有争议，属于词表漏收）
    "不通气": "鼻塞", "鼻子不通气": "鼻塞",
    "风团": "荨麻疹", "忘事": "记忆力下降", "记性差": "记忆力下降", "脸上": "面部",
    "堵得慌": "胸闷", "心口窝": "胸口", "脖子": "颈",
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
            # 评测集暴露的漏词：耳鸣原来根本不在词表里，"我耳朵嗡嗡响"会直接 0 命中
            "耳鸣": 2, "记忆力下降": 2, "喘": 1, "闷": 1,
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
            "肛门": 3, "腋下": 2, "疙瘩": 2,
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
            "产后": 3, "不孕": 3, "备孕": 3, "坐月子": 3,
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
            "脚踝": 2, "足跟": 2, "腿痛": 2, "跌倒": 2, "胳膊": 1,
            "抬不起来": 1, "颈": 1,
        },
    },
    {
        "code": "YK",
        "name": "眼科",
        "scope": "眼部疾病、视力问题、眼外伤",
        "keywords": {
            "眼睛": 3, "眼红": 3, "眼痛": 3, "眼屎": 2, "流泪": 3,
            "视力": 3, "视力模糊": 3, "近视": 3, "远视": 3, "散光": 2,
            "白内障": 3, "青光眼": 3, "飞蚊": 3, "眼皮": 3,
            "结膜炎": 3, "干眼": 3, "黑影": 3, "视野": 2, "遮挡": 2,
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
            "头皮屑": 2, "皮肤过敏": 3, "面部": 2, "疙瘩": 2,
        },
    },
    {
        "code": "KQK",
        "name": "口腔科",
        "scope": "牙齿、牙周、口腔黏膜疾病",
        "keywords": {
            "牙痛": 3, "牙齿": 3, "牙龈": 3, "牙龈出血": 3, "牙周": 3,
            "口腔溃疡": 3, "口腔": 2, "口臭": 3, "拔牙": 3, "龋齿": 3,
            "智齿": 3, "镶牙": 3, "正畸": 3, "舌头": 2, "咬合": 2, "牙": 1,
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

# LLM 抽取层补词的权重折扣。模型补充的词属于「弱信号」：它只是把规则漏掉的
# 症状补上，不该和规则精确命中的词平权。实测（eval 第八节）里，模型把「小腿胀」
# 联想成「腹胀」、「小肚子坠疼」联想成「腹痛」，平权时反而把内科顶到第一，Top1
# 从 99.3% 掉到 97.9%。折到 0.4 后，规则命中的「静脉曲张」「白带」仍是主导，
# 模型补词只在规则完全漏词时才真正起作用。
LLM_ADDED_WEIGHT = 0.4

# 无信息量的口语词，不参与打分
STOPWORDS = frozenset({
    "本人", "最近", "一直", "有点", "有点儿", "感觉", "觉得", "请问", "帮忙",
    "看看", "看一下", "怎么", "怎么办", "什么", "样子", "情况", "现在", "已经",
    "几天", "多天", "好久", "反复", "经常", "偶尔", "还有", "伴随",
})

_SYNONYM_ITEMS = sorted(SYNONYMS.items(), key=lambda kv: len(kv[0]), reverse=True)

# ---------------------------------------------------------------------------
# 二点五、否定词、急症红旗、主体词（v2）
# ---------------------------------------------------------------------------

# 否定词。按长度降序匹配，"没有" 要比 "没" 先命中，否则会把"没有"切成"没"+"有"。
_NEGATORS = tuple(sorted(
    ("没有", "并没有", "从没", "从来没", "否认", "不是", "未", "无", "不"),
    key=len, reverse=True,
))
# 否定作用域：从否定词末尾往后最多数 4 个字。
# 为什么是 4：中文主诉里"没有发烧"、"不咳嗽"这类结构，被否定的症状基本紧跟否定词；
# 窗口再大会把"没有发烧但是咳嗽"里的咳嗽也吞进去。
_NEG_WINDOW = 4

# "无 / 未" 在中文里大量做构词前缀，不是否定（无力、无痛、未愈、无碍）。
# 遇到这些组合直接跳过，不做否定标记，否则"四肢无力"会被判成"力被否定"。
_WU_COMPOUND_PREFIX = ("力", "痛", "症", "菌", "法", "效", "数", "比", "形", "边",
                       "奈", "聊", "线", "烟", "糖", "盐", "碍", "须", "需")

# 急症红旗：组内 OR、组间 AND。命中即中断分诊，不给普通科室排名。
# 从 7 条起步，宁可漏掉罕见情形，也不要为了覆盖率把普通症状误升级成急诊——
# 误升级会让真正需要急诊的人被"狼来了"消耗掉信任。
RED_FLAGS = (
    {
        "id": "rf_heart",
        "title": "胸痛伴大汗，当心心梗",
        "advice": "请立即拨打 120，不要自行走动，不要等号。",
        "groups": (("胸痛", "胸口疼", "心口疼", "胸口压", "压榨"),
                   ("大汗", "冒汗", "冷汗", "出汗", "湿透", "身汗")),
    },
    {
        "id": "rf_stroke",
        "title": "嘴歪、半身无力、说话不清，当心中风",
        "advice": "请立即拨打 120，记下发病时间，不要喂水喂药。",
        # 加"右边/左边/右侧/左侧"：老人常说"右边手脚没劲"，很少说"半身"，
        # 只按教科书词汇收会漏掉最常见的口语表述。
        "groups": (("嘴歪", "口角歪", "半边", "一侧", "半身", "单侧",
                    "右边", "左边", "右侧", "左侧", "一边", "身子"),
                   ("没劲", "无力", "抬不起来", "说话不清", "说不清", "吐字", "不利索")),
    },
    {
        "id": "rf_bleed",
        "title": "呕血或黑便，可能是消化道出血",
        "advice": "请立即拨打 120，暂时禁食禁水。",
        "groups": (("呕血", "吐血", "吐了血", "口血", "黑便", "大便发黑", "柏油"),),
    },
    {
        "id": "rf_breath",
        "title": "静坐仍喘不上气，属急症",
        "advice": "请立即拨打 120，保持坐位，松开衣领。",
        "groups": (("喘不上气", "上不来气", "呼吸困难", "憋气"),
                   ("躺着", "平躺", "躺下", "不动", "坐着也", "坐也", "静坐", "夜里憋醒")),
    },
    {
        "id": "rf_head",
        "title": "突发剧烈头痛伴呕吐",
        "advice": "请立即拨打 120，避免剧烈活动。",
        "groups": (("剧烈头痛", "头疼得厉害", "炸开", "从没这么疼"),
                   ("呕吐", "吐了", "喷射")),
    },
    {
        "id": "rf_faint",
        "title": "晕倒或意识不清",
        "advice": "请立即拨打 120，让患者平卧、头偏向一侧。",
        "groups": (("晕倒", "昏倒", "人事不省", "叫不应", "意识不清", "晕过去"),),
    },
    {
        "id": "rf_seizure",
        "title": "抽搐或惊厥，需立即就医",
        "advice": "请立即前往急诊。抽搐时不要按住肢体、不要往嘴里塞东西，记录发作时长。",
        # 儿童热性惊厥最常见，但成人首次抽搐要考虑癫痫与脑血管病，同样按急症处理。
        "groups": (("抽搐", "惊厥", "抽风", "抽了", "翻白眼", "口吐白沫"),),
    },
    {
        "id": "rf_abdomen",
        "title": "突发剧烈腹痛，需排查急腹症",
        "advice": "请尽快前往急诊，不要自行服用止痛药。",
        "groups": (("腹痛", "肚子疼", "胃疼", "胃痛"),
                   ("剧烈", "受不了", "打滚", "疼得直不起腰")),
    },
    {
        "id": "rf_poison",
        "title": "误服毒物或药物过量",
        "advice": "请立即拨打 120 并带上药瓶/包装，不要自行催吐。",
        # 中毒在评测里原本是明确漏检项：词表只覆盖症状，覆盖不到"原因"。
        "groups": (("农药", "百草枯", "误服", "吃错药", "中毒", "煤气"),),
    },
    {
        "id": "rf_trauma",
        "title": "严重创伤或无法止住的大出血",
        "advice": "请立即拨打 120，直接压迫止血，不要自行拔出扎入物。",
        # 只收"血止不住"而不收"血不止"：后者会命中"出血不止"这个普通外科主诉，
        # 把一个小伤口升级成创伤急救，属于过度升级。
        "groups": (("血止不住", "绞了", "绞伤", "断指", "断肢", "压断", "大出血"),),
    },
    {
        "id": "rf_crash",
        "title": "车祸、坠落等严重外伤",
        "advice": "请立即拨打 120，不要随意搬动伤者；怀疑脊柱损伤时保持原位，等待急救。",
        # 单看"撞了/摔了"误伤太大（撞到桌角、摔了一跤），所以做成机制 + 严重度两组 AND：
        # 只有"车祸/坠落"这类高能量机制，加上"动不了/叫不醒/变形"这类体征，才升级。
        "groups": (("车祸", "被车撞", "撞飞", "碾压", "轧", "翻车",
                    "坠落", "摔下", "跳楼", "砸伤"),
                   ("动不了", "动弹不得", "站不起来", "起不来", "出血", "昏迷",
                    "叫不醒", "叫不应", "疼得", "痛得", "变形", "骨折", "没知觉")),
    },
)

# 家属代述标记：主诉里的症状主体不是说话人本人。
# 只影响提示与年龄分流，不改变症状打分——"我妈发烧"和"我发烧"在症状维度上没有区别。
_FAMILY_SUBJECT_WORDS = (
    "我妈", "我爸", "我母亲", "我父亲", "我奶奶", "我爷爷", "我外婆", "我外公",
    "我儿子", "我女儿", "我孙子", "我孙女", "我家孩子", "婆婆", "公公", "丈母娘",
    "岳父", "老伴", "老公", "老婆", "爱人", "家里人", "家里老人",
)
_CHILD_SUBJECT_WORDS = ("孩子", "儿童", "宝宝", "小孩", "婴儿", "小朋友", "小儿",
                        "新生儿", "幼儿")

_PUNCT_RE = re.compile(
    r"[\s,，。、;；:：!！?？\.\-—~～/\\|\(\)（）\[\]【】\"'“”‘’《》<>+*=#@%^&…·]+"
)


# ---------------------------------------------------------------------------
# 三、归一化与分词
# ---------------------------------------------------------------------------

def normalize_barriered(text):
    """归一化，但把标点替换成屏障符 ``|`` 而不是删掉。

    这是 v2 为否定识别加的一层：否定词的作用域必须止步于标点边界。
    "没有发烧，但是咳嗽" 归一化后如果删掉逗号变成 "没有发热但是咳嗽"，
    否定窗口就可能跨过分句把"咳嗽"也一起否掉。留一个屏障符，作用域自然截断。

    对外仍然只暴露 :func:`normalize`（返回不带屏障的文本），避免调用方感知这一层。
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text)).lower()
    for oral, standard in _SYNONYM_ITEMS:
        if oral in text:
            text = text.replace(oral, standard)
    return _PUNCT_RE.sub("|", text)


def normalize(text):
    """全角转半角、去标点、把口语折叠成标准术语。"""
    return normalize_barriered(text).replace("|", "")


# 方向词（左边/右侧…）只在指代"半身"时才用于卒中判定。
# 但"右边膝盖没劲""右边肩膀抬不起来"是关节病，不加约束会被判成中风——
# 这是补"右边手脚没劲"这条口语说法时带进来的副作用。
# 处理办法：方向词后面紧跟单个局部部位时，把方向词降级掉，不参与红旗匹配。
# 注意：手、脚、身子、肢体不在降级表里——"右边手脚没劲"必须仍然触发卒中。
_LOCAL_PARTS_AFTER_SIDE = ("膝盖", "膝关节", "肩", "肩膀", "肩关节", "脚踝", "踝",
                           "手腕", "肘", "牙", "耳", "眼", "腰", "髋", "关节",
                           "脚趾", "手指", "扁桃体", "腮", "膝")
_SIDE_RE = re.compile(r"(?:左边|右边|左侧|右侧)(?=%s)" % "|".join(_LOCAL_PARTS_AFTER_SIDE))


def demote_local_side(norm_bar):
    """把"右边膝盖"这类局部部位的方位词去掉，避免误触发卒中红旗。

    只在红旗匹配用的屏蔽文本上做，不影响分词与科室打分——
    「右边膝盖疼」仍然靠"膝盖"去抢骨科的分，只是不再被当成半身无力。
    """
    if not norm_bar:
        return norm_bar
    return _SIDE_RE.sub("", norm_bar)


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
# 五、否定识别、急症红旗、主体识别（v2 新增）
# ---------------------------------------------------------------------------

def _find_all(hay, word):
    """返回 word 在 hay 中的所有 (起, 止) 位置。"""
    spans, start = [], 0
    while word:
        i = hay.find(word, start)
        if i < 0:
            break
        spans.append((i, i + len(word)))
        start = i + 1
    return spans


def negation_spans(norm_bar):
    """否定作用域：否定词之后、遇屏障或超窗即止。

    返回 [(起, 止, 否定词)]，坐标是 ``normalize_barriered()`` 文本里的下标。
    """
    spans = []
    for neg in _NEGATORS:
        for i, _ in _find_all(norm_bar, neg):
            end_neg = i + len(neg)
            # "无 / 未" 做构词前缀时（无力、未愈）不当否定
            if neg in ("无", "未") and end_neg < len(norm_bar):
                if norm_bar[end_neg] in _WU_COMPOUND_PREFIX:
                    continue
            stop = min(len(norm_bar), end_neg + _NEG_WINDOW)
            j = end_neg
            while j < stop and norm_bar[j] != "|":
                j += 1
            if j > end_neg:
                spans.append((end_neg, j, neg))
    return spans


def _starts_in_span(start, spans):
    """词必须**起始于**否定作用域内才算被否定。

    为什么不是"有重叠就算"：`没劲`、`无力` 这类词本身以否定字开头，
    按重叠判定会把"半身没劲"（中风体征）误判成"没被否定的劲"。
    """
    for s, e, _ in spans:
        if s <= start < e:
            return True
    return False


def split_by_negation(norm_bar, tokens, spans):
    """把命中词拆成（肯定, 否定）。

    一个词在文中出现多次时，只有**全部出现位置都被否定**才判为否定；
    只要有一处是肯定表述，就仍按肯定计入打分。
    """
    positive, negated = [], []
    for token in tokens:
        starts = [s for s, _ in _find_all(norm_bar, token)]
        if starts and all(_starts_in_span(s, spans) for s in starts):
            negated.append(token)
        else:
            positive.append(token)
    return positive, negated


def _word_present(norm_bar, spans, word):
    """词在文中出现且至少有一处未被否定。"""
    starts = [s for s, _ in _find_all(norm_bar, word)]
    return any(not _starts_in_span(s, spans) for s in starts)


def check_red_flags(norm_bar, spans=()):
    """急症红旗拦截：组内 OR、组间 AND。

    否定过的症状不参与判定——"没有胸痛，就是咳嗽"不该触发心梗拦截。
    返回命中的 flag 字典，未命中返回 None。
    """
    if not norm_bar:
        return None
    for flag in RED_FLAGS:
        hit_all = True
        for group in flag["groups"]:
            if not any(_word_present(norm_bar, spans, w) for w in group):
                hit_all = False
                break
        if hit_all:
            return flag
    return None


def detect_subject(raw):
    """识别主诉主体：self（本人）/ family（家属代述）/ child（儿童）。

    只用于提示与年龄分流，不参与症状打分——"我妈发烧"和"我发烧"在症状维度等价，
    差别在于就诊人是谁、有没有人陪、能不能自己说清。
    """
    if not raw:
        return {"type": "self", "hint": ""}
    text = unicodedata.normalize("NFKC", str(raw))
    for word in _FAMILY_SUBJECT_WORDS:
        if word in text:
            return {"type": "family", "hint": "信息来自家属代述，请确认就诊人年龄与既往史。"}
    for word in _CHILD_SUBJECT_WORDS:
        if word in text:
            return {"type": "child", "hint": "就诊人为儿童，儿科分诊请核对年龄是否满 14 周岁。"}
    return {"type": "self", "hint": ""}


_SUBJECT_HINTS = {
    "family": "信息来自家属代述，请确认就诊人年龄与既往史。",
    "child": "就诊人为儿童，儿科分诊请核对年龄是否满 14 周岁。",
    "self": "",
}


def subject_from_type(kind):
    """LLM 侧给出的主体类型转成统一的 subject 结构，口径与 detect_subject 一致。"""
    kind = kind if kind in _SUBJECT_HINTS else "self"
    return {"type": kind, "hint": _SUBJECT_HINTS[kind]}


def _empty_extract(note=""):
    """抽取层的默认状态。used=False 即表示本次是纯规则路径。"""
    return {"used": False, "provider": "", "terms": [], "negated": [], "added": [],
            "fallback": False, "error": "", "latency_ms": 0.0, "note": note}


def build_ask_back(norm, positive, negated=()):
    """低置信度时该问什么。追问要具体到"缺哪一类信息"，不要泛泛而问。

    已经明确否认的症状不再问——患者刚说"没有发热"，再问"有没有发热"是明显的机器味。
    """
    if not norm:
        return "请说一句具体哪里不舒服，比如「头疼三天了」「肚子疼还想吐」。"
    if not positive:
        return "没听出具体症状。能说说主要是哪个部位、怎么个难受法吗？比如疼、胀、麻、没劲。"
    probes = [s for s in ("发热", "呕吐", "出汗") if s not in negated]
    follow = "、".join(probes) if probes else "其他不适"
    if len(positive) == 1:
        return "请再补充一点：这个症状多久了？有没有加重？有没有伴随%s？" % follow
    return "信息还不够确定科室，请补充病程长短和有没有其他伴随症状，或到导诊台人工分诊。"


# ---------------------------------------------------------------------------
# 六、对外接口
# ---------------------------------------------------------------------------

def suggest(text, top_k=3, extractor=None):
    """给定症状描述，返回按相似度降序的科室建议。

    返回结构（全部字段都可解释，前端逐条展示即可）::

        {
          "query":        原始输入,
          "normalized":   归一化后的文本,
          "tokens":       命中的症状词（含被否定的），规则侧结果,
          "negated":      被否定、已从打分中剔除的症状词,
          "subject":      {"type": "self"/"family"/"child", "hint": ...},
          "extract":      LLM 抽取层的工作情况（见下）,
          "confidence":   "high" / "medium" / "low",
          "route":        "direct" / "ask_more" / "human" / "emergency",
          "emergency":    bool,
          "emergency_title" / "emergency_advice": 红旗命中时的标题与处置建议,
          "ask_back":     需要追问时问什么,
          "advice":       给挂号员的一句话提示,
          "suggestions":  [{"code","name","scope","score","percent",
                            "keyword_score","cosine","hits":[...]}]
        }

    四个 route 的含义（这是"置信度"落到实处的地方，不只是标签）：
      direct     指向明确，可直接据此筛排班
      ask_more   多科室接近，应先追问再定
      human      信息不足，走导诊台人工分诊
      emergency  命中急症红旗，不推荐普通科室，只给急诊指引

    关于 ``extractor``（LLM 抽取层，缺省 None = 纯规则）
    --------------------------------------------------
    传入一个 ``callable(text) -> dict``（见 ``llm_extract.extract``）即可启用。
    这里刻意做成**可注入**而不是直接 import，有三个原因：
      1. 规则引擎不依赖网络，断网、没配 Key、模型挂了都能照常工作；
      2. 单元测试可以注入假实现，不产生费用也不受网络波动影响；
      3. 抽取失败时的降级逻辑集中在一处，不会散落到调用方。

    安全边界：**急症红旗永远先跑，且只看原文**。命中红旗直接返回，
    既不给科室排名，也不花一次模型调用——心梗病人不该等 1 秒网络往返。
    LLM 抽出的症状只做「并集」，无权删掉规则已命中的词，因此模型抽错
    最多让科室排序变差，不会让病人被漏掉。
    """
    raw = (text or "").strip()
    norm = normalize(raw)
    # 红旗判定用屏蔽文本；先做一次"局部部位方位词降级"，再算否定跨度，
    # 两处必须基于同一份字符串，否则跨度下标会错位。
    norm_bar = demote_local_side(normalize_barriered(raw))
    tokens = tokenize(norm)
    spans = negation_spans(norm_bar)
    positive, negated = split_by_negation(norm_bar, tokens, spans)
    subject = detect_subject(raw)

    # 第一优先：急症红旗。命中即中断，不给普通科室排名——
    # 给心梗患者一个"内科 45%"比不给结果更危险。
    flag = check_red_flags(norm_bar, spans)
    if flag:
        return {
            "query": raw,
            "normalized": norm,
            "tokens": tokens,
            "negated": negated,
            "subject": subject,
            "confidence": "high",
            "route": "emergency",
            "emergency": True,
            "emergency_title": flag["title"],
            "emergency_advice": flag["advice"],
            "ask_back": "",
            "advice": "发现危急信号，请立即急诊或拨打 120，不要按普通门诊挂号。",
            "extract": _empty_extract("急症命中，跳过 LLM 抽取"),
            "suggestions": [],
            "all": [],
        }

    # 第二优先：LLM 抽取层（可注入，缺省不启用）。
    # 只做并集：LLM 可以补上规则漏掉的症状，但不能删掉规则已命中的症状——
    # 否则模型一次幻觉就能把"胸痛"抹掉，安全性就交到模型手里了。
    extract_info = _empty_extract()
    added = []  # 模型补充、规则未命中的症状词（弱信号，打分时打折）
    if extractor is not None:
        try:
            raw_extract = extractor(raw) or {}
        except Exception as exc:  # 抽取层任何异常都不能让挂号流程中断
            raw_extract = {"ok": False, "error": "抽取层异常：%s" % (exc,)}

        if raw_extract.get("ok"):
            positive = list(positive)
            negated = list(negated)
            llm_terms = [t for t in (raw_extract.get("terms") or []) if t not in negated]
            llm_negated = list(raw_extract.get("negated") or [])
            added = [t for t in llm_terms if t not in positive]
            positive.extend(added)
            for term in llm_negated:
                if term not in negated and term not in positive:
                    negated.append(term)

            kind = raw_extract.get("subject") or ""
            if kind and kind != "self" and subject["type"] == "self":
                subject = subject_from_type(kind)

            extract_info = {
                "used": True,
                "provider": raw_extract.get("provider", ""),
                "terms": llm_terms,
                "negated": llm_negated,
                "added": added,
                "fallback": False,
                "error": "",
                "latency_ms": raw_extract.get("latency_ms", 0.0),
                "note": ("补充识别 %d 个症状" % len(added)) if added else "与规则结果一致",
            }
        else:
            extract_info = _empty_extract("抽取失败，已退回纯规则")
            extract_info.update({
                "fallback": True,
                "provider": raw_extract.get("provider", ""),
                "error": raw_extract.get("error", "未知原因"),
                "latency_ms": raw_extract.get("latency_ms", 0.0),
            })

    # 查询的 bigram 向量（辅信号）
    bigram_counts = {}
    for gram in _bigrams(norm):
        bigram_counts[gram] = bigram_counts.get(gram, 0) + 1
    query_vec = _tfidf(bigram_counts)

    results = []
    added_set = set(added)
    for dept in DEPARTMENTS:
        weights = dept["keywords"]
        # 只让未被否定的症状参与打分：这是 v2 修掉的第一类错误
        hits = sorted({t for t in positive if t in weights})
        # 模型补的词打折：规则精确命中 vs 模型语义联想，可信度不同，不能平权
        hit_weight = float(sum(
            weights[t] * (LLM_ADDED_WEIGHT if t in added_set else 1.0)
            for t in hits))
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
        if not positive and top_score <= 0.0:
            confidence = "low"
            route = "human"
            advice = "没有识别出有效症状描述，请补充具体不适部位与主要症状，或到导诊台人工分诊。"
        elif top_score >= CONF_HIGH:
            confidence = "high"
            route = "direct"
            advice = "症状指向明确，建议挂 %s。" % results[0]["name"]
        elif top_score >= CONF_MEDIUM:
            confidence = "medium"
            route = "ask_more"
            advice = "存在多个可能科室，建议挂 %s；若医生判断不属于本科室，可现场转诊。" % results[0]["name"]
        else:
            confidence = "low"
            route = "human"
            advice = "症状信息不足，以下结果仅供参考，建议到导诊台人工确认。"
    else:
        confidence = "low"
        route = "human"
        advice = "科室库为空，请先维护科室数据。"

    if negated:
        advice += "（已按否定处理：%s，未计入症状）" % "、".join(negated)
    if subject["hint"]:
        advice += subject["hint"]
    if extract_info["used"] and extract_info["added"]:
        advice += "（模型补充识别：%s）" % "、".join(extract_info["added"])

    # 没有任何肯定症状时不给科室排名：全是 0% 的列表对挂号员是噪音，
    # 这时候"说不出科室"本身就是正确答案。
    ranked = results[:top_k] if positive else []

    return {
        "query": raw,
        "normalized": norm,
        "tokens": tokens,
        "negated": negated,
        "subject": subject,
        "confidence": confidence,
        "route": route,
        "emergency": False,
        "emergency_title": "",
        "emergency_advice": "",
        "ask_back": build_ask_back(norm, positive, negated) if route in ("ask_more", "human") else "",
        "advice": advice,
        "extract": extract_info,
        "suggestions": ranked,
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
