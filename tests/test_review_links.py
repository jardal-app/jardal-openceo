from __future__ import annotations

import sys
import unittest
from pathlib import Path


CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from agents.dev.review_links import apply_review_links_to_params, extract_review_links


class ReviewLinksTests(unittest.TestCase):
    def test_extracts_explicit_review_links_block(self) -> None:
        parsed = extract_review_links(
            """
            Trabajo terminado.

            REVIEW_LINKS_START
            https://dev.melanyryr.jardal.app/dashboard/inventario/transferencias
            /dashboard/inventario/transferencias/crear
            REVIEW_LINKS_END
            """,
            base_url="https://dev.melanyryr.jardal.app",
        )

        self.assertTrue(parsed.explicit_block)
        self.assertEqual(
            parsed.links,
            [
                "https://dev.melanyryr.jardal.app/dashboard/inventario/transferencias",
                "https://dev.melanyryr.jardal.app/dashboard/inventario/transferencias/crear",
            ],
        )

    def test_extracts_none_marker_from_explicit_block(self) -> None:
        parsed = extract_review_links(
            """
            REVIEW_LINKS_START
            NONE
            REVIEW_LINKS_END
            """,
            base_url="https://dev.melanyryr.jardal.app",
        )

        self.assertTrue(parsed.explicit_block)
        self.assertEqual(parsed.links, [])

    def test_fallback_extracts_dashboard_paths(self) -> None:
        parsed = extract_review_links(
            "Abrí /dashboard/ventas/reporte y luego https://dev.melanyryr.jardal.app/dashboard/ventas/editar/123",
            base_url="https://dev.melanyryr.jardal.app",
        )

        self.assertFalse(parsed.explicit_block)
        self.assertEqual(
            parsed.links,
            [
                "https://dev.melanyryr.jardal.app/dashboard/ventas/editar/123",
                "https://dev.melanyryr.jardal.app/dashboard/ventas/reporte",
            ],
        )

    def test_apply_review_links_sets_primary_urls(self) -> None:
        updated = apply_review_links_to_params(
            {"pm_ticket": {"module": "UX"}},
            extract_review_links(
                """
                REVIEW_LINKS_START
                /dashboard/ventas/reporte
                REVIEW_LINKS_END
                """,
                base_url="https://dev.melanyryr.jardal.app",
            ),
        )

        self.assertEqual(
            updated,
            {
                "pm_ticket": {"module": "UX"},
                "review_links": ["https://dev.melanyryr.jardal.app/dashboard/ventas/reporte"],
                "test_url": "https://dev.melanyryr.jardal.app/dashboard/ventas/reporte",
                "client_review_url": "https://dev.melanyryr.jardal.app/dashboard/ventas/reporte",
            },
        )

    def test_apply_review_links_clears_existing_when_block_is_none(self) -> None:
        updated = apply_review_links_to_params(
            {
                "review_links": ["https://dev.melanyryr.jardal.app/dashboard/ventas/reporte"],
                "test_url": "https://dev.melanyryr.jardal.app/dashboard/ventas/reporte",
                "client_review_url": "https://dev.melanyryr.jardal.app/dashboard/ventas/reporte",
            },
            extract_review_links(
                """
                REVIEW_LINKS_START
                NONE
                REVIEW_LINKS_END
                """,
                base_url="https://dev.melanyryr.jardal.app",
            ),
        )

        self.assertEqual(updated, {})
