"""Simplified-Chinese safety lexicon (T28).

The same contract as `en.py`: the patterns a guard uses to refuse model output that would
decide cover, promise or refuse payment, state a breach, or advise a purchase, surrender,
cancellation or switch. They are matched without word boundaries, because Chinese is not
written with spaces and `\\b` does not fall between two Han characters.
"""
from __future__ import annotations

import re

# Verdicts on cover, payment or breach. Each alternative is a formulation that decides an
# outcome; neutral descriptions ("the clause sets out the documents needed") do not match.
OUTCOME_PATTERN = re.compile(
    r"(?:"
    r"(?:会|将|一定|肯定|必然|保证|确定)(?:会|能|可以|被)?(?:获得|得到)?"
    r"(?:赔付|赔偿|理赔|报销|获赔|赔钱|给付|赔)"
    r"|(?:不会|不能|无法|不可能|不予|拒绝|不被)(?:被)?"
    r"(?:赔付|赔偿|理赔|报销|获赔|承保|给付|赔)"
    r"|拒赔|不赔|不保"
    r"|(?:可以|能够|能)(?:获得|得到)?(?:全额)?(?:赔付|理赔|报销|获赔)"
    r"|(?:属于|不属于|在|不在)(?:本合同的?|本保险的?|保单的?|你的|您的)?"
    r"(?:保险责任|保障范围|承保范围|保险范围|理赔范围)"
    r"|(?:您|你)(?:已经|已)?(?:有|没有|无|不在)(?:保障|保险保障)"
    r"|(?:违反|违背)了?(?:保险)?(?:合同|条款|约定|义务|告知义务)"
    r"|(?:合同|保单|保险)(?:已经|已)?(?:无效|失效|作废)"
    r"|(?:理赔|索赔)(?:会|将|一定|肯定)?(?:失败|被拒)"
    r")")

# Product advice: buy, surrender, cancel, switch, recommend. "建议" alone is not advice
# ("建议您提供出院小结" asks for a document); "建议" with a purchase verb is.
ADVICE_PATTERN = re.compile(
    r"(?:退保|转保|换保|改保|加保|退掉|买保险|购买|推荐"
    r"|买(?:一份|份|这款|那款|这份|那份)"
    r"|取消(?:保单|保险|合同|投保)"
    r"|(?:换|更换|换成)(?:一家|别家|其他)(?:保险公司|保险|产品)"
    r"|建议(?:您|你)?(?:购买|投保|选择|退保|更换|换|买))")

NOTE_BANNED_PHRASES: tuple[str, ...] = (
    "保证赔付", "一定赔", "肯定赔", "不会赔", "拒赔", "不在保障范围", "违约", "违反合同",
)
