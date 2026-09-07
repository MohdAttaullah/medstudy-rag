"""Expansion coverage over explicit synthetic source requirements, independent of chunk recall."""

import json
from pathlib import Path
from statistics import mean
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from app.core.chunking_config import ChunkingConfig
from app.core.reranking_config import EvidenceBudgetConfig, ExpansionConfig
from app.evidence.assembly import EvidenceAssembler
from app.evidence.model import ArtifactRef, EvidenceSource, SourceSpan
from app.ingestion.chunking.builder import Builder
from app.ingestion.chunking.model import ChunkInput, SourceElement, Span
from app.ingestion.chunking.tokenizer import LocalTokenizer
from app.retrieval.model import Provenance


def evaluate_context(root: Path) -> dict[str, Any]:
    gold = json.loads(
        (root / "backend/tests/fixtures/reranking/context-gold.json").read_text(encoding="utf-8")
    )
    fixtures = json.loads(
        (root / "backend/tests/fixtures/chunking/gold.json").read_text(encoding="utf-8")
    )
    fixtures["cases"].extend(gold.get("extra_fixtures", []))
    tokens = LocalTokenizer(ChunkingConfig())
    report = {"version": gold["version"], "notice": gold["notice"], "policies": {}}
    for neighbours in (0, 1):
        for budget in (64, 512, 1024, 4096):
            results = []
            for case in fixtures["cases"]:
                if case["id"] not in gold["cases"]:
                    continue
                expected = gold["cases"][case["id"]]
                frozen = ChunkInput.model_validate(case["source"])
                dataset = Builder(frozen, ChunkingConfig(**case.get("config", {})), tokens).build()
                elements = {e.id: e for e in frozen.elements}
                artifacts = {a.id: a for a in frozen.artifacts}
                run = uuid5(NAMESPACE_URL, case["id"] + ":m6")
                tenant = uuid5(NAMESPACE_URL, "synthetic-eval-tenant")
                sources = {}
                for chunk in dataset.chunks:

                    def span(s: Span, elements: dict[UUID, SourceElement] = elements) -> SourceSpan:
                        e = elements[s.element_id]
                        return SourceSpan(
                            element_id=e.id,
                            start=s.start,
                            end=s.end,
                            text=e.text[s.start : s.end],
                            page=e.page_number,
                            reading_order=e.reading_order,
                            role=s.role,
                            bbox=e.bbox or (None, None, None, None),
                        )

                    refs = [
                        ArtifactRef(
                            artifact_id=a.id,
                            kind=a.kind,
                            source_element_id=a.element_id,
                            href="/fixture-only",
                            row_indexes=chunk.metadata.get("row_indexes", []),
                            header_rows=chunk.metadata.get("header_rows", []),
                            cells=chunk.metadata.get("cells", []),
                            image_available=a.data.get("image_available", False),
                        )
                        for aid in chunk.artifact_ids
                        for a in [artifacts[aid]]
                    ]
                    p = Provenance(
                        document_id=frozen.document_id,
                        document_version_id=frozen.version_id,
                        chunk_run_id=run,
                        document_title=frozen.title,
                        source_type=frozen.source_type,
                        authority_level=frozen.authority_level,
                        subject=None,
                        specialty=None,
                        chunk_type=chunk.kind,
                        page_start=chunk.page_start,
                        page_end=chunk.page_end,
                        sequence_number=chunk.sequence,
                        parent_chunk_id=uuid5(NAMESPACE_URL, chunk.parent_key)
                        if chunk.parent_key
                        else None,
                        question_id=None,
                    )
                    sources[chunk.key] = EvidenceSource(
                        tenant_id=tenant,
                        chunk_id=uuid5(NAMESPACE_URL, chunk.key),
                        parse_run_id=frozen.parse_run_id,
                        provenance=p,
                        retrieval_text=chunk.retrieval_text,
                        text=chunk.source_text,
                        spans=[span(s) for s in chunk.spans],
                        artifacts=refs,
                        hierarchy=[
                            SourceSpan(
                                element_id=e.id,
                                start=0,
                                end=len(e.text),
                                text=e.text,
                                page=e.page_number,
                                reading_order=e.reading_order,
                                role="HIERARCHY",
                                bbox=e.bbox or (None, None, None, None),
                            )
                            for eid in chunk.hierarchy
                            for e in [elements[eid]]
                        ],
                    )
                anchors = [
                    s for s in sources.values() if s.provenance.chunk_type == expected["kind"]
                ]
                if not anchors:
                    results.append({"case": case["id"], "failure": "ANCHOR_NOT_BUILT"})
                    continue
                anchor = next(
                    (
                        s
                        for s in anchors
                        if any(elements[span.element_id].kind == "PARAGRAPH" for span in s.spans)
                    ),
                    anchors[0],
                )
                relatives = []
                if anchor.provenance.parent_chunk_id:
                    parent = next(
                        s
                        for s in sources.values()
                        if s.chunk_id == anchor.provenance.parent_chunk_id
                    )
                    relatives.append(parent)
                    siblings = sorted(
                        (
                            s
                            for s in sources.values()
                            if s.provenance.parent_chunk_id == anchor.provenance.parent_chunk_id
                        ),
                        key=lambda s: s.provenance.sequence_number,
                    )
                    position = siblings.index(anchor)
                    relatives.extend(
                        siblings[max(0, position - 1) : position]
                        + siblings[position + 1 : position + 2]
                    )
                blocks, warnings, duplicates = EvidenceAssembler(
                    ExpansionConfig(previous_siblings=neighbours, next_siblings=neighbours),
                    EvidenceBudgetConfig(max_total_tokens=budget),
                    tokens.count,
                ).assemble([anchor], {anchor.chunk_id: relatives})
                required = {
                    e.id for e in frozen.elements if e.reading_order in expected["required_orders"]
                }
                covered = {s.element_id for b in blocks for s in [*b.source_spans, *b.hierarchy]}
                all_spans = [s for b in blocks for s in [*b.source_spans, *b.hierarchy]]
                required_chars = sum(max(1, len(elements[e].text)) for e in required)
                covered_chars = 0
                for eid in required:
                    positions = {
                        i for s in all_spans if s.element_id == eid for i in range(s.start, s.end)
                    }
                    covered_chars += len(positions) if elements[eid].text else int(eid in covered)
                missing = required - covered
                noise = covered - required
                results.append(
                    {
                        "case": case["id"],
                        "required_source_ids": sorted(map(str, required)),
                        "covered_source_ids": sorted(map(str, covered)),
                        "evidence_coverage_at_budget": len(required & covered) / len(required),
                        "required_character_coverage": covered_chars / required_chars,
                        "added_noise_source_count": len(noise),
                        "noise_ratio": len(noise) / len(covered) if covered else 0,
                        "tokens": sum(b.token_count for b in blocks),
                        "blocks": len(blocks),
                        "expansions": sum(b.expansion_reason != "RERANKED_ANCHOR" for b in blocks),
                        "parent_expansion": any(
                            b.expansion_reason == "PARENT_EXPANSION" for b in blocks
                        ),
                        "neighbour_expansion": any(
                            b.expansion_reason.endswith("SIBLING") for b in blocks
                        ),
                        "duplicates_removed": duplicates,
                        "warnings": warnings,
                        "failures": (
                            ["CONTEXT_MISSING_REQUIRED_SOURCE"]
                            if missing or covered_chars < required_chars
                            else []
                        )
                        + (["CONTEXT_ADDED_NOISE"] if noise else []),
                        "blocks_detail": [b.model_dump(mode="json") for b in blocks],
                    }
                )
            measured = [r for r in results if "evidence_coverage_at_budget" in r]
            report["policies"][f"neighbours-{neighbours}-budget-{budget}"] = {
                "mean_coverage": mean(r["evidence_coverage_at_budget"] for r in measured),
                "mean_character_coverage": mean(r["required_character_coverage"] for r in measured),
                "mean_noise_ratio": mean(r["noise_ratio"] for r in measured),
                "mean_tokens": mean(r["tokens"] for r in measured),
                "parent_rate": mean(r["parent_expansion"] for r in measured),
                "neighbour_rate": mean(r["neighbour_expansion"] for r in measured),
                "cases": results,
            }
    return report
