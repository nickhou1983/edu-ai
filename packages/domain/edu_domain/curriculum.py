from dataclasses import dataclass
from typing import Literal

from edu_domain.contracts import SkillId

Unit = Literal["有理数", "整式", "一元一次方程"]


@dataclass(frozen=True)
class Skill:
    id: SkillId
    unit: Unit
    title: str
    hint: str


SKILLS = (
    Skill("rational-sum", "有理数", "有理数加减", "把减去一个数写成加上它的相反数，再确定结果的符号。"),
    Skill(
        "rational-product",
        "有理数",
        "有理数乘除",
        "先判断正负号，再算绝对值；除以一个非零数可写成乘它的倒数。",
    ),
    Skill("rational-mixed", "有理数", "有理数混合运算", "先算括号，再算乘方、乘除，最后算加减。"),
    Skill("like-terms", "整式", "合并同类项", "把未知数次数相同的项放到一起，只对它们的系数做加减。"),
    Skill("distribution", "整式", "去括号与化简", "括号外的因数要乘括号内每一项，特别注意负号。"),
    Skill(
        "linear-equations",
        "一元一次方程",
        "一元一次方程求解",
        "先合并同类项，再在等式两边做相同的运算，让未知数单独留在一边。",
    ),
)
SKILL_BY_ID = {skill.id: skill for skill in SKILLS}
