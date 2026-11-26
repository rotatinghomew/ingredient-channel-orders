"""渠道授权与交货认定的业务规则。

本模块把 ``交换记录.md`` 中的约定落成可核查的判定：

* 销售看开票地址、仓库看收货地址，两者都不是认定依据；
  认定依据是客户有权代表确认的**最终使用工厂与行政区**。
* 中转点（hub）只用于运输，不产生经销权。
* ``unknown`` 回执不等于未提，不能据此向接方另发同货指令。
* 订货确认 / 商业转单同意都不等于出库授权。
* 行政区调整后的既有用途按合同过渡期处理，新增用途重新审。
* 同一笔交货只有一份有效渠道报酬；争议金额暂缓，不受影响的行照常出账；
  违约补偿单独按最终核认的区域、受损合同与争议结论办理。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from .model import (
    LOCAL_TZ,
    Allocation,
    Claim,
    Confirmation,
    Contract,
    DirectException,
    Handover,
    LedgerEntry,
    Line,
    Order,
    RuleError,
    Shipment,
    TerritoryBook,
    Transfer,
    TransferCheck,
    shipments_from_payload,
    claims_from_payload,
    money,
    transfer_from_payload,
)

# 客户可见的分送行状态的归类。
CONTINUABLE = frozenset({"authorised", "direct_exception", "transferred"})
NEEDS_CONSENT = frozenset({"consent_required"})
NEEDS_VERIFICATION = frozenset(
    {"evidence_required", "review_required", "no_authority"}
)

CUSTOMER_MESSAGE = {
    "authorised": "可按现报最终用途继续提货",
    "direct_exception": "属已报备直供例外，可按现报最终用途继续提货",
    "transferred": "已获客户与接方同意，移交后可提货（实际放行仍以仓库检查为准）",
    "consent_required": "现报受单渠道在该用途上无经销权，需客户与有权接方另行同意",
    "evidence_required": "最终用途更正缺少客户有权代表确认，需补充核实材料",
    "review_required": "用途或数量属于新增，需按现报最终用途重新审核",
    "no_authority": "该最终使用地当前没有可对应的经销授权，需核实",
}


# ---------------------------------------------------------------------------
# 载入
# ---------------------------------------------------------------------------


def _read(root: Path, name: str) -> Any:
    return json.loads((root / "inputs" / f"{name}.json").read_text(encoding="utf-8"))


@dataclass
class Case:
    book: TerritoryBook
    order: Order
    shipments: list[Shipment]
    transfer: Transfer | None
    claims: list[Claim]
    as_of: date

    @classmethod
    def load(cls, root: str | Path, as_of: date | None = None) -> "Case":
        base = Path(root)
        book = TerritoryBook.from_payload(_read(base, "territories"))
        order = Order.from_payload(_read(base, "purchase"))
        shipments = shipments_from_payload(_read(base, "shipping"))
        requests = _read(base, "requests")
        transfer = transfer_from_payload(requests.get("transfer"))
        claims = claims_from_payload(requests.get("claims", []))
        if as_of is None:
            if order.confirmation:
                as_of = order.confirmation.at.date()
            elif shipments:
                as_of = shipments[0].day
            else:
                as_of = date.today()
        return cls(book, order, shipments, transfer, claims, as_of)


# ---------------------------------------------------------------------------
# 时间、地区与既有用途
# ---------------------------------------------------------------------------


def _is_existing_use(
    book: TerritoryBook,
    order: Order,
    line: Line,
    district: str,
    plant: str,
    day: date,
) -> bool:
    """交货日的用途是否属于“原来签认、未变”的既有用途（可享过渡期）。"""
    move = book.move
    if move is None or move.district != district:
        return False
    if not (move.effective <= day <= move.grace_until):
        return False
    # 与报单时签认的最终用途一致；分送地或数量后来加过的，重新审。
    if district != line.reported_district or plant != line.reported_plant:
        return False
    if line.reported_qty != line.qty:
        return False
    # 区划调整生效后补送的确认只要改了用途/数量，就视为新增。
    confirmation = order.confirmation
    if (
        confirmation is not None
        and confirmation.line == line.id
        and confirmation.at.date() >= move.effective
    ):
        if (
            confirmation.plant != line.reported_plant
            or confirmation.district != line.reported_district
            or (confirmation.qty is not None and confirmation.qty != line.reported_qty)
        ):
            return False
    return True


@dataclass(frozen=True)
class Entitlement:
    region: str | None  # 实际适用的地区（过渡期内为调整前地区）
    contract: Contract | None
    grace_applied: bool


def entitlement_on(
    book: TerritoryBook,
    order: Order,
    line: Line,
    day: date,
    *,
    plant: str | None = None,
    district: str | None = None,
) -> Entitlement:
    """返回某分送行在指定日期、指定最终用途上的经销权归属。"""
    district = district or line.district
    plant = plant or line.plant
    if _is_existing_use(book, order, line, district, plant, day):
        old_region = book.region_before_move(district)
        contract = book.contract_for(old_region, order.sku, day)
        if contract is not None:
            return Entitlement(old_region, contract, True)
    region = book.region_of(district, day)
    return Entitlement(region, book.contract_for(region, order.sku, day), False)


def _direct_exception_on(
    book: TerritoryBook, order: Order, plant: str, day: date
) -> DirectException | None:
    exception = book.direct_exception
    if exception and exception.covers(order.sku, plant, order.buyer, day):
        return exception
    return None


# ---------------------------------------------------------------------------
# 分送行认定（客户报单视角）
# ---------------------------------------------------------------------------


def _handover(
    line: Line, status: str, reasons: Sequence[str], contract: Contract | None = None
) -> Handover:
    return Handover(
        line_id=line.id,
        qty=line.qty,
        district=line.district,
        plant=line.plant,
        status=status,
        contract_id=contract.id if contract else None,
        dealer=contract.dealer if contract else None,
        reasons=tuple(reasons),
    )


def assess_handover(case: Case) -> dict[str, Handover]:
    """逐行判断客户能否按现报最终用途继续安排提货。"""
    book, order = case.book, case.order
    result: dict[str, Handover] = {}
    for line in order.lines.values():
        # 更正是否有客户有权代表签字。
        if line.use_status != "confirmed":
            result[line.id] = _handover(
                line, "evidence_required", [line.use_reason or "最终用途待核实"]
            )
            continue

        # 每行能否独立交货须由客户确认。
        if not line.within_order_independent:
            result[line.id] = _handover(
                line, "review_required", ["该分送行未确认可独立交货"]
            )
            continue

        day = case.as_of
        confirmation = order.confirmation
        if confirmation is not None and confirmation.line == line.id:
            day = confirmation.at.date()

        # 已报备直供客户例外优先于经销授权。
        exception = _direct_exception_on(book, order, line.plant, day)
        if exception is not None:
            result[line.id] = _handover(
                line,
                "direct_exception",
                [
                    f"最终使用工厂 {line.plant} 属客户 {order.buyer} 已报备直供例外，"
                    f"有效期至 {exception.until.isoformat()}"
                ],
            )
            continue

        ent = entitlement_on(book, order, line, day)
        contract = ent.contract
        if contract is None:
            result[line.id] = _handover(
                line,
                "no_authority",
                [f"最终使用地 {line.district} 于 {day.isoformat()} 无对应 {order.sku} 经销合同"],
            )
            continue

        reasons = [
            f"最终使用工厂 {line.plant} / 行政区 {line.district} 归属"
            f"{'过渡期内的' if ent.grace_applied else ''}{contract.region} 区，"
            f"由 {contract.id} / {contract.dealer} 独家经销"
        ]
        if ent.grace_applied:
            reasons.append(
                f"既有用途未变，按合同过渡期至 {book.move.grace_until.isoformat()} 处理"
            )

        if line.sales == contract.dealer:
            result[line.id] = _handover(line, "authorised", reasons, contract)
            continue

        transfer = case.transfer
        matched = (
            transfer is not None
            and transfer.line == line.id
            and transfer.from_dealer == line.sales
            and transfer.to_dealer == contract.dealer
            and transfer.buyer_agrees
            and transfer.acceptor_agrees
            and transfer.qty == line.qty
        )
        if matched:
            reasons.append(
                f"受单渠道 {line.sales} 在 {contract.region} 区无经销权，"
                f"已按 {transfer.id} 全量转给 {contract.dealer}，客户与接方均已同意"
            )
            result[line.id] = _handover(line, "transferred", reasons, contract)
        else:
            if transfer is not None and transfer.line == line.id:
                if transfer.to_dealer != contract.dealer:
                    reasons.append(
                        f"转单接方 {transfer.to_dealer} 并非 {contract.region} 区的有权经销商"
                        f"（应为 {contract.dealer}）"
                    )
                elif transfer.qty < line.qty:
                    reasons.append(
                        f"{transfer.id} 仅同意转交 {transfer.qty}/{line.qty} 箱，"
                        f"其余 {line.qty - transfer.qty} 箱仍须另行确认"
                    )
                elif not (transfer.buyer_agrees and transfer.acceptor_agrees):
                    reasons.append(f"{transfer.id} 尚未取得客户与接方双方同意")
            reasons.append(
                f"受单渠道 {line.sales} 在 {contract.region} 区无经销权，"
                f"须转给 {contract.dealer} 并取得客户与接方同意"
            )
            result[line.id] = _handover(line, "consent_required", reasons, contract)
    return result


# ---------------------------------------------------------------------------
# 转单与出库检查（仓库视角）
# ---------------------------------------------------------------------------


def _latest_instruction_state(
    case: Case, line_id: str, as_of: datetime | None
) -> tuple[str, Shipment | None]:
    """返回该行最后一条出库指令及其在检查时点的最新回执。"""
    instructions: dict[str, list[Shipment]] = {}
    for shipment in case.shipments:
        if shipment.line != line_id:
            continue
        if as_of is not None and shipment.at > as_of:
            continue
        instructions.setdefault(shipment.instruction, []).append(shipment)
    if not instructions:
        return "", None
    # 以最早回执最晚的那条指令为当前有效指令。
    instruction = max(instructions, key=lambda key: min(s.at for s in instructions[key]))
    events = sorted(instructions[instruction], key=lambda s: (s.at, s.event))
    return instruction, events[-1]


def check_transfer(case: Case, as_of: datetime | None = None) -> TransferCheck | None:
    """仓班在移交时检查该行能否发，以及原出库指令如何处理。"""
    transfer = case.transfer
    if transfer is None:
        return None
    order, book = case.order, case.book
    line = order.lines.get(transfer.line)
    if line is None:
        return TransferCheck(
            transfer.id, transfer.line, False, "blocked", (), ("转单指向不存在的分送行",), None
        )

    blockers: list[str] = []
    reasons: list[str] = []

    # 已确认订单的转交须客户与接方同意。
    consents = []
    if transfer.buyer_agrees:
        consents.append("客户同意")
    else:
        blockers.append("缺少客户同意")
    if transfer.acceptor_agrees:
        consents.append("接方同意")
    else:
        blockers.append("缺少接方同意")
    if not line.within_order_independent:
        blockers.append("该分送行未确认可独立交货")
    if transfer.qty > line.qty:
        blockers.append(f"转单数量 {transfer.qty} 超过分送数量 {line.qty}")

    # 接方须在最终用途上有权，转出方须确实无权。
    day = as_of.date() if as_of is not None else case.as_of
    ent = entitlement_on(book, order, line, day)
    authorised_dealer = ent.contract.dealer if ent.contract else None
    if ent.contract is None:
        blockers.append("该最终用途当前无经销授权，接方无从接单")
    else:
        if ent.contract.dealer != transfer.to_dealer:
            blockers.append(
                f"接方 {transfer.to_dealer} 在 {ent.contract.region} 区无该产品经销权"
            )
        if ent.contract.dealer == transfer.from_dealer:
            blockers.append(
                f"转出方 {transfer.from_dealer} 在该用途上仍有经销权，不属于可转单情形"
            )
    if transfer.qty < line.qty:
        reasons.append(
            f"本次仅移交 {transfer.qty}/{line.qty} 箱，剩余行仍按原受单渠道单独审核"
        )

    # 原出库指令的回执状态决定能否向接方另发。
    instruction, latest = _latest_instruction_state(case, transfer.line, as_of)
    if latest is None:
        action = "reissue"
        reasons.append("该行尚无已发出的出库指令，可按转单直接向接方开单")
    elif latest.status == "unknown":
        blockers.append(
            f"原出库指令 {instruction} 回执 unknown，"
            f"不得向接方另发可提走相同货物的指令，须先查明"
        )
        action = "blocked"
    elif latest.status == "not_picked":
        reasons.append(
            f"原出库指令 {instruction} 已于 {latest.at.isoformat()} 确认未提货，"
            f"可停止原指令后再向接方发货"
        )
        action = "stop_original_then_reissue"
    else:
        blockers.append(
            f"原出库指令 {instruction} 已处于 {latest.status}，不能再向接方另发"
        )
        action = "blocked"

    if blockers:
        action = "blocked"
    reasons.append("商业转单确认不等于出库授权，实际能否发以本次仓库检查为准")
    return TransferCheck(
        transfer_id=transfer.id,
        line_id=transfer.line,
        allowed=not blockers,
        action=action,
        consents=tuple(consents),
        blockers=tuple(blockers),
        authorised_dealer=authorised_dealer,
        reasons=tuple(reasons),
    )


def transfer_timeline(case: Case) -> list[TransferCheck]:
    """按回执到达顺序回放仓库检查，显示回执不明与确认未提的区别。"""
    if case.transfer is None:
        return []
    moments = sorted({s.at for s in case.shipments if s.line == case.transfer.line})
    return [
        check
        for moment in moments
        if (check := check_transfer(case, moment)) is not None
    ]


# ---------------------------------------------------------------------------
# 实际交货的渠道核认
# ---------------------------------------------------------------------------


def _claims_for(case: Case, shipment: Shipment) -> list[Claim]:
    return [
        claim
        for claim in case.claims
        if claim.shipment == shipment.event and claim.line == shipment.line
    ]


def _allocation(
    shipment: Shipment,
    line: Line,
    *,
    qty: int,
    district: str,
    plant: str,
    region: str | None,
    status: str,
    contract: Contract | None,
    paying_dealer: str | None,
    fee: Decimal,
    breach: Decimal,
    held: Decimal,
    currency: str,
    reasons: Sequence[str],
    grace_applied: bool = False,
    delivery_plant: str | None = None,
    delivery_region: str | None = None,
    transit: bool = False,
    location_known: bool = True,
    customer_error: bool = False,
) -> Allocation:
    return Allocation(
        shipment=shipment.event,
        line_id=line.id,
        qty=qty,
        district=district,
        region=region,
        plant=plant,
        status=status,
        contract_id=contract.id if contract else None,
        contract_dealer=contract.dealer if contract else None,
        paying_dealer=paying_dealer,
        fee_amount=fee,
        breach_amount=breach,
        held_amount=held,
        currency=currency,
        reasons=tuple(reasons),
        grace_applied=grace_applied,
        delivery_plant=delivery_plant,
        delivery_region=delivery_region,
        transit=transit,
        location_known=location_known,
        customer_error=customer_error,
    )


def _plant_districts(case: Case) -> dict[str, str]:
    """从报单、更正与补送确认中汇总已知的工厂 → 行政区。"""
    mapping: dict[str, str] = {}
    for line in case.order.lines.values():
        mapping.setdefault(line.reported_plant, line.reported_district)
        mapping.setdefault(line.plant, line.district)
    for confirmation in case.order.confirmations_for_any():
        mapping.setdefault(confirmation.plant, confirmation.district)
    return mapping


def _delivery_purpose(
    case: Case, shipment: Shipment
) -> tuple[str, str | None, bool, bool]:
    """从实际交货回执重建真实使用地。

    返回 ``(use_plant, use_district, transit, known)``：

    * 该行有客户有权代表签认的最终用途确认时，以确认的工厂与行政区为准；
      这是唯一能推翻仓库交货点的凭据，补送确认即按此产生差额。
    * 收货点只是客户中转点（``hub``）时，以该行报单的最终使用地为准，
      ``transit`` 为真。
    * 否则收货工厂就是真实使用工厂，行政区取已核实的工厂归属。
    * 交货地点未报告或工厂归属不明时 ``known`` 为假，不核认报酬。
    """
    order = case.order
    line = order.lines[shipment.line]
    # 客户有权代表签认的最终用途更正优先于仓库交货点；是否改算仍按交货当日的
    # 有效合同判定，经销资格变化不会把已交付部分改给别家。
    confirmation = order.signed_confirmation_for(line.id)
    if confirmation is not None:
        return (
            confirmation.plant,
            confirmation.district,
            shipment.plant == order.hub,
            True,
        )
    plant = shipment.plant
    if plant is None:
        return line.plant, line.district, False, False
    if plant == order.hub:
        return line.plant, line.district, True, line.use_status == "confirmed"
    district = _plant_districts(case).get(plant)
    return plant, district, False, district is not None


def allocate(case: Case) -> list[Allocation]:
    """逐笔已交货回执核认实际交货份额。

    认定以**真实使用地**与交货当时批准的渠道为准；收货点只是中转点时，
    以获认可的最终用途凭证判断适用经销权。
    """
    book, order = case.book, case.order
    zero = money("0")
    allocations: list[Allocation] = []
    for shipment in case.shipments:
        if not shipment.delivered:
            continue
        line = order.lines.get(shipment.line)
        if line is None:
            raise RuleError(f"回执 {shipment.event} 指向不存在的分送行 {shipment.line}")
        qty = shipment.qty if shipment.qty is not None else line.qty
        day = shipment.day
        reasons: list[str] = []
        plant, district, transit, known = _delivery_purpose(case, shipment)

        if not known:
            allocations.append(
                _allocation(
                    shipment,
                    line,
                    qty=qty,
                    district=line.district,
                    plant=line.plant,
                    region=book.region_of(line.district, day),
                    status="misreport",
                    contract=None,
                    paying_dealer=None,
                    fee=zero,
                    breach=zero,
                    held=zero,
                    currency=book.settlement.currency,
                    reasons=[
                        "回执未报告可核实的真实使用地，无法认定适用经销权，报酬暂不核认"
                    ],
                    delivery_plant=shipment.plant,
                    location_known=False,
                )
            )
            continue
        if district is None:
            allocations.append(
                _allocation(
                    shipment,
                    line,
                    qty=qty,
                    district=line.district,
                    plant=plant,
                    region=None,
                    status="misreport",
                    contract=None,
                    paying_dealer=None,
                    fee=zero,
                    breach=zero,
                    held=zero,
                    currency=book.settlement.currency,
                    reasons=[f"真实使用工厂 {plant} 的行政区未核实，报酬暂不核认"],
                    delivery_plant=shipment.plant,
                    delivery_region=None,
                    transit=transit,
                    location_known=False,
                )
            )
            continue
        if transit:
            reasons.append(
                f"收货点 {shipment.plant} 是客户中转点，按已认可的最终使用工厂 "
                f"{plant} / {district} 判断适用经销权"
            )
        delivery_region = (
            district if transit else book.region_of(district, day)
        )

        # 直供例外：不产生渠道报酬。
        exception = _direct_exception_on(book, order, plant, day)
        if exception is not None:
            allocations.append(
                _allocation(
                    shipment,
                    line,
                    qty=qty,
                    district=district,
                    plant=plant,
                    region=book.region_of(district, day),
                    status="direct",
                    contract=None,
                    paying_dealer=None,
                    fee=zero,
                    breach=zero,
                    held=zero,
                    currency=book.settlement.currency,
                    reasons=[
                        f"最终使用工厂 {plant} 属直供例外（至 {exception.until.isoformat()}），"
                        f"不核认渠道报酬"
                    ],
                    delivery_plant=shipment.plant,
                    delivery_region=delivery_region,
                    transit=transit,
                )
            )
            continue

        ent = entitlement_on(book, order, line, day, plant=plant, district=district)
        contract = ent.contract
        if ent.grace_applied:
            reasons.append(
                f"既有用途未变，{district} 于 {day.isoformat()} 仍在过渡期内，"
                f"按原 {ent.region} 区合同 {contract.id if contract else '-'} 核认"
            )
        if contract is None:
            allocations.append(
                _allocation(
                    shipment,
                    line,
                    qty=qty,
                    district=district,
                    plant=plant,
                    region=ent.region,
                    status="misreport",
                    contract=None,
                    paying_dealer=None,
                    fee=zero,
                    breach=zero,
                    held=zero,
                    currency=book.settlement.currency,
                    reasons=reasons
                    + [
                        f"{day.isoformat()} 在 {district} 无对应 {order.sku} 排他合同，"
                        f"不核认渠道报酬，须按真实使用地补正"
                    ],
                    grace_applied=ent.grace_applied,
                    delivery_plant=shipment.plant,
                    delivery_region=delivery_region,
                    transit=transit,
                )
            )
            continue

        rate = contract.fee_per_case
        total_fee = money(qty * rate)

        # 实送方与有权经销商不一致：区分跨区违约、合法中转与客户报错。
        breach = zero
        customer_error = False
        actor_is_entitled = shipment.actor == contract.dealer
        if not actor_is_entitled:
            actor_contracts = [
                c
                for c in book.contracts
                if c.dealer == shipment.actor and c.start <= day <= c.end
            ]
            actor_region = actor_contracts[0].region if actor_contracts else None
            if actor_region is None:
                customer_error = True
                reasons.append(
                    f"实送方 {shipment.actor} 不是有记录的经销商，按客户报错处理，"
                    f"不认定跨区违约"
                )
            elif actor_region != contract.region:
                breach = money(qty * book.settlement.breach_fee_per_case)
                reasons.append(
                    f"{shipment.actor}（{actor_region} 区）跨区向 {contract.region} 区交货，"
                    f"按 {book.settlement.breach_fee_per_case} 元/箱计违约补偿，"
                    f"受损合同 {contract.id}（{contract.dealer}）"
                )
            elif transit:
                reasons.append(
                    f"{shipment.actor} 属 {contract.region} 区，收货点只是中转，"
                    f"按合法中转处理，不认定违约"
                )
            else:
                reasons.append(
                    f"实送方 {shipment.actor} 与合约服务方不同，按客户报错处理，"
                    f"不认定跨区违约"
                )

        # 同一笔交货只能产生一份有效渠道报酬；多个记录的认领则暂缓争议部分。
        shipment_claims = _claims_for(case, shipment)
        claimants = sorted({claim.by for claim in shipment_claims})
        held = zero
        payable_qty = qty
        if len(claimants) > 1:
            # 争议金额按各方申报数量的重合部分暂缓；无重合则不扣住整笔。
            required: dict[str, int] = {}
            for claim in shipment_claims:
                required[claim.by] = required.get(claim.by, 0) + claim.qty
            first, second = (required[name] for name in claimants[:2])
            overlap = max(first + second - qty, 0)
            overlap = min(overlap, qty)
            payable_qty = qty - overlap
            held = money(overlap * rate)
            reasons.append(
                f"同一笔交货 {shipment.event} 有 {len(claimants)} 份记录的认领"
                f"（{'、'.join(claimants)}），只核认一份渠道报酬；"
                f"{overlap} 箱对应 {held:.2f} 元争议暂缓，待结论"
            )

        if not actor_is_entitled and not customer_error:
            reasons.append(
                f"报酬仍核认给合约服务方 {contract.dealer}，实送方不另得渠道报酬"
            )

        paid_fee = money(payable_qty * rate)
        if held > 0:
            status = "payment_hold"
        elif breach > 0:
            status = "violation"
        else:
            status = "allocated"

        reasons.append(
            f"真实使用地 {plant} / {district} → {contract.region} 区，"
            f"合约服务方 {contract.dealer}，{rate:.2f} 元/箱"
        )
        allocations.append(
            _allocation(
                shipment,
                line,
                qty=qty,
                district=district,
                plant=plant,
                region=contract.region,
                status=status,
                contract=contract,
                paying_dealer=None if held >= total_fee else contract.dealer,
                fee=paid_fee,
                breach=breach,
                held=held,
                currency=book.settlement.currency,
                reasons=reasons,
                grace_applied=ent.grace_applied,
                delivery_plant=shipment.plant,
                delivery_region=delivery_region,
                transit=transit,
                customer_error=customer_error,
            )
        )
    return allocations


def settle(
    case: Case, allocations: Sequence[Allocation] | None = None
) -> list[LedgerEntry]:
    """出账：未受争议波及的按认领付款，争议部分挂起，违约单独列示。"""
    if allocations is None:
        allocations = allocate(case)
    entries: list[LedgerEntry] = []
    for alloc in allocations:
        if alloc.status == "direct":
            continue
        if alloc.fee_amount > 0 and alloc.paying_dealer:
            entries.append(
                LedgerEntry(
                    kind="channel_fee",
                    dealer=alloc.paying_dealer,
                    customer=case.order.buyer,
                    line_id=alloc.line_id,
                    shipment=alloc.shipment,
                    region=alloc.region,
                    amount=alloc.fee_amount,
                    currency=alloc.currency,
                    note=f"实际交货 {alloc.qty} 箱的渠道报酬",
                )
            )
        if alloc.held_amount > 0:
            entries.append(
                LedgerEntry(
                    kind="hold",
                    dealer=None,
                    customer=case.order.buyer,
                    line_id=alloc.line_id,
                    shipment=alloc.shipment,
                    region=alloc.region,
                    amount=alloc.held_amount,
                    currency=alloc.currency,
                    note="认领争议未决，争议金额暂缓出账",
                )
            )
        if alloc.breach_amount > 0:
            entries.append(
                LedgerEntry(
                    kind="breach_compensation",
                    dealer=alloc.contract_dealer,
                    customer=case.order.buyer,
                    line_id=alloc.line_id,
                    shipment=alloc.shipment,
                    region=alloc.region,
                    amount=alloc.breach_amount,
                    currency=alloc.currency,
                    note=f"跨区违约补偿，受损合同 {alloc.contract_id}",
                )
            )
    return entries


# ---------------------------------------------------------------------------
# 补送最终用途确认的差额处理
# ---------------------------------------------------------------------------


def apply_supplemental_confirmation(
    case: Case,
    *,
    line_id: str,
    plant: str,
    district: str,
    signed_by: str,
    at: datetime | None = None,
    confirmation_id: str = "SUP-1",
) -> Case:
    """补送的最终用途确认：保留原交接记录，改变该行现报最终用途。

    已签认的历史账可查；补送确认只影响按该用途重新核认后的差额。
    """
    order = case.order
    line = order.lines[line_id]
    if signed_by not in {"GP-11"}:
        raise RuleError("补送的最终用途确认缺少客户有权代表签字")
    moment = at or datetime.combine(case.as_of, datetime.min.time(), tzinfo=LOCAL_TZ)
    supplemental = order.supplemental + (
        Confirmation(
            id=confirmation_id,
            line=line_id,
            plant=plant,
            district=district,
            signed_by=signed_by,
            at=moment,
        ),
    )
    updated = replace(order, lines=dict(order.lines), supplemental=supplemental)
    updated.lines[line_id] = replace(line, plant=plant, district=district)
    return replace(case, order=updated)


def adjustment_entries(
    case: Case,
    prior: Sequence[Allocation],
    updated: Sequence[Allocation],
) -> list[LedgerEntry]:
    """补送的最终用途确认改变金额时，按差额挂到对的客户与区域。

    只调整渠道报酬的差额；违约补偿单独按最终核认的区域、受损合同与
    争议处理结论办理，不随补送确认直接重算。
    """

    def bucket(
        allocations: Sequence[Allocation],
    ) -> dict[tuple[str | None, str], Decimal]:
        totals: dict[tuple[str | None, str], Decimal] = {}
        for alloc in allocations:
            if alloc.status in {"direct", "misreport"}:
                continue
            if alloc.fee_amount == 0:
                continue
            key = (alloc.paying_dealer, alloc.region or "-")
            totals[key] = totals.get(key, money("0")) + alloc.fee_amount
        return totals

    before, after = bucket(prior), bucket(updated)
    entries: list[LedgerEntry] = []
    for key in sorted(set(before) | set(after), key=lambda k: (str(k[0]), str(k[1]))):
        delta = after.get(key, money("0")) - before.get(key, money("0"))
        if delta == 0:
            continue
        dealer, region = key
        entries.append(
            LedgerEntry(
                kind="adjustment",
                dealer=dealer,
                customer=case.order.buyer,
                line_id=None,
                shipment=None,
                region=None if region == "-" else region,
                amount=delta,
                currency=case.book.settlement.currency,
                note=(
                    "补送最终用途确认的渠道报酬差额：已发生交接保留可查，"
                    "差额挂到最终核认的区域与客户"
                ),
            )
        )
    return entries


# ---------------------------------------------------------------------------
# 可见性
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CustomerLineView:
    line_id: str
    qty: int
    plant: str
    district: str
    status: str
    message: str
    can_continue: bool
    needs_consent: bool
    needs_verification: bool
    dealer: str | None


@dataclass(frozen=True)
class DealerView:
    dealer: str
    contracts: tuple[dict[str, Any], ...]
    deliveries: tuple[dict[str, Any], ...]
    vouchers: tuple[dict[str, Any], ...]
    claims: tuple[dict[str, Any], ...]


def customer_view(
    case: Case, handovers: Mapping[str, Handover] | None = None
) -> list[CustomerLineView]:
    """总部客户在自己订单上分辨可继续提货 / 需另行同意 / 需核实。"""
    if handovers is None:
        handovers = assess_handover(case)
    return [
        CustomerLineView(
            line_id=line.id,
            qty=line.qty,
            plant=line.plant,
            district=line.district,
            status=handovers[line.id].status,
            message=CUSTOMER_MESSAGE.get(handovers[line.id].status, "状态未知"),
            can_continue=handovers[line.id].status in CONTINUABLE,
            needs_consent=handovers[line.id].status in NEEDS_CONSENT,
            needs_verification=handovers[line.id].status in NEEDS_VERIFICATION,
            dealer=handovers[line.id].dealer,
        )
        for line in case.order.lines.values()
    ]


def dealer_view(
    case: Case,
    dealer: str,
    allocations: Sequence[Allocation] | None = None,
    ledger: Sequence[LedgerEntry] | None = None,
) -> DealerView:
    """经销商只看到自己的授权、交货与报酬凭据。"""
    if allocations is None:
        allocations = allocate(case)
    if ledger is None:
        ledger = settle(case, allocations)
    contracts = tuple(
        {
            "id": contract.id,
            "region": contract.region,
            "skus": sorted(contract.skus),
            "from": contract.start.isoformat(),
            "to": contract.end.isoformat(),
            "exclusive": contract.exclusive,
            "fee_per_case": f"{contract.fee_per_case:.2f}",
        }
        for contract in case.book.contracts
        if contract.dealer == dealer
    )
    deliveries = tuple(
        {
            "shipment": alloc.shipment,
            "line": alloc.line_id,
            "qty": alloc.qty,
            "district": alloc.district,
            "region": alloc.region,
            "status": alloc.status,
            "fee": f"{alloc.fee_amount:.2f}",
            "held": f"{alloc.held_amount:.2f}",
        }
        for alloc in allocations
        if alloc.contract_dealer == dealer or alloc.paying_dealer == dealer
    )
    vouchers = tuple(
        {
            "kind": entry.kind,
            "shipment": entry.shipment,
            "region": entry.region,
            "amount": f"{entry.amount:.2f}",
            "currency": entry.currency,
            "note": entry.note,
        }
        for entry in ledger
        if entry.dealer == dealer
    )
    claims = tuple(
        {"id": claim.id, "line": claim.line, "shipment": claim.shipment, "qty": claim.qty}
        for claim in case.claims
        if claim.by == dealer
    )
    return DealerView(dealer, contracts, deliveries, vouchers, claims)


# ---------------------------------------------------------------------------
# 合同覆盖冲突
# ---------------------------------------------------------------------------


def contract_conflicts(book: TerritoryBook) -> list[tuple[str, str]]:
    """一个地区、同一产品、重叠期间出现多份独立排他合同。"""
    conflicts: list[tuple[str, str]] = []
    exclusive = [c for c in book.contracts if c.exclusive]
    for i, left in enumerate(exclusive):
        for right in exclusive[i + 1 :]:
            if left.region != right.region or not (left.skus & right.skus):
                continue
            if left.start <= right.end and right.start <= left.end:
                conflicts.append((left.id, right.id))
    return conflicts
