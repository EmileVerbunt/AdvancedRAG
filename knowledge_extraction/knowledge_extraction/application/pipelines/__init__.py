"""Workflow stages, ordered to mirror the pipeline run.

Layout reflects the execution order used by the CLI / Orchestrator:

    stage_1_chunking/                  - section-aware semantic chunking (isolated)
    stage_2a_extraction_governed.py    - ontology-validated extraction (default)
    stage_2b_extraction_discovery.py   - alternative: discovery extraction
    stage_3_semantic_clustering.py     - cluster discovery findings (discovery path)
    stage_4_ontology_proposal.py       - propose ontology updates  (discovery path)
    stage_5_graph.py                   - graph build + GraphML/JSON-LD/Cypher export
    orchestrator.py                    - cross-cutting stage runner (not a stage)
"""
