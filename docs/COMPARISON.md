# How this project compares with commercial recruiting software

## Read this first

This is a comparison from **public information and our own measurements**. I did not run Workday, iCIMS, Greenhouse,
Lever or the resume-parsing APIs side by side with this project, and the vendors do not publish their internals, so
this is **not a head-to-head benchmark**. Vendor figures below are vendor-reported and unaudited.

The fair framing: commercial products are full hiring platforms (job posting, pipeline stages, scheduling, offers,
integrations, compliance, multi-tenant scale). This project implements **one slice**: parsing, evaluating, ranking and
querying resumes against a job. The honest question is how that slice compares.

## Architecture, side by side

| Dimension | This project | Commercial tools (public information) | Verdict |
|---|---|---|---|
| **Resume parsing method** | LLM extraction into a strict schema, validated, retried, then verified by a second model | Dedicated parsing engines and APIs (Textkernel/Sovren, Affinda, RChilli) with trained models, OCR and many languages | They are broader. Ours is more transparent about every field. |
| **Scoring method** | Hybrid: skills and experience computed in code, projects and fit judged by AI, median of 3 | Proprietary matching models (for example HiredScore in Workday, Winston Match in SmartRecruiters); internals not published | Ours is fully inspectable; theirs is trained on far more data. |
| **Explainability** | Per-component score, per-skill colour badges, evidence-backed correction log, tools shown for each chat answer | Varies by vendor, and often limited to a score or a rank | An area where a small project can genuinely lead. |
| **Grounding / anti-hallucination** | Every stored skill must appear in the resume text (137 of 137 measured); corrections need a verbatim quote | Not publicly documented | Distinctive strength. |
| **Natural-language Q&A** | Fixed, parameterised SQL tools chosen by an AI; answers cite results; never writes SQL | Some vendors now offer AI assistants and search; details vary | Comparable idea, smaller scope. |
| **Data model** | People, applications, normalized skills, analyses, verification log, per-job scoping | Requisitions, candidates, applications, pipeline stages, scorecards, communications | Theirs covers the whole lifecycle. |
| **Scale and deployment** | Single process, one PostgreSQL database, sequential pipeline | Multi-tenant cloud, distributed, queued processing | Theirs is production-grade; ours is a prototype. |
| **Integrations** | None | Job boards, HRIS, background checks, calendars, hundreds of connectors | Not comparable. |
| **Security and access control** | Email and password login, admin and recruiter roles, a private workspace per account, hashed passwords, signed and revocable sessions, login lockout, security headers (all tested) | Roles, single sign-on, audit logs, certifications | Basic but real; still no SSO, audit log or certifications. |
| **Compliance for AI screening** | Human-in-the-loop by design and explanations, but no bias audit, notices or opt-out | Some tools emphasise compliance and audits; approaches differ | The most important gap (see below). |
| **Cost** | $0 | Subscription pricing | |

## Efficiency and performance

| Measure | This project (measured, [`BENCHMARK.md`](BENCHMARK.md)) | Vendor-reported |
|---|---|---|
| Parsing time per resume | about 2.8 s (extraction plus normalisation) | about 2 s per document (RChilli, Affinda) |
| Whole pipeline per resume | 10.8 s mean (parse, verify, normalise, score, store) | Not comparable: parsers stop at extraction |
| Throughput | 5.5 resumes per minute sequential; limited by free-tier API limits | Scales with infrastructure |
| Extraction accuracy | 7/7 on identity fields, 137/137 skills grounded, on 8 resumes | High figures, self-reported, on large private corpora |
| Repeatability | 70% of the score exactly repeatable; the AI 30% varies by 1.8 points on average (worst 6.0) | Rarely published |

**Verdict on performance:** parsing speed is in the same range as commercial parsers' reported figures, but that is
because we call fast hosted models, not because of a faster design. We do extra work (verification, scoring,
explanations) in about 10 s. We cannot honestly claim accuracy parity: 8 resumes is a demonstration, not a validation,
and commercial parsers handle scanned files, dozens of languages and hundreds of fields that we do not.

## The compliance gap deserves its own section

Automated screening tools are regulated. From public sources:

- **New York City Local Law 144** requires an independent bias audit of automated employment decision tools, published
  results, candidate notice in advance, and offers penalties per day of violation.
- **The EU AI Act** classifies recruitment and candidate-evaluation AI as **high risk**, with requirements for risk
  assessment, technical documentation, bias testing, human oversight, transparency to candidates and continuous
  monitoring. One summary gives 2 December 2027 as the start date for these obligations.
- Third-party commentary describes vendors taking different stances, from cautious AI use with audits to declining
  algorithmic screening altogether.

This project reads names and contact details, has no bias monitoring, and has no candidate notice or opt-out. **It is a
prototype for a technical assessment and must not make hiring decisions on its own.** A human recruiter decides, and the
UI is built to support that (every score is decomposed and explained).

## Where this project is strong

1. **Transparency.** Every score, skill badge, correction and chat answer can be traced to its cause.
2. **Grounded extraction.** Nothing enters the database that is not in the resume text, and corrections require evidence.
3. **Honest failure handling.** Bad files, AI outages and duplicate resumes each produce a clear, recoverable state.
4. **Multi-job design and a Q&A layer** that reuses safe, fixed queries.
5. **Zero cost and small enough to read in an afternoon.**

## Gaps, ordered by what would matter first in production

1. **Validation at scale.** A labelled set of hundreds of resumes to measure real accuracy, including messy and multi-language ones.
2. **OCR** for scanned resumes (the most common real-world failure).
3. **Bias and fairness tooling:** adverse-impact monitoring, an anonymised-screening mode, candidate notice and consent, retention and deletion.
4. **Single sign-on, an audit log and security certifications** (basic login, roles and per-account privacy now exist).
5. **Background queue and worker pool**, a connection pool, and a paid API tier for real throughput.
6. **Integrations** (job boards, calendars, HRIS) and pipeline stages.

## Sources

- Parsing speed and vendor landscape (vendor-reported): [Best Resume Parser Tools (Peoplebox)](https://www.peoplebox.ai/blog/best-resume-parser/), [Top 5 CV and Resume Parsers in 2026 (Airparser)](https://airparser.com/blog/top-cv-and-resume-parsers/), [Textkernel Sovren](https://www.textkernel.com/sovren/)
- ATS AI features and positioning: [Lever: Greenhouse alternatives](https://www.lever.co/alternative/greenhouse-alternatives), [Integral Recruiting Design: AI candidate screening and iCIMS](https://integralrecruiting.com/ai-candidate-screening-how-does-icims-compare/), [OVI: the recruiter AI gap](https://www.ovi-me.com/blog/recruiter-ai-gap-icims-greenhouse-workday-dont-screen-candidates)
- NYC Local Law 144: [NYC rules](https://rules.cityofnewyork.us/rule/automated-employment-decision-tools-2/), [Deloitte](https://www.deloitte.com/us/en/services/audit-assurance/articles/nyc-local-law-144-algorithmic-bias.html)
- EU AI Act and hiring: [artificialintelligenceact.eu](https://artificialintelligenceact.eu/what-the-act-means-for-staffing-businesses/), [European Commission](https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai)

Several of these are commercial or third-party blogs, so treat their claims as marketing or commentary, not as audited fact.
