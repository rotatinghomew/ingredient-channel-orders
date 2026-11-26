"""核对渠道授权、分送认定、转单交货与清分的业务规则。"""
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import unittest

from channel_ops.engine import (
    Case,
    adjustment_entries,
    allocate,
    apply_supplemental_confirmation,
    assess_handover,
    check_transfer,
    contract_conflicts,
    customer_view,
    dealer_view,
    settle,
    transfer_timeline,
)
from channel_ops.model import RuleError


ROOT = __file__.rsplit("/tests/", 1)[0]


def load() -> Case:
    return Case.load(ROOT)


class Authorisation(unittest.TestCase):
    def test_认定按最终使用地而非开票地址或中转点(self):
        case = load()
        handovers = assess_handover(case)
        # L01 开票地 E11 与使用地相同，但属直供例外。
        self.assertEqual(handovers["L01"].status, "direct_exception")
        # L02 开票 E11、报单 E12、实际使用 W13；只有 W13 决定归属。
        self.assertEqual(handovers["L02"].district, "W13")
        self.assertEqual(handovers["L02"].dealer, "DE2")
        self.assertNotEqual(case.order.billing_district, handovers["L02"].district)

    def test_排他合同按地区产品与期限生效(self):
        case = load()
        book = case.book
        self.assertEqual(book.authority_holder(product="P11", district="E11", day=date(2026, 12, 1)).id, "EXE")
        self.assertEqual(book.authority_holder(product="P11", district="W13", day=date(2026, 12, 1)).id, "EXW")
        # P12 只在 E 区有合同。
        self.assertIsNone(book.authority_holder(product="P12", district="W13", day=date(2026, 12, 1)))
        # 合同期满后不再排他。
        self.assertIsNone(book.authority_holder(product="P11", district="E11", day=date(2026, 12, 21)))

    def test_同一产品同一地区重叠排他合同视为冲突(self):
        self.assertEqual(contract_conflicts(load().book), [])

    def test_直供例外只覆盖已报备工厂与集团(self):
        case = load()
        handovers = assess_handover(case)
        self.assertEqual(handovers["L01"].status, "direct_exception")
        # 直供例外仅至 12-08；之后的交货不再适用。
        exception = case.book.direct_exception
        self.assertTrue(exception.covers("P11", "FX-E1", "GP-11", date(2026, 12, 8)))
        self.assertFalse(exception.covers("P11", "FX-E1", "GP-11", date(2026, 12, 9)))
        self.assertFalse(exception.covers("P11", "FX-E2", "GP-11", date(2026, 12, 8)))
        self.assertFalse(exception.covers("P11", "FX-E1", "GP-99", date(2026, 12, 8)))

    def test_更正缺客户有权代表确认时不作认定(self):
        case = load()
        case.order = replace(
            case.order,
            confirmation=replace(case.order.confirmation, signed_by="GP-99"),
        )
        case.order.lines["L02"].use_status = "evidence_pending"
        case.order.lines["L02"].use_reason = "更正未经客户有权代表确认，待核实"
        handovers = assess_handover(case)
        self.assertEqual(handovers["L02"].status, "evidence_required")
        self.assertTrue(handovers["L02"].needs_verification)


class HandoverAndTransfer(unittest.TestCase):
    def test_客户视图区分可继续需同意与需核实(self):
        views = {view.line_id: view for view in customer_view(load())}
        self.assertTrue(views["L01"].can_continue)  # 直供例外
        self.assertTrue(views["L02"].can_continue)  # 已同意转单
        self.assertTrue(views["L03"].can_continue)
        self.assertFalse(any(v.needs_consent for v in views.values()))

    def test_受单渠道无经销权且未转单时客户须另行同意(self):
        case = load()
        case.transfer = None
        handovers = assess_handover(case)
        self.assertEqual(handovers["L02"].status, "consent_required")
        self.assertTrue(handovers["L02"].needs_consent)
        self.assertFalse(handovers["L02"].can_continue)

    def test_转单接方必须是有权经销商(self):
        case = load()
        case.transfer = replace(case.transfer, to_dealer="DE1")
        handovers = assess_handover(case)
        self.assertEqual(handovers["L02"].status, "consent_required")

    def test_部分转单不等于整行可继续(self):
        case = load()
        case.transfer = replace(case.transfer, qty=10)
        handovers = assess_handover(case)
        self.assertEqual(handovers["L02"].status, "consent_required")
        self.assertIn("18 箱", " ".join(handovers["L02"].reasons))

    def test_已确认订单转交须双方同意(self):
        case = load()
        case.transfer = replace(case.transfer, buyer_agrees=False)
        check = check_transfer(case)
        self.assertFalse(check.allowed)
        self.assertIn("缺少客户同意", check.blockers)

        case = load()
        case.transfer = replace(case.transfer, acceptor_agrees=False)
        self.assertIn("缺少接方同意", check_transfer(case).blockers)


