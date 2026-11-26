"""渠道授权、分送认定、移交交货与清分模型。

口径来源：``交换记录.md`` 与 ``inputs/*.json``。本模块只使用标准库，
所有金额以 ``Decimal`` 计算，时间统一换算到合同使用的 UTC+08:00 日历日。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

LOCAL_TZ = timezone(timedelta(hours=8))

# 出库回执状态。unknown 只表示回执不明，既不是“未提”也不是“已送”。
SHIPPING_STATUSES = frozenset(
    {"unknown", "not_picked", "picked", "in_transit", "delivered"}
)

# 最终使用地更正必须有客户有权代表的签字。
AUTHORIZED_SIGNERS = frozenset({"GP-11"})


class RuleError(Exception):
    """记录不满足约定口径。"""


# ---------------------------------------------------------------------------
# 时间与金额
# ---------------------------------------------------------------------------


def local_dt(value: str) -> datetime:
    """解析带时区的时间，并换算到业务使用的 UTC+08:00。"""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise RuleError(f"时间缺少偏移量：{value}")
    return parsed.astimezone(LOCAL_TZ)


def money(value: str | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# 合同
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Contract:
    id: str
    region: str
    dealer: str
    skus: frozenset[str]
    districts: frozenset[str]
    start: date
    end: date
    exclusive: bool
    fee_per_case: Decimal

    def covers(self, product: str, district: str, day: date) -> bool:
        return (
            product in self.skus
            and district in self.districts
            and self.start <= day <= self.end
        )


@dataclass(frozen=True)
class DirectException:
    parent: str
    plants: frozenset[str]
    skus: frozenset[str]
    until: date

    def covers(self, product: str, plant: str, buyer: str, day: date) -> bool:
        return (
            product in self.skus
            and plant in self.plants
            and buyer == self.parent
            and day <= self.until
        )


@dataclass(frozen=True)
class TerritoryMove:
    district: str
    to_region: str
    effective: date
    grace_until: date


@dataclass(frozen=True)
class Settlement:
    breach_fee_per_case: Decimal
    currency: str


@dataclass(frozen=True)
class TerritoryBook:
    product: str
    contracts: tuple[Contract, ...]
    direct_exception: DirectException | None
    move: TerritoryMove | None
    settlement: Settlement

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "TerritoryBook":
        regions = {
            region["id"]: frozenset(region["districts"])
            for region in payload["regions"]
        }
        contracts = tuple(
            Contract(
                id=row["id"],
                region=row["region"],
                dealer=row["dealer"],
                skus=frozenset(row["skus"]),
                districts=regions[row["region"]],
                start=date.fromisoformat(row["from"]),
                end=date.fromisoformat(row["to"]),
                exclusive=bool(row["exclusive"]),
                fee_per_case=money(row["fee_per_case"]),
            )
            for row in payload["contracts"]
        )
        exception = payload.get("direct_exception")
        move = payload.get("move")
        settle = payload["settle_violation"]
        return cls(
            product=payload["product"],
            contracts=contracts,
            direct_exception=(
                DirectException(
                    parent=exception["parent"],
                    plants=frozenset(exception["plants"]),
                    skus=frozenset(exception["skus"]),
                    until=date.fromisoformat(exception["until"]),
                )
                if exception
                else None
            ),
            move=(
                TerritoryMove(
                    district=move["district"],
                    to_region=move["to"],
                    effective=date.fromisoformat(move["effective"]),
                    grace_until=date.fromisoformat(move["existing_use_grace_until"]),
                )
                if move
                else None
            ),
            settlement=Settlement(
                breach_fee_per_case=money(settle["breach_fee_per_case"]),
                currency=settle["currency"],
            ),
        )

    # -- 行政区在其时点所属的地区 -------------------------------------
    def region_of(self, district: str, day: date) -> str | None:
        move = self.move
        if move and move.district == district and day >= move.effective:
            return move.to_region
        for contract in self.contracts:
            if district in contract.districts:
                return contract.region
        return None

    def region_before_move(self, district: str) -> str | None:
        for contract in self.contracts:
            if district in contract.districts:
                return contract.region
        return None

    def contracts_in(self, region: str) -> tuple[Contract, ...]:
        return tuple(c for c in self.contracts if c.region == region)

    def contract_for(self, region: str | None, product: str, day: date) -> Contract | None:
        if region is None:
            return None
        for contract in self.contracts_in(region):
            if contract.exclusive and product in contract.skus and contract.start <= day <= contract.end:
                return contract
        return None

    # -- 授权认定 ------------------------------------------------------
    def authority_holder(
        self, *, product: str, district: str | None, day: date
    ) -> Contract | None:
        """返回在该产品、地区、日期上拥有排他授权的合同。"""
        if district is None:
            return None
        return self.contract_for(self.region_of(district, day), product, day)


# ---------------------------------------------------------------------------
# 订购单与分送行
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Confirmation:
    id: str
    line: str
    plant: str
    district: str
    signed_by: str
    at: datetime
    qty: int | None = None

    @property
    def signed(self) -> bool:
        return self.signed_by in AUTHORIZED_SIGNERS


@dataclass
class Line:
    id: str
    plant: str
    district: str
    qty: int
    within_order_independent: bool
    sales: str
    # 报单时的原始值，用于识别后来新增的分送地或数量。
    reported_plant: str = ""
    reported_district: str = ""
    reported_qty: int = 0
    # 目的确认后的状态。
    use_status: str = "confirmed"  # confirmed | evidence_pending | unconfirmed
    use_reason: str = ""

    def __post_init__(self) -> None:
        if not self.reported_plant:
            self.reported_plant = self.plant
        if not self.reported_district:
            self.reported_district = self.district
        if not self.reported_qty:
            self.reported_qty = self.qty


@dataclass
class Order:
    order: str
    buyer: str
    billing_district: str
    sku: str
    hub: str
    lines: dict[str, Line]
    confirmation: Confirmation | None = None
    # 交货后补送、经客户有权代表签认的最终用途确认。
    supplemental: tuple[Confirmation, ...] = ()

    def confirmations_for(self, line_id: str) -> list[Confirmation]:
        found = []
        if self.confirmation is not None and self.confirmation.line == line_id:
            found.append(self.confirmation)
        found.extend(c for c in self.supplemental if c.line == line_id)
        return sorted(found, key=lambda c: c.at)

    def confirmations_for_any(self) -> list[Confirmation]:
        found = list(self.supplemental)
        if self.confirmation is not None:
            found.append(self.confirmation)
        return found

    def signed_confirmation_for(self, line_id: str) -> Confirmation | None:
        signed = [c for c in self.confirmations_for(line_id) if c.signed]
        return signed[-1] if signed else None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Order":
        lines = {
            row["id"]: Line(
                id=row["id"],
                plant=row["plant"],
                district=row["district"],
                qty=int(row["qty"]),
                within_order_independent=bool(row["within_order_independent"]),
                sales=row["sales"],
            )
            for row in payload["lines"]
        }
        raw = payload.get("destination_confirmation")
        confirmation = None
        if raw:
            if raw["line"] not in lines:
                raise RuleError(f"更正指向不存在的分送行：{raw['line']}")
            confirmation = Confirmation(
                id=raw["id"],
                line=raw["line"],
                plant=raw["plant"],
                district=raw["district"],
                signed_by=raw["signed_by"],
                at=local_dt(raw["at"]),
                qty=int(raw["qty"]) if raw.get("qty") is not None else None,
            )
            line = lines[raw["line"]]
            if not confirmation.signed:
                line.use_status = "evidence_pending"
                line.use_reason = "更正未经客户有权代表确认，待核实"
            else:
                line.district = confirmation.district
                line.plant = confirmation.plant
                if confirmation.qty is not None:
                    line.qty = confirmation.qty
        return cls(
            order=payload["order"],
            buyer=payload["buyer"],
            billing_district=payload["billing_district"],
            sku=payload["sku"],
            hub=payload["hub"],
            lines=lines,
            confirmation=confirmation,
        )


# ---------------------------------------------------------------------------
# 出库回执
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Shipment:
    event: str
    instruction: str
    line: str
    actor: str
    status: str
    at: datetime
    qty: int | None
    plant: str | None

    @property
    def delivered(self) -> bool:
        return self.status == "delivered"

    @property
    def blocks_reissue(self) -> bool:
        """回执不明时，不得向接方另发能提走相同货物的指令。"""
        return self.status == "unknown"

    @property
    def stops_instruction(self) -> bool:
        """确认尚未提货后可停止原指令。"""
        return self.status == "not_picked"

    @property
    def day(self) -> date:
        return self.at.date()


def shipments_from_payload(payload: Sequence[Mapping[str, Any]]) -> list[Shipment]:
    result = []
    for row in payload:
        if row["status"] not in SHIPPING_STATUSES:
            raise RuleError(f"未知出库回执状态：{row['status']}")
        result.append(
            Shipment(
                event=row["event"],
                instruction=row["instruction"],
                line=row["line"],
                actor=row["actor"],
                status=row["status"],
                at=local_dt(row["at"]),
                qty=int(row["qty"]) if row.get("qty") is not None else None,
                plant=row.get("plant"),
            )
        )
    return sorted(result, key=lambda s: (s.at, s.event))


# ---------------------------------------------------------------------------
# 转单、申领与移交
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Transfer:
    id: str
    line: str
    from_dealer: str
    to_dealer: str
    qty: int
    buyer_agrees: bool
    acceptor_agrees: bool


@dataclass(frozen=True)
class Claim:
    id: str
    line: str
    shipment: str
    by: str
    qty: int


def transfer_from_payload(payload: Mapping[str, Any] | None) -> Transfer | None:
    if not payload:
        return None
    return Transfer(
        id=payload["id"],
        line=payload["line"],
        from_dealer=payload["from"],
        to_dealer=payload["to"],
        qty=int(payload["qty"]),
        buyer_agrees=bool(payload["buyer_agrees"]),
        acceptor_agrees=bool(payload["acceptor_agrees"]),
    )


def claims_from_payload(payload: Sequence[Mapping[str, Any]]) -> list[Claim]:
    return [
        Claim(
            id=row["id"],
            line=row["line"],
            shipment=row["shipment"],
            by=row["by"],
            qty=int(row["qty"]),
        )
        for row in payload
    ]


# ---------------------------------------------------------------------------
# 认定结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Handover:
    """分送行在客户报单时点的认定结果。"""

    line_id: str
    qty: int
    district: str
    plant: str
    status: str
    contract_id: str | None
    dealer: str | None
    reasons: tuple[str, ...]

    @property
    def can_continue(self) -> bool:
        """客户能否按现报用途继续安排提货。"""
        return self.status in {"authorised", "direct_exception", "transferred"}

    @property
    def needs_consent(self) -> bool:
        return self.status == "consent_required"

    @property
    def needs_verification(self) -> bool:
        return self.status in {"evidence_required", "review_required", "no_authority"}


@dataclass(frozen=True)
class Allocation:
    """单笔已交货回执的渠道核认。"""

    shipment: str
    line_id: str
    qty: int
    district: str
    region: str | None
    plant: str | None
    status: str
    contract_id: str | None
    contract_dealer: str | None
    paying_dealer: str | None
    fee_amount: Decimal
    breach_amount: Decimal
    held_amount: Decimal
    currency: str
    reasons: tuple[str, ...]
    grace_applied: bool = False
    delivery_plant: str | None = None
    delivery_region: str | None = None
    transit: bool = False
    location_known: bool = True
    customer_error: bool = False


@dataclass(frozen=True)
class TransferCheck:
    transfer_id: str
    line_id: str
    allowed: bool
    action: str
    consents: tuple[str, ...]
    blockers: tuple[str, ...]
    authorised_dealer: str | None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class LedgerEntry:
    """清分账：一笔渠道报酬或违约补偿。"""

    kind: str  # channel_fee | breach_compensation | adjustment | hold
    dealer: str | None
    customer: str | None
    line_id: str | None
    shipment: str | None
    region: str | None
    amount: Decimal
    currency: str
    note: str
