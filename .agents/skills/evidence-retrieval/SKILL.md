---
name: evidence-retrieval
description: >-
  Extract and retrieve multimodal payment evidence (contracts, invoices, receipts, dispute letters)
  using Qwen2.5-VL extraction, BGE-M3 hybrid search, and BGE reranking.
---

# Evidence Retrieval Skill

This skill outlines how to ingest, index, and query unstructured and multimodal documentation supporting disputed transactions.

## Pipeline Architecture
1. **Document Ingestion & Checksum**:
   - Compute SHA-256 checksum of document blobs to ensure immutability and provenance.
   - Extract raw text and layout with Docling / Qwen2.5-VL.
2. **Field Extraction & Schema Validation**:
   - Mandatory extracted fields: `invoice_number`, `billed_entity`, `recipient_entity`, `amount_minor`, `currency`, `issue_date`, `payment_terms`.
   - Never assert 100% document authenticity: assign confidence scores to each field.
3. **Hybrid Indexing**:
   - Dense embeddings: `BAAI/bge-m3` across 8192 token windows.
   - Sparse lexical index: BM25 for strict identifier/reference matching (e.g. exact invoice ID, transaction reference).
4. **Cross-Encoder Reranking**:
   - Apply `BAAI/bge-reranker-v2-m3` on top 20 candidate passages to filter noise and rank top 3 evidentiary excerpts.
5. **Context Grounding**:
   - All factual assertions in fraud investigation reports must link back to specific `document_id` and page/span citations.