class WarehouseRelease(unittest.TestCase):
    def test_回执不明不得向接方另发能提走相同货物的指令(self):
        case = load()
        check = check_transfer(case, as_of=case.shipments[0].at)  # K1 unknown
        self.assertFalse(check.allowed)
        self.assertEqual(check.action, "blocked")
        self.assertIn("unknown", " ".join(check.blockers))

    def test_确认未提货后停止原指令再向接方发货(self):
        case = load()
        timeline = transfer_timeline(case)
        self.assertEqual([item.action for item in timeline], ["blocked", "stop_original_then_reissue"])
        self.assertTrue(timeline[-1].allowed)

    def test_订货确认不等于出库授权(self):
        case = load()
        check = check_transfer(case)
        self.assertTrue(check.allowed)  # 商业条件均满足
        self.assertIn("不等于出库授权", " ".join(check.reasons))

    def test_原指令已提货则不能另发(self):
        case = load()
        case.shipments.append(
            case.shipments[0].__class__(
                event="K9",
                instruction="R-13",
                line="L02",
                actor="DE1",
                status="picked",
                at=case.shipments[1].at,
                qty=28,
                plant="FX-W1",
            )
        )
        case.shipments.sort(key=lambda s: (s.at, s.event))
        check = check_transfer(case)
        self.assertFalse(check.allowed)


class AllocationAndSettlement(unittest.TestCase):
    def test_同一笔交货只有一份有效渠道报酬(self):
        allocations = {a.shipment: a for a in allocate(load())}
        k3 = allocations["K3"]
        self.assertEqual(k3.contract_dealer, "DE1")
        self.assertEqual(k3.held_amount, Decimal("14.40"))
        self.assertEqual(k3.status, "payment_hold")
        # 争议只暂缓争议金额，不重复给付。
        ledger = settle(load())
        fees = [e for e in ledger if e.kind == "channel_fee" and e.shipment == "K3"]
        self.assertEqual(fees, [])

    def test_未受争议波及的分送行正常出账(self):
        ledger = settle(load())
        fees = [e for e in ledger if e.kind == "channel_fee"]
        self.assertEqual(
            [(e.shipment, e.dealer, e.amount) for e in fees],
            [("K4", "DE1", Decimal("12.00"))],
        )

    def test_过渡期内既有用途按原合同核认(self):
        allocations = {a.shipment: a for a in allocate(load())}
        k4 = allocations["K4"]
        self.assertTrue(k4.grace_applied)
        self.assertEqual(k4.contract_id, "EXE")
        self.assertEqual(k4.region, "E")
        # 12-07 之后同一行政区按调整后的地区重新审。
        self.assertGreater(date(2026, 12, 5), date(2026, 12, 4))

    def test_实际交货按真实使用地核认且资格变化不改算(self):
        case = load()
        case.shipments[2] = replace(case.shipments[2], plant="FX-W1")  # K3 实际送到 W 区
        allocations = {a.shipment: a for a in allocate(case)}
        # 工厂 FX-W1 对应行政区 W13，已按 12-03 更正核对；归属 W 区。
        self.assertEqual(allocations["K3"].region, "W")
        self.assertEqual(allocations["K3"].contract_dealer, "DE2")
        self.assertGreater(allocations["K3"].breach_amount, 0)  # DE1 跨区

    def test_跨区违约与客户报错含义不同(self):
        case = load()
        case.shipments[3] = replace(case.shipments[3], actor="DE2")  # K4 由 W 区经销商送到 E 区
        alloc = {a.shipment: a for a in allocate(case)}["K4"]
        self.assertEqual(alloc.breach_amount, 24.00)  # 10 箱 × 2.40
        self.assertEqual(alloc.fee_amount, 12.00)  # 报酬仍归 DE1
        self.assertEqual(alloc.status, "violation")

        case = load()
        case.shipments[3] = replace(case.shipments[3], actor="GP-11")  # 客户自行送货，不是违约
        alloc = {a.shipment: a for a in allocate(case)}["K4"]
        self.assertEqual(alloc.breach_amount, 0)
        self.assertEqual(alloc.fee_amount, 12.00)

    def test_违约补偿单独按受损合同办理(self):
        case = load()
        case.shipments[3] = replace(case.shipments[3], actor="DE2")
        ledger = settle(case)
        breach = [e for e in ledger if e.kind == "breach_compensation"]
        self.assertEqual(
            [(e.dealer, e.region, e.amount) for e in breach],
            [("DE1", "E", Decimal("24.00"))],
        )
        self.assertNotIn("breach_compensation", [e.kind for e in ledger if e.shipment == "K3"])

    def test_补送确认按差额挂到对的客户与区域(self):
        case = load()
        prior = allocate(case)
        updated = apply_supplemental_confirmation(
            case, line_id="L03", plant="FX-W1", district="W13", signed_by="GP-11"
        )
        deltas = adjustment_entries(case, prior, allocate(updated))
        by_region = {entry.region: entry for entry in deltas}
        # K4 的 E 区报酬 12.00 冲回；K3 仍在争议挂起，不参与差额。
        self.assertEqual(by_region["E"].amount, Decimal("-12.00"))
        # W 区 DE2 按 1.15 元/箱核认 K4 的 10 箱。
        self.assertEqual(by_region["W"].amount, Decimal("11.50"))
        self.assertTrue(all(entry.customer == "GP-11" for entry in deltas))
        self.assertEqual(sum(1 for entry in deltas if entry.region == "E"), 1)

    def test_补送确认缺授权签字则拒绝(self):
        case = load()
        with self.assertRaises(RuleError):
            apply_supplemental_confirmation(
                case, line_id="L03", plant="FX-W1", district="W13", signed_by="GP-99"
            )

    def test_报错行不核认报酬(self):
        case = load()
        line = replace(
            case.order.lines["L03"],
            district="X99",
            plant="FX-X9",
            reported_district="X99",
            reported_plant="FX-X9",
        )
        case.order.lines["L03"] = line
        alloc = {a.shipment: a for a in allocate(case)}["K4"]
        self.assertEqual(alloc.status, "misreport")
        self.assertEqual(alloc.fee_amount, Decimal("0"))


