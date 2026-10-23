import unittest
from datetime import date

from core.retrieval import (
    EmbeddingMetadata,
    HashEmbeddingProvider,
    HybridRetriever,
    IndexedDocument,
)


class HybridRetrievalTests(unittest.TestCase):
    def test_temporal_filter_runs_before_ranking(self) -> None:
        documents = [
            IndexedDocument(
                document_id="old",
                text="260.6 significant change refund",
                authority="regulation",
                effective_from=date(2024, 1, 1),
                effective_to=date(2024, 12, 31),
            ),
            IndexedDocument(
                document_id="current",
                text="260.6 significant change refund owed",
                authority="regulation",
                effective_from=date(2025, 1, 1),
            ),
        ]

        hits, excluded = HybridRetriever(documents).search(
            "260.6 refund", as_of_date=date(2026, 7, 3)
        )

        self.assertEqual([hit.document.document_id for hit in hits], ["current"])
        self.assertEqual(excluded, 1)

    def test_filters_document_type_and_carrier(self) -> None:
        documents = [
            IndexedDocument(
                document_id="aa-contract",
                text="meal commitment during controllable cancellation",
                authority="contract",
                doc_type="contract",
                carrier="AA",
                effective_from=date(2025, 1, 1),
            ),
            IndexedDocument(
                document_id="dl-contract",
                text="meal commitment during controllable cancellation",
                authority="contract",
                doc_type="contract",
                carrier="DL",
                effective_from=date(2025, 1, 1),
            ),
        ]

        hits, _ = HybridRetriever(documents).search(
            "meal cancellation",
            as_of_date=date(2026, 1, 1),
            doc_types={"contract"},
            carrier="DL",
        )

        self.assertEqual([hit.document.document_id for hit in hits], ["dl-contract"])

    def test_detailed_result_reports_hash_provider_and_cache_identity(self) -> None:
        document = IndexedDocument(
            document_id="refund",
            text="refund after significant schedule change",
            authority="regulation",
            effective_from=date(2025, 1, 1),
        )
        retriever = HybridRetriever([document], embedding_provider=HashEmbeddingProvider())

        first = retriever.search_detailed("refund", as_of_date=date(2026, 1, 1))
        second = retriever.search_detailed("refund", as_of_date=date(2026, 1, 1))

        self.assertEqual(first.report.served_provider, "hash")
        self.assertIsNone(first.report.fallback_reason)
        self.assertEqual(first.report.corpus_hash, second.report.corpus_hash)
        self.assertEqual(first.report.candidate_count, 1)
        self.assertNotEqual(
            HashEmbeddingProvider(dimensions=256).metadata.cache_key(document),
            HashEmbeddingProvider(dimensions=128).metadata.cache_key(document),
        )

    def test_embedding_failure_is_explicit_and_uses_hash_fallback(self) -> None:
        class BrokenProvider:
            metadata = EmbeddingMetadata(
                provider="sentence-transformers",
                model_id="local-bge",
                model_version="test",
                pooling="mean",
                normalize=True,
                query_instruction="query: ",
                document_instruction="",
            )

            def embed_query(self, text: str) -> list[float]:
                raise RuntimeError("model files unavailable")

            def embed_documents(self, texts: list[str]) -> list[list[float]]:
                raise RuntimeError("model files unavailable")

        document = IndexedDocument(
            document_id="refund",
            text="refund after significant schedule change",
            authority="regulation",
            effective_from=date(2025, 1, 1),
        )
        result = HybridRetriever([document], embedding_provider=BrokenProvider()).search_detailed(
            "refund", as_of_date=date(2026, 1, 1)
        )

        self.assertEqual(result.report.embedding_provider, "sentence-transformers")
        self.assertEqual(result.report.served_provider, "hash")
        self.assertIn("model files unavailable", result.report.fallback_reason or "")

    def test_jurisdiction_authority_and_retrieval_layer_filter_before_ranking(self) -> None:
        documents = [
            IndexedDocument(
                document_id="regulation",
                text="refund obligation",
                authority="regulation",
                effective_from=date(2025, 1, 1),
                jurisdiction="US",
            ),
            IndexedDocument(
                document_id="notice",
                text="refund enforcement policy",
                authority="enforcement_policy",
                effective_from=date(2025, 1, 1),
                jurisdiction="US",
                doc_type="enforcement_policy",
            ),
        ]

        result = HybridRetriever(documents).search_detailed(
            "refund",
            as_of_date=date(2026, 1, 1),
            authority_types={"regulation"},
            jurisdictions={"US"},
        )

        self.assertEqual([hit.document.document_id for hit in result.hits], ["regulation"])
        self.assertEqual(result.report.excluded_by_authority, 1)

    def test_cross_encoder_is_bounded_versioned_and_falls_back_to_rrf(self) -> None:
        class FakeReranker:
            model_id = "fake-cross-encoder"
            model_version = "fixture-v1"

            def score(self, query: str, documents: list[str]) -> list[float]:
                return [float(index) for index, _ in enumerate(documents)]

        documents = [
            IndexedDocument(
                document_id=f"doc-{index}",
                text=f"refund clause {index}",
                authority="regulation",
                effective_from=date(2025, 1, 1),
            )
            for index in range(3)
        ]
        reranked = HybridRetriever(documents, reranker=FakeReranker()).search_detailed(
            "refund", as_of_date=date(2026, 1, 1), candidate_limit=2
        )
        self.assertEqual(reranked.report.reranker_provider, "fake-cross-encoder")
        self.assertEqual(reranked.report.reranker_model_version, "fixture-v1")
        self.assertEqual(reranked.report.candidate_count, 2)
        self.assertGreaterEqual(reranked.report.rerank_latency_ms, 0)

        class BrokenReranker(FakeReranker):
            def score(self, query: str, documents: list[str]) -> list[float]:
                raise RuntimeError("cross-encoder files unavailable")

        fallback = HybridRetriever(documents, reranker=BrokenReranker()).search_detailed(
            "refund", as_of_date=date(2026, 1, 1), candidate_limit=2
        )
        self.assertEqual(fallback.report.reranker_provider, "rrf")
        self.assertIn("cross-encoder files unavailable", fallback.report.fallback_reason or "")
        self.assertEqual(
            [hit.document.document_id for hit in fallback.hits],
            [
                hit.document.document_id
                for hit in sorted(fallback.hits, key=lambda item: -item.score)
            ],
        )


if __name__ == "__main__":
    unittest.main()
