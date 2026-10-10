#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
麦当劳健身餐搭配求解器。

在官方营养数据集（list-nutrition-foods 返回的 160 款 SKU）上，
用「主项 + 配餐」两段式组合逼近目标热量，并诊断蛋白缺口与钠负荷。

纯标准库实现，无第三方依赖，可离线运行。
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from itertools import combinations

DATA_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "references", "nutrition-foods.json"
)

# 训练目标 -> (热量系数, 蛋白系数, 脂肪系数)。系数作用在目标体重(kg)上。
GOAL_PRESETS = {
    "cut":  {"label": "减脂",   "kcal_per_kg": 30, "protein_per_kg": 2.0, "fat_ratio": 0.25},
    "maintain": {"label": "维持", "kcal_per_kg": 38, "protein_per_kg": 1.6, "fat_ratio": 0.28},
    "bulk":  {"label": "增肌",   "kcal_per_kg": 46, "protein_per_kg": 2.2, "fat_ratio": 0.25},
}

# 主项候选：单品热量足够撑起一餐，且蛋白密度不低。
MAIN_ITEM_MIN_KCAL = 280
MAIN_ITEM_MIN_PROTEIN = 14
# 配餐候选：主食/薯条/沙拉/低卡项，用来微调热量。
# 下限设为 45kcal，避免求解器用两杯 10kcal 黑咖啡「凑」出精确分数——那不是一顿饭。
SIDE_ITEM_MIN_KCAL = 45
SIDE_ITEM_MAX_KCAL = 400

# 钠预警阈值（mg）。健身餐常见高钠问题，超过即提示。
SODIUM_ALERT_MG = 1200

# 品类关键词分类。求解时饮品不能充当正餐主食——否则会出现「两杯奶昔当一餐」
# 这种热量数字达标但毫无饱腹意义的组合。
BEVERAGE_KEYWORDS = (
    "可乐", "雪碧", "咖啡", "奶茶", "红茶", "绿茶", "奶昔", "旋酷", "新地", "冰淇淋",
    "牛奶", "怡泉", "鲜萃", "浓缩", "朱古力热", "热朱古力", "柠檬", "橙", "麦旋",
)
DESSERT_KEYWORDS = ("派", "新地", "冰淇淋", "圣代", "曲奇", "蛋糕")


def classify(name: str) -> str:
    """按品名关键词粗分品类。用于把饮品/甜点排除出正餐候选。"""
    if any(k in name for k in DESSERT_KEYWORDS):
        return "dessert"
    if any(k in name for k in BEVERAGE_KEYWORDS):
        return "beverage"
    return "food"


@dataclass
class Food:
    name: str
    kcal: int
    protein: float
    fat: float
    carb: float
    sodium: float
    calcium: float

    @property
    def protein_density(self) -> float:
        """每 100 大卡的蛋白克数。衡量「同等热量下能吃到多少蛋白」。"""
        return (self.protein / self.kcal * 100) if self.kcal > 0 else 0.0


@dataclass
class Result:
    main: Food
    sides: list = field(default_factory=list)
    total_kcal: float = 0.0
    total_protein: float = 0.0
    total_fat: float = 0.0
    total_carb: float = 0.0
    total_sodium: float = 0.0
    kcal_gap: float = 0.0
    protein_gap: float = 0.0


def load_foods(path: str = DATA_FILE) -> list:
    """读取营养数据集。数据集字段带 _g/_mg 后缀，此处映射到内部字段名。"""
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    return [
        Food(
            name=item["name"],
            kcal=item["kcal"],
            protein=item["protein_g"],
            fat=item["fat_g"],
            carb=item["carb_g"],
            sodium=item["sodium_mg"],
            calcium=item["calcium_mg"],
        )
        for item in raw
    ]


def daily_targets(weight_kg: float, goal: str) -> dict:
    """按体重线性外推日目标。个体重差异较大，故用 kg 系数而非固定数值。"""
    preset = GOAL_PRESETS.get(goal)
    if not preset:
        raise ValueError(f"未知目标: {goal}，可选 {list(GOAL_PRESETS)}")
    kcal = preset["kcal_per_kg"] * weight_kg
    protein = preset["protein_per_kg"] * weight_kg
    fat = kcal * preset["fat_ratio"] / 9
    carb = max(0.0, (kcal - protein * 4 - fat * 9) / 4)
    return {
        "kcal": kcal, "protein": protein, "fat": fat, "carb": carb,
        "label": preset["label"],
    }