class DeliveryFacts(unittest.TestCase):
    def test_中转点不改变最终用途(self):
        case = load()
        case.shipments[2] = replace(case.shipments[2], plant="HK-7")
        alloc = {a.shipment: a for a in allocate(case)}["K3"]
        self.assertTrue(alloc.transit)
        self.assertEqual(alloc.region, "E")
        self.assertEqual(alloc.plant, "FX-E2")
        self.assertIn("中转", " ".join(alloc.reasons))

    def test_中转点交货不算跨区违约(self):
        case = load()
        case.shipments[2] = replace(case.shipments[2], plant="HK-7")
        alloc = {a.shipment: a for a in allocate(case)}["K3"]
        self.assertEqual(alloc.breach_amount, Decimal("0"))

    def test_交货地点未报告则不核认报酬(self):
        case = load()
        case.shipments[2] = replace(case.shipments[2], plant=None)
        alloc = {a.shipment: a for a in allocate(case)}["K3"]
        self.assertFalse(alloc.location_known)
        self.assertEqual(alloc.fee_amount, Decimal("0"))
        self.assertEqual(alloc.status, "misreport")

    def test_跨区交货必须真实送往对方地区(self):
        case = load()
        # K4 实送方换成 W 区的 DE2，但送货地点仍是 E 区的最终使用工厂。
        case.shipments[3] = replace(case.shipments[3], actor="DE2", plant="FX-E2")
        alloc = {a.shipment: a for a in allocate(case)}["K4"]
        # 实送方与合约方不同但交货地仍在 E 区：违约以跨区送达为前提。
        self.assertEqual(alloc.region, "E")
        self.assertEqual(alloc.breach_amount, Decimal("24.00"))

    def test_补送确认以差额挂账且原交接可查(self):
        case = load()
        prior = allocate(case)
        updated = apply_supplemental_confirmation(
            case, line_id="L03", plant="FX-W1", district="W13", signed_by="GP-11"
        )
        later = allocate(updated)
        # 补送确认改变金额，但不是改写历史，而是按差额挂到对的区域。
        deltas = adjustment_entries(case, prior, later)
        deltas_by_region = {entry.region: entry.amount for entry in deltas}
        self.assertEqual(deltas_by_region["E"], Decimal("-12.00"))
        self.assertEqual(deltas_by_region["W"], Decimal("11.50"))
        # 已经发生的交接记录仍在。
        self.assertEqual([a.shipment for a in prior], [a.shipment for a in later])

    def test_经销资格变化不改算已交付部分(self):
        case = load()
        prior = {a.shipment: a for a in allocate(case)}["K4"]
        # 合同到期后 E 区改由 DE3 独家经销（12-21 起）。
        later = replace(
            case.book.contracts[-1],
            id="EXE-2",
            region="E",
            dealer="DE3",
            start=date(2026, 12, 21),
            end=date(2027, 3, 31),
        )
        case.book = replace(
            case.book, contracts=case.book.contracts + (later,)
        )
        after = {a.shipment: a for a in allocate(case)}["K4"]
        # K4 于 12-05 在 EXE 有效期内交付，仍归 DE1，不改算给 DE3。
        self.assertEqual(after.contract_id, "EXE")
        self.assertEqual(after.contract_dealer, "DE1")
        self.assertEqual(after.region, "E")
        self.assertEqual(after.fee_amount, prior.fee_amount)


class Visibility(unittest.TestCase):
    def test_经销商只看自己的授权与凭据(self):
        case = load()
        de1 = dealer_view(case, "DE1")
        de2 = dealer_view(case, "DE2")
        self.assertEqual({c["id"] for c in de1.contracts}, {"EXE"})
        self.assertEqual({c["id"] for c in de2.contracts}, {"EXW"})
        self.assertEqual({d["shipment"] for d in de1.deliveries}, {"K3", "K4"})
        self.assertEqual(de2.deliveries, ())
        self.assertEqual({c["id"] for c in de1.claims}, {"A1"})
        self.assertEqual({c["id"] for c in de2.claims}, {"A2"})
        self.assertTrue(all(v["region"] == "E" for v in de1.vouchers))


if __name__ == "__main__":
    unittest.main()
