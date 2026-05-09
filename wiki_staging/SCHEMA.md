# Wiki Schema — Jarvis Brain

## Domain
Matt Turner's personal intelligence layer. Covers the intersecting domains that
matter to Matt: healthcare AI & clinical data, metabolic health & longevity,
machine learning & AI research, data leadership & strategy, personal finance &
investing, family & faith, and emerging technology.

This wiki is Matt's compounding knowledge base — synthesized from papers, articles,
emails, conversations, and experiments. It grows smarter over time.

## Owner
Matt Turner — Data leader, healthcare AI, father, Christian, Kentucky fan.

## Conventions
- File names: lowercase, hyphens, no spaces (e.g., `transformer-architecture.md`)
- Every wiki page starts with YAML frontmatter (see below)
- Use `[[wikilinks]]` to link between pages (minimum 2 outbound links per page)
- When updating a page, always bump the `updated` date
- Every new page must be added to `index.md` under the correct section
- Every action must be appended to `log.md`
- **Provenance markers:** On pages synthesizing 3+ sources, append `^[raw/articles/source-file.md]`
  at the end of paragraphs whose claims come from a specific source.

## Frontmatter
```yaml
---
title: Page Title
created: YYYY-MM-DD
updated: YYYY-MM-DD
type: entity | concept | comparison | query | summary
tags: [from taxonomy below]
sources: [raw/articles/source-name.md]
confidence: high | medium | low
contested: true                        # optional — set when unresolved contradictions exist
contradictions: [other-page-slug]      # optional
---
```

## Tag Taxonomy

### Healthcare & Clinical
- `healthcare-ai` — AI applications in clinical settings
- `clinical-data` — EHR, claims, clinical data engineering
- `metrics` — healthcare KPIs, quality measures
- `medicaid` — Medicaid policy, populations, programs
- `population-health` — care management, risk stratification

### Metabolic & Longevity
- `longevity` — lifespan, healthspan, aging research
- `metabolic-health` — metabolism, insulin, glucose
- `cardiovascular` — heart health, lipids, CVD prevention
- `nutrition` — diet, fasting, food science
- `exercise` — training, VO2 max, strength

### AI & Machine Learning
- `llm` — large language models, foundation models
- `agents` — AI agents, autonomous systems, agentic frameworks
- `ml-research` — machine learning methods, experiments
- `fine-tuning` — RLHF, DPO, alignment, training
- `inference` — serving, quantization, deployment

### Data & Technology
- `data-strategy` — data leadership, governance, architecture
- `data-engineering` — pipelines, dbt, Spark, orchestration
- `analytics` — BI, metrics platforms, dashboards
- `cloud` — AWS, GCP, Azure, infrastructure

### Finance & Investing
- `investing` — stocks, ETFs, portfolio strategy
- `529` — 529 education savings
- `real-estate` — property, home ownership
- `ipo` — IPO opportunities, pre-IPO investing
- `bills` — utilities, subscriptions, recurring expenses
- `insurance` — health, home, auto, life, disability
- `statements` — bank/brokerage/account statements
- `receipts` — purchase records, warranties

### Personal
- `faith` — Christianity, spiritual growth, ministry
- `family` — Toni, Addison, Katelyn, parenting
- `leadership` — management, mentorship, career
- `learning` — books, courses, skills development

### Meta
- `comparison` — side-by-side analyses
- `timeline` — chronological records
- `open-question` — unresolved questions worth tracking
- `prediction` — forecasts and market positions

## Page Thresholds
- **Create a page** when an entity/concept appears in 2+ sources OR is central to one source
- **Add to existing page** when a source mentions something already covered
- **DON'T create a page** for passing mentions or minor details
- **Split a page** when it exceeds ~200 lines

## Entity Pages
One page per notable person, organization, product, or model. Include:
- Overview / what it is
- Key facts and dates
- Relationships ([[wikilinks]])
- Source references

## Concept Pages
One page per concept or topic. Include:
- Definition / explanation
- Current state of knowledge
- Open questions
- Related concepts ([[wikilinks]])

## Update Policy
When new info conflicts with existing content:
1. Check dates — newer sources generally supersede older
2. If genuinely contradictory, note both positions with dates and sources
3. Mark frontmatter: `contradictions: [page-name]`
4. Flag for user review