def split_meal_share(ratio: float) -> dict:
    """把日目标按餐次比例切分。默认三餐制：早 30% / 午 40% / 晚 30%。"""
    return {"kcal": ratio, "protein": ratio, "fat": ratio, "carb": ratio}


def solve(foods: list, target_kcal: float, target_protein: float,
          max_sides: int = 2, sodium_cap: float = SODIUM_ALERT_MG) -> Result:
    """
    两段式求解：
      1. 主项在满足最低门槛的 SKU 中，优先选蛋白密度高、热量贴近目标一半的
      2. 配餐从低热量候选中做 0~max_sides 的组合，用组合和补齐剩余热量

    代价函数 = 热量偏差 + 蛋白不足惩罚 + 超钠惩罚。
    单纯最小化热量偏差会退化成「堆油炸」——实测可达 1900mg 钠，
    对健身场景毫无意义，故把钠作为软约束纳入代价，避免求解器给出营养上荒谬的组合。
    """
    # 主项必须是真正的食物；饮品与甜点可作配餐调节热量，但不可充当整餐主食
    mains = [f for f in foods
             if f.kcal >= MAIN_ITEM_MIN_KCAL
             and f.protein >= MAIN_ITEM_MIN_PROTEIN
             and classify(f.name) == "food"]
    # 配餐放宽到全部低热量项，让饮品/甜点仍可用于微调热量
    sides = [f for f in foods
             if SIDE_ITEM_MIN_KCAL <= f.kcal <= SIDE_ITEM_MAX_KCAL]

    if not mains:
        raise ValueError("数据集中没有符合主项门槛的餐品")

    best = None
    for main in mains:
        remaining = target_kcal - main.kcal
        # 配餐组合：0 个、1 个、2 个（去重按名称避免同款重复计入）
        for size in range(0, max_sides + 1):
            for combo in combinations(sides, size):
                if size and len({c.name for c in combo}) != size:
                    continue
                add_kcal = sum(c.kcal for c in combo)
                # 剩余热量为负（主项已超）且仍加配餐时跳过
                if remaining < 0 and add_kcal > 0:
                    continue
                total_kcal = main.kcal + add_kcal
                total_protein = main.protein + sum(c.protein for c in combo)
                total_sodium = main.sodium + sum(c.sodium for c in combo)

                # 代价：相对热量偏差的绝对值（按目标归一化，使不同目标可比）
                cost = abs(total_kcal - target_kcal) / max(target_kcal, 1) * 100
                # 蛋白不足按缺口克数惩罚；超出目标不额外罚，避免为凑蛋白牺牲热量精度
                if total_protein < target_protein:
                    cost += (target_protein - total_protein) * 0.5
                # 超钠按超出量惩罚，权重足以在热量接近时压过「多几大卡」的诱惑
                if total_sodium > sodium_cap:
                    cost += (total_sodium - sodium_cap) / 50

                if best is None or cost < best[0] - 1e-9:
                    best = (cost, main, combo, total_sodium)

    if best is None:
        raise ValueError("未能找到可行组合，请放宽目标区间")

    _, main, combo, _ = best
    return Result(
        main=main, sides=list(combo),
        total_kcal=main.kcal + sum(c.kcal for c in combo),
        total_protein=main.protein + sum(c.protein for c in combo),
        total_fat=main.fat + sum(c.fat for c in combo),
        total_carb=main.carb + sum(c.carb for c in combo),
        total_sodium=main.sodium + sum(c.sodium for c in combo),
        kcal_gap=(main.kcal + sum(c.kcal for c in combo)) - target_kcal,
        protein_gap=(main.protein + sum(c.protein for c in combo)) - target_protein,
    )


def top_protein_foods(foods: list, limit: int = 5) -> list:
    """按蛋白密度排序，用于蛋白缺口补齐建议。"""
    return sorted([f for f in foods if f.protein > 0],
                  key=lambda f: -f.protein_density)[:limit]


