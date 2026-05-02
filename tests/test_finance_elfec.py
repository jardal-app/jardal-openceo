from __future__ import annotations

import sys
import unittest
from pathlib import Path


CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from agents.finance.elfec import parse_pending_invoice_text, select_oldest_invoice


class ElfecInvoiceParsingTests(unittest.TestCase):
    def test_prefers_billing_month_over_due_date(self) -> None:
        invoice = parse_pending_invoice_text("(House) 375.80 2025-12 2026-01-28", 0)
        self.assertEqual(invoice.label, "House")
        self.assertEqual(invoice.month_label, "2025-12")
        self.assertEqual(invoice.due_date_label, "2026-01-28")
        self.assertEqual(str(invoice.amount_value), "375.80")

    def test_handles_comma_decimal_amount(self) -> None:
        invoice = parse_pending_invoice_text("(House) 1.234,56 2025-11 2025-12-28", 0)
        self.assertEqual(invoice.month_label, "2025-11")
        self.assertEqual(str(invoice.amount_value), "1234.56")

    def test_selects_oldest_invoice_and_remaining(self) -> None:
        selected, remaining = select_oldest_invoice(
            [
                "(House) 380.10 2026-02 2026-03-28",
                "(House) 375.80 2025-12 2026-01-28",
                "(House) 378.00 2026-01 2026-02-28",
            ]
        )
        self.assertEqual(selected.month_label, "2025-12")
        self.assertEqual([invoice.month_label for invoice in remaining], ["2026-01", "2026-02"])


if __name__ == "__main__":
    unittest.main()
