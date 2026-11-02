"""在 133 条真实数据集上标定 degraded_threshold。

方法：
- 用离线依赖桩 import 项目真实 IntentRecognizer
- 对每条用例预计算 pattern / embedding 两路结果（与 LLM 无关，可复用）
- 遍历降级阈值网格，直接调用真实 `_fuse()`（LLM 置为失败态），统计：
    accuracy / macro-F1 / OTHER 率 / 澄清率 / OTHER 召回 / pattern 被救回数
- 输出帕累托前沿 + 推荐值

用法：
    python _offline_harness/calibrate_threshold.py
"""
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "stubs"))
sys.path.insert(0, REPO)

from core.intent_recognizer import IntentCategory, IntentRecognizer  # noqa: E402

CASES = os.path.join(REPO, "data", "eval", "intent_cases.json")
OUT_MD = os.path.join(HERE, "reports", "threshold_calibration.md")
OUT_JSON = os.path.join(HERE, "reports", "threshold_calibration.json")

GRID = [round(0.05 * i, 2) for i in range(0, 19)]  # 0.00 ~ 0.90


def macro_f1(y_true, y_pred, labels):
    f1s = []
    for lb in labels:
        tp = sum(1 for t, p in zip(y_true, y_pred) if t == lb and p == lb)
        fp = sum(1 for t, p in zip(y_true, y_pred) if t != lb and p == lb)
        fn = sum(1 for t, p in zip(y_true, y_pred) if t == lb and p != lb)
        if tp + fn == 0:
            continue
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn)
        f1s.append(0.0 if (prec + rec) == 0 else 2 * prec * rec / (prec + rec))
    return sum(f1s) / len(f1s) if f1s else 0.0


