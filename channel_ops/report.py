"""按四个受众输出当前样例的认定结果。

用法::

    python3 -m channel_ops.report [数据目录]
"""
from __future__ import annotations

import sys
from pathlib import Path

from .engine import (
    Case,
    adjustment_entries,
    allocate,
    assess_handover,
    check_transfer,
    customer_view,
    dealer_view,
    settle,
    transfer_timeline,
)


def _money(amount) -> str:
    return f"{amount:.2f}"


def main(root: str = ".") -> int:
    case = Case.load(root)
    handovers = assess_handover(case)
    allocations = allocate(case)
    ledger = settle(case, allocations)
    lines = ["# 渠道交付认定报告", ""]

    lines += [
        "## 区域负责人：分送行认定",
        "",
        "| 行 | 数量 | 最终使用地 | 状态 | 经销方 | 说明 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for line in case.order.lines.values():
        handover = handovers[line.id]
        lines.append(
            f"| {line.id} | {handover.qty} | {handover.plant} / {handover.district} "
            f"| {handover.status} | {handover.dealer or '-'} "
            f"| {'；'.join(handover.reasons)} |"
        )

    lines += ["", "## 仓库：转单出库检查", ""]
    for check in transfer_timeline(case):
        lines.append(
            f"- {check.transfer_id}：能否发={check.allowed}，处理={check.action}"
        )
        for blocker in check.blockers:
            lines.append(f"  - 阻断：{blocker}")
        for reason in check.reasons:
            lines.append(f"  - {reason}")

    lines += [
        "",
        "## 渠道财务：实际交货核认",
        "",
        "| 回执 | 行 | 箱数 | 真实使用地 | 合约服务方 | 渠道报酬 | 暂缓 | 违约补偿 | 状态 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for alloc in allocations:
        lines.append(
            f"| {alloc.shipment} | {alloc.line_id} | {alloc.qty} "
            f"| {alloc.plant} / {alloc.district} | {alloc.contract_dealer or '-'} "
            f"| {_money(alloc.fee_amount)} | {_money(alloc.held_amount)} "
            f"| {_money(alloc.breach_amount)} | {alloc.status} |"
        )
    lines += ["", "出账日记账：", ""]
    for entry in ledger:
        role = entry.dealer or "（暂缓）"
        lines.append(
            f"- {entry.kind} / {role} / {entry.region or '-'} / "
            f"{_money(entry.amount)} {entry.currency} — {entry.note}"
        )

    lines += [
        "",
        "## 经销商各自可见凭据",
        "",
    ]
    for dealer in sorted({c.dealer for c in case.book.contracts}):
        view = dealer_view(case, dealer, allocations, ledger)
        lines.append(f"### {dealer}")
        lines.append(f"- 授权合同：{', '.join(c['id'] for c in view.contracts) or '无'}")
        lines.append(f"- 交货记录：{len(view.deliveries)} 笔")
        lines.append(f"- 报酬凭据：{len(view.vouchers)} 张")
        lines.append(f"- 本人申领：{', '.join(c['id'] for c in view.claims) or '无'}")
        lines.append("")

    lines += ["## 总部客户：订单行可提货状态", ""]
    for view in customer_view(case, handovers):
        lines.append(f"- {view.line_id}（{view.qty} 箱，{view.plant} / {view.district}）：{view.message}")

    # 演示补送确认的差额挂账（不改变已发生交接的可查性）。
    updated = None
    try:
        from .engine import apply_supplemental_confirmation

        updated = apply_supplemental_confirmation(
            case, line_id="L03", plant="FX-W1", district="W13", signed_by="GP-11"
        )
    except Exception:  # pragma: no cover - 样例数据不支持时跳过演示
        updated = None
    if updated is not None:
        deltas = adjustment_entries(case, allocations, allocate(updated))
        if deltas:
            lines += ["", "## 补送最终用途确认的差额挂账", ""]
            for entry in deltas:
                lines.append(
                    f"- {entry.customer} / {entry.region} / {entry.dealer or '-'} / "
                    f"{_money(entry.amount)} {entry.currency} — {entry.note}"
                )

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]))
