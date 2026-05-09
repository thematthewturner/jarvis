# Jarvis Brain — Wiki Log

> Chronological record of all wiki actions. Append-only.
> Format: `## [YYYY-MM-DD] action | subject`
> Actions: ingest, update, query, lint, create, archive, delete
> Rotate when this file exceeds 500 entries: rename to log-YYYY.md, start fresh.

## [2026-04-29] create | Wiki initialized
- Owner: Matt Turner
- Domain: Healthcare AI, metabolic health, ML/AI research, data strategy, personal finance, family & faith
- Structure created: SCHEMA.md, index.md, log.md, raw/{articles,papers,transcripts,assets}, entities/, concepts/, comparisons/, queries/
- Path: /opt/data/jarvis/wiki
- Ready for first ingests

## [2026-05-03] update | LLM Wiki full enablement health pass
- Confirmed active wiki path should be `/opt/data/jarvis/wiki`
- Added missing graph anchor pages so existing wikilinks resolve:
  - concepts/llm.md
  - concepts/metabolic-health.md
  - concepts/cardiovascular-health.md
  - concepts/healthcare-ai-clinical-decision-support.md
  - concepts/andrej-karpathy.md
  - concepts/deep-work-framework.md
  - concepts/finances/genworth-insurance.md
  - concepts/finances/carolina-park-hoa.md
  - concepts/finances/mount-pleasant-waterworks.md
- Updated index.md total pages to 23
- Kept nightly arXiv cron disabled to preserve the daily-digest-only notification policy

## [2026-05-01] ingest | Nightly arXiv crawl — 4 papers ingested
- Searched 7 arXiv queries (healthcare AI, LLM/EHR, longevity/metabolic, cardiovascular/lipid ML, AI agents, data governance, cognitive load)
- 32 unique candidates retrieved, 4 kept after relevance + signal filtering
- Note: longevity/metabolic, cardiovascular/lipid ML, data governance, cognitive load queries returned no domain-relevant papers this cycle
- Raw papers saved:
  - raw/papers/2604.28178-llm-clinical-graph-eeg-seizure.md
  - raw/papers/2604.28179-ct-gaussian-splatting-bronchoscopy.md
  - raw/papers/2604.27967-structgp-ehr-clinical-forecasting.md
  - raw/papers/2604.28182-exploration-hacking-llm-rl-resistance.md
- Concept pages created:
  - concepts/llm-graph-eeg-seizure-detection.md (Healthcare AI / LLM)
  - concepts/ct-gaussian-splatting-bronchoscopy.md (Healthcare AI / Clinical Imaging)
  - concepts/ehr-clinical-forecasting-structgp.md (Healthcare AI / Clinical Data)
  - concepts/exploration-hacking-llm-rl-resistance.md (AI Safety / LLM)
- index.md updated: 4 new entries, total pages → 9
- Searched 7 arXiv queries (healthcare AI, LLM/EHR, longevity/metabolic, cardiovascular/lipid ML, AI agents, data governance, cognitive load)
- 56 candidate papers retrieved, 4 kept after relevance + signal filtering
- Raw papers saved:
  - raw/papers/2604.26880-archehr-qa-llm-pipeline.md
  - raw/papers/2604.26904-clawgym-agent-framework.md
  - raw/papers/2604.26803-pm-ekf-energy-expenditure-wearable.md
  - raw/papers/2604.26951-tide-diffusion-llm-distillation.md
- Concept pages created:
  - concepts/llm-clinical-qa-ehr.md (Healthcare AI / LLM)
  - concepts/wearable-energy-expenditure-physiological-models.md (Metabolic/Longevity)
  - concepts/ai-agents-claw-workspace-frameworks.md (AI Agents)
  - concepts/diffusion-llms-architecture-distillation.md (ML Research)
- index.md updated: 4 new entries, total pages → 5
- Note: cardiovascular/lipid ML query returned no domain-relevant papers this cycle

## [2026-05-02] ingest | Nightly arXiv crawl — quiet night
- Searched 7 arXiv queries (healthcare AI, LLM/EHR, longevity/metabolic, cardiovascular/lipid ML, AI agents, data governance, cognitive load)
- All results dated 2026-04-30 — arXiv has not yet processed 2026-05-01 submissions at 2:00 AM UTC run time
- All April 30 papers were already evaluated/ingested in the 2026-05-01 crawl
- No new high-signal papers found; wiki unchanged

## [2026-04-29] ingest | Pacific life 2026.pdf
- Source: Pacific Life Insurance — Notice of Premium Due, Jan 12 2026
- Raw: raw/documents/2026-04-29-insurance-pacific-life-2026.md
- Created: concepts/finances/pacific-life-insurance.md
- Key data: Policy 2L11735180, annual premium $820.15, due Feb 8 2026, agent Jeffrey Zander


- Source: Charleston County Notice of Classification, Appraisal & Assessment 2025
- Raw: raw/documents/2026-04-29-bills-property-tax-2025.md
- Created: concepts/finances/property-tax-1522-old-rivers-gate.md
- Key data: Parcel 5980300487, capped value $686,320, market value $1,119,500, taxable assessment $27,450

- Owner: Matt Turner
- Domain: Healthcare AI, metabolic health, ML/AI research, data strategy, personal finance, family & faith
- Structure created: SCHEMA.md, index.md, log.md, raw/{articles,papers,transcripts,assets}, entities/, concepts/, comparisons/, queries/
- Path: /opt/data/jarvis/wiki
- Ready for first ingests