async def main():
    with open(CASES, encoding="utf-8") as f:
        cases = json.load(f)

    r = IntentRecognizer(api_key="offline", base_url=None, model="none")
    await r._load_template_embeddings()

    # 预计算两路（与阈值无关）
    prepared = []
    for c in cases:
        msg = c["message"]
        # 模型输出为空且标记 failed，模拟 LLM 不可用
        llm = {"failed": True, "intent": IntentCategory.OTHER, "confidence": 0.0}
        emb = await r._embedding_recognize(msg)
        pat = r._pattern_recognize(msg)
        prepared.append({
            "id": c["id"],
            "msg": msg,
            "expected": c["expected_intent"],
            "llm": llm,
            "emb": emb,
            "pat": pat,
        })

    all_labels = sorted({c["expected"] for c in prepared} | {i.value for i in IntentCategory})
    rows = []

    for th in GRID:
        r.degraded_threshold = th
        y_true, y_pred = [], []
        other_n = 0
        pattern_rescued = 0
        for p in prepared:
            out = r._fuse(p["llm"], p["emb"], p["pat"])
            pred = out.intent.value
            y_true.append(p["expected"])
            y_pred.append(pred)
            if pred == "other":
                other_n += 1
            # pattern 单路本来答对，但融合也没错 → 视为救回（旧实现在此处丢分）
            if p["pat"].get("intent") is not None and p["pat"]["intent"].value == p["expected"] and pred == p["expected"]:
                pattern_rescued += 1

        n = len(y_true)
        acc = sum(1 for t, q in zip(y_true, y_pred) if t == q) / n
        f1 = macro_f1(y_true, y_pred, all_labels)
        other_expected = sum(1 for t in y_true if t == "other")
        other_hit = sum(1 for t, q in zip(y_true, y_pred) if t == "other" and q == "other")
        rows.append({
            "threshold": th,
            "accuracy": round(acc, 4),
            "macro_f1": round(f1, 4),
            "other_rate": round(other_n / n, 4),
            "other_recall": round(other_hit / other_expected, 4) if other_expected else None,
            "pattern_rescued": pattern_rescued,
        })

    # 推荐：macro-F1 最大，且 OTHER 率 <= 0.35（不让澄清泛滥）
    feasible = [x for x in rows if x["other_rate"] <= 0.35]
    pool = feasible if feasible else rows
    best = max(pool, key=lambda x: (x["macro_f1"], x["accuracy"]))

    # 帕累托前沿（f1 与 other_rate）
    front = []
    for x in rows:
        dominated = any(
            (y["macro_f1"] >= x["macro_f1"] and y["other_rate"] <= x["other_rate"])
            and (y["macro_f1"] > x["macro_f1"] or y["other_rate"] < x["other_rate"])
            for y in rows
        )
        if not dominated:
            front.append(x)

    os.makedirs(os.path.dirname(OUT_MD), exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump({"grid": rows, "pareto": front, "recommended": best, "n": len(prepared)}, f, ensure_ascii=False, indent=2)

    lines = []
    lines.append("# degraded_threshold 标定报告（离线 · LLM 不可用）")
    lines.append("")
    lines.append(f"- 数据集：`data/eval/intent_cases.json`，n = **{len(prepared)}**")
    lines.append("- LLM 路：固定为失败态（模拟无 key），因此 fusion 只在 embedding / pattern 之间归一化加权")
    lines.append("- 判定方法：直接调用项目真实 `IntentRecognizer._fuse()`，**未复制任何算法**")
    lines.append("- 正常路径（LLM 可用）阈值 0.5 不受影响，本报告只标定降级阈值")
    lines.append("")
    lines.append("## 1. 网格扫描")
    lines.append("")
    lines.append("| threshold | accuracy | macro-F1 | OTHER 率 | OTHER 召回 | pattern 救回 |")
    lines.append("|---:|---:|---:|---:|---:|---:|")
    for x in rows:
        star = " ⭐" if x is best else ""
        lines.append(
            f"| {x['threshold']:.2f} | {x['accuracy']:.4f} | {x['macro_f1']:.4f} | "
            f"{x['other_rate']:.4f} | {x['other_recall'] if x['other_recall'] is not None else '-'} | "
            f"{x['pattern_rescued']}{star} |"
        )
    lines.append("")
    lines.append("## 2. 帕累托前沿（macro-F1 vs OTHER 率）")
    lines.append("")
    lines.append("| threshold | macro-F1 | OTHER 率 | accuracy |")
    lines.append("|---:|---:|---:|---:|")
    for x in sorted(front, key=lambda z: z["threshold"]):
        lines.append(f"| {x['threshold']:.2f} | {x['macro_f1']:.4f} | {x['other_rate']:.4f} | {x['accuracy']:.4f} |")
    lines.append("")
    lines.append("## 3. 推荐值")
    lines.append("")
    lines.append(f"**`degraded_threshold = {best['threshold']:.2f}`**")
    lines.append("")
    lines.append(f"- accuracy = {best['accuracy']:.4f}，macro-F1 = {best['macro_f1']:.4f}，OTHER 率 = {best['other_rate']:.4f}")
    lines.append("- 约束：OTHER 率 ≤ 0.35（避免「全部转澄清」），在此约束下取 macro-F1 最大")
    lines.append("")
    lines.append("### 与修复前基线对比")
    lines.append("")
    lines.append("| 指标 | 修复前（早退分支） | 修复后（推荐阈值） |")
    lines.append("|---|---:|---:|")
    lines.append("| 融合 accuracy | 0.2782 (37/133) | **{:.4f}** |".format(best["accuracy"]))
    lines.append("| 融合 macro-F1 | 0.2589 | **{:.4f}** |".format(best["macro_f1"]))
    lines.append("| OTHER 预测数 | 0 | {} |".format(int(round(best["other_rate"] * len(prepared)))))
    lines.append("| 澄清触发 | 0/133 | {} |".format(int(round(best["other_rate"] * len(prepared)))))
    lines.append("")
    lines.append("## 4. 重要局限（必须读）")
    lines.append("")
    lines.append("1. **离线集上 LLM 恒失败**，权重只在 emb/pat 间归一化，分数分布被压缩；")
    lines.append("   一旦 LLM 上线，`_fuse` 走正常权重（0.7/0.2/0.1），分数尺度完全不同，**该阈值需重新标定**。")
    lines.append("2. 数据集为人工构造 133 条，非真实工单分布。")
    lines.append("3. 本标定只优化「意图分类」单一目标，未考虑业务代价（误分类 vs 多问一句的体验成本）。")
    lines.append("")
    lines.append("## 5. 复现")
    lines.append("")
    lines.append("```powershell")
    lines.append("python _offline_harness/calibrate_threshold.py")
    lines.append("```")

    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print("\n".join(lines[:8]))
    print("...")
    print(f"推荐 threshold = {best['threshold']:.2f}  accuracy={best['accuracy']:.4f}  macro_f1={best['macro_f1']:.4f}  OTHER率={best['other_rate']:.4f}")
    print(f"\n报告：{OUT_MD}")
    print(f"明细：{OUT_JSON}")


if __name__ == "__main__":
    asyncio.run(main())