def format_report(result: Result, targets: dict, foods: list) -> str:
    dev_pct = (result.kcal_gap / targets["kcal"] * 100) if targets["kcal"] else 0
    lines = []
    lines.append("=" * 58)
    lines.append(f"  麦麦健身餐搭配方案  ·  目标：{targets['label']}")
    lines.append("=" * 58)
    lines.append(f"\n【目标】热量 {targets['kcal']:.0f} kcal"
                 f" | 蛋白 {targets['protein']:.0f} g"
                 f" | 脂肪 {targets['fat']:.0f} g"
                 f" | 碳水 {targets['carb']:.0f} g")
    lines.append("\n【推荐搭配】")
    lines.append(f"  主食  {result.main.name}")
    lines.append(f"        {result.main.kcal} kcal | 蛋白 {result.main.protein}g"
                 f" | 脂肪 {result.main.fat}g | 碳水 {result.main.carb}g"
                 f" | 钠 {result.main.sodium:.0f}mg")
    for side in result.sides:
        lines.append(f"  配餐  {side.name}")
        lines.append(f"        {side.kcal} kcal | 蛋白 {side.protein}g"
                     f" | 脂肪 {side.fat}g | 碳水 {side.carb}g"
                     f" | 钠 {side.sodium:.0f}mg")

    lines.append("\n【合计】")
    lines.append(f"  热量 {result.total_kcal:.0f} kcal"
                 f"  (偏差 {result.kcal_gap:+.0f} kcal / {dev_pct:+.1f}%)")
    lines.append(f"  蛋白 {result.total_protein:.0f} g"
                 f"  ({result.protein_gap:+.0f} g vs 目标)")
    lines.append(f"  脂肪 {result.total_fat:.0f} g | 碳水 {result.total_carb:.0f} g")

    if abs(dev_pct) <= 5.0:
        lines.append(f"  ✅ 热量偏差在 ±5% 以内，达标")
    else:
        lines.append(f"  ⚠️ 热量偏差 {dev_pct:+.1f}%，"
                     f"建议微调或调整餐次分配")

    if result.protein_gap < -5:
        lines.append(f"\n【蛋白缺口】差 {abs(result.protein_gap):.0f} g。"
                     f"优先补蛋白密度高的单品：")
        for food in top_protein_foods(foods, 3):
            lines.append(f"    · {food.name.strip()} — "
                         f"{food.protein:.0f}g 蛋白 / {food.kcal} kcal"
                         f"（密度 {food.protein_density:.1f} g/100kcal）")
    else:
        lines.append(f"\n【蛋白】已达标（{result.total_protein:.0f} g）")

    if result.total_sodium > SODIUM_ALERT_MG:
        lines.append(f"\n【钠预警】合计 {result.total_sodium:.0f} mg，"
                     f"已超过 {SODIUM_ALERT_MG} mg。")
        lines.append("  建议：改选无糖可乐/纯牛奶，或去掉高钠酱料，"
                     "训练前 2 小时避免摄入过多钠。")
    else:
        lines.append(f"\n【钠】合计 {result.total_sodium:.0f} mg，在合理区间")

    lines.append("\n" + "=" * 58)
    lines.append("仅供参考，不构成医疗或营养建议。")
    lines.append("餐品信息、价格与供应状态以麦当劳官方渠道实时结果为准。")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="麦当劳健身餐搭配求解器")
    parser.add_argument("--weight", type=float, required=True, help="体重 kg")
    parser.add_argument("--goal", choices=list(GOAL_PRESETS), default="cut",
                        help="训练目标：cut 减脂 / maintain 维持 / bulk 增肌")
    parser.add_argument("--ratio", type=float, default=0.35,
                        help="本餐占日目标热量比例，默认 0.35（≈午餐）")
    parser.add_argument("--data", default=DATA_FILE, help="营养数据 JSON 路径")
    args = parser.parse_args()

    try:
        foods = load_foods(args.data)
    except FileNotFoundError:
        print(f"找不到营养数据文件: {args.data}", file=sys.stderr)
        return 1

    daily = daily_targets(args.weight, args.goal)
    share = split_meal_share(args.ratio)
    meal_kcal = daily["kcal"] * share["kcal"]
    meal_protein = daily["protein"] * share["protein"]
    meal_targets = {
        "kcal": meal_kcal, "protein": meal_protein,
        "fat": daily["fat"] * share["fat"],
        "carb": daily["carb"] * share["carb"],
        "label": f"{daily['label']} · 本餐 {args.ratio:.0%}",
    }

    result = solve(foods, meal_kcal, meal_protein)
    print(format_report(result, meal_targets, foods))
    return 0


if __name__ == "__main__":
    sys.exit(main())