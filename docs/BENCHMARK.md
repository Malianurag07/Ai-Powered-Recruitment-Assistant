# Measured performance and accuracy

Everything here was measured on this project's own code with `python scripts/benchmark.py` (temporary database, live
free-tier APIs, a Windows laptop with 7 GB RAM and no GPU) on 2026-09-20. **Read the caveats at the end before quoting
any number.**

Test set: 8 resumes (5 real, 3 synthetic with known ground truth) against `data/sample_job_description.txt` (12 required and 3 preferred skills).

## Speed (one resume at a time, as the web UI does it)

| Stage | Mean seconds | Notes |
|---|---|---|
| Text extraction (PyMuPDF / python-docx) | 0.02 | Local, effectively free |
| AI extraction to structured JSON | 1.9 | Groq `qwen3.8-27b` |
| Verification (second AI, different model family) | 4.1 | Gemini; the slowest stage |
| Skill normalisation | 0.9 | Alias table; AI only for unknown skills |
| Scoring (3 parallel AI judgments, median) | 4.4 | 2.3 s with a single judgment |
| Database write and duplicate check | 0.04 | |
| **Whole resume** | **10.8 mean, 9.6 median, 18.7 p95** | about 5.5 resumes per minute |

Parsing alone (extraction plus normalisation) takes about 2.8 s per resume. The job description takes about 3 s to parse, once per job.

## Parallel throughput (4 workers, same 8 resumes)

| Configuration | Wall time | Result |
|---|---|---|
| Sequential (reference) | 86.6 s | 7 stored, 1 conflict (the expected same-person duplicate) |
| Parallel, first version | 30.6 s | **Only 3 of 8 succeeded.** Bursts of AI calls exhausted the free-tier rate limits. |
| Parallel, after adding a concurrency cap and exponential backoff, 3 judgments | 77.5 s | All 8 handled correctly |
| Parallel, same fix, 1 judgment (`JUDGE_SAMPLES=1`) | 55.5 s | All 8 handled correctly |

Lesson: on free tiers, throughput is limited by the providers' rate limits, not by this code (which spends 0.06 s of each
resume outside AI calls). The fix trades raw speed for reliability. Real speed-ups need a paid tier.

## Accuracy on facts that can be checked

| Check | Result |
|---|---|
| Email extracted exactly as printed on the resume | 7 of 7 |
| Name found in the resume header | 7 of 7 |
| Years of paid experience exact (including internships kept separate) | 7 of 7 |
| Every stored skill is present in the resume text | 137 of 137 (a guarantee of the verification step, not a lucky result) |
| Invented skills caught and removed by the free code check | 2 |
| Corrections proposed by the second AI and accepted (each with a verified quote) | 12 |
| Skill precision / recall on the 3 synthetic resumes | 1.00 / 1.00 |

## Score repeatability

The same stored profile was scored three times.

| | Spread (max minus min) across 5 candidates | Mean |
|---|---|---|
| Single AI judgment | 4.2, 3.7, 5.2, 0.8, 6.9 points | 4.2 |
| Median of 3, rounded to tens (shipped) | 6.0, 3.0, 0.0, 0.0, 0.0 points | 1.8 |

Three of five candidates became perfectly repeatable, and the mean noise fell by more than half. The worst case did **not**
improve (6.0 points), because a rounding boundary can still flip. Only 30% of the score comes from the AI; the other 70%
(skills and experience) is computed in code and is exactly repeatable.

## Chat latency

Mean 4.3 s, maximum 9.0 s over five questions (one plan call, one SQL query, one answer call).

## Cost

$0 for the AI: free tiers of Groq and Gemini and open-source libraries (the measurements here were taken on the earlier SQLite version).

## Caveats (important)

- **Small sample.** 8 resumes, mostly fresher resumes, one run each. These numbers show the system works and where its
  time goes; they are not a statistically meaningful accuracy claim.
- **Ground truth is limited.** Skill precision/recall was checked only on 3 simple synthetic resumes. "137 of 137
  grounded" proves no invention, not that no skill was missed on messy real resumes.
- **Times depend on the providers.** Free-tier latency and rate limits vary through the day.
- **Not comparable to vendor benchmarks.** See [`COMPARISON.md`](COMPARISON.md).

## Hybrid retrieval experiment

Measured before building the feature, to decide whether it earns its place. 7 resumes split into 43 chunks; 11 queries with
known correct candidates (5 using the resume's own words, 6 paraphrased so they share almost no words with it, for example
"recognising objects in camera images" for a resume that says "OpenCV, YOLO"). Metric: the fraction of the correct candidates
found among the top results. Embeddings: `gemini-embedding-001`.

| Method | Exact-term queries | Paraphrased queries | Overall |
|---|---|---|---|
| Keyword only (BM25) | 1.00 | 0.67 | 0.82 |
| Semantic only (embeddings) | 1.00 | 0.83 | 0.91 |
| Hybrid, equal weights | 1.00 | 0.67 | 0.82 |
| **Hybrid, semantic weighted 2:1 (shipped)** | 1.00 | **0.83** | **0.91** |
| Hybrid, semantic weighted 3:1 | 1.00 | 0.83 | 0.91 |

Findings: semantic search clearly helps on paraphrased questions; naive equal-weight fusion threw that gain away; weighting
meaning 2:1 kept the gain and keeps keyword search as a safety net (it is the fallback if embeddings are unavailable).
768-dimension vectors scored the same as 3072-dimension ones at a quarter of the storage. Backfilling 70 chunks took 3.0 s.
Live check on the real resumes: the four concept questions asked in the chat each found the right candidate and quoted the
supporting resume text. **Caveat:** 11 queries over 7 resumes is directional evidence, not a general accuracy figure.

## Self-hosted mode (`LLM_MODE=local`)

One resume (`kushal_br.pdf`) and the sample job description, every AI call routed to a local `llama3.2:3b` on a CPU-only laptop.

| | Self-hosted | Cloud (same resume) |
|---|---|---|
| Job description parsed | 26 s | about 3 s |
| Whole resume | **96 s** | 10.8 s (mean) |
| Skills extracted | Python, SQL, Power BI | the same three |
| Score / recommendation | 30.4 / Reject | 28.1 / Reject |

Nothing left the machine (a request for a cloud provider was rerouted to the local model, and embeddings correctly refused
in this mode). One resume proves the mode works end to end; it does not prove equal accuracy.

## Chat regression on the final code (throwaway database, live AI)

The 10 assessment questions plus 15 more. A first run scored 21 of 25; reading each failure showed three were overly narrow
checks in the test script (the assistant's answers were correct) and one was a real bug: the planner silently chose
"Ananya Iyer" when the user typed only "Ananya". That is now enforced in code (`keep_ambiguity`) with tests.


## Update after the QA round (2026-09-21)

Measured with `scripts/qa_live.py` on 12 generated resumes with known answers (a different, larger set than above), free-tier APIs:

| Stage | Mean seconds | AI calls | Share of time |
|---|---|---|---|
| Text extraction | 0.00 | 0 | 0% |
| AI extraction | 1.1 | 1.0 | 8% |
| Verification (second AI) | 5.8 | 1.0 | 43% |
| Skill normalisation | 0.7 | 0.6 | 5% |
| Scoring (median of 3) | 5.7 | 3.0 | 43% |
| Whole resume | **13.3** | **5.6** | 100% |

The meaning-based skill pass added later costs one more AI call per resume (about 6.6 in total). Free-tier limits were the real ceiling:
about 700 AI calls in one afternoon exhausted the Gemini quota (HTTP 429), so a full live QA run is best done once a day.

Results: 58 of 63 live cases passed before the fixes; after them the extraction and ranking checks were 24 of 24 on real AI, and the offline
suite was 43 of 43. Fairness: 12 of 12 personal-detail changes (name, gender, college, age, career break, village) moved the score by 5 points or less.
Prompt injection: 11 of 12 attacks had no effect; the twelfth (invisible keyword stuffing) raised a weak candidate from 30.9 to 86.0 and was fixed.
A pharma resume that scored 30 to 38 (skills part 0%) now scores 86 to 94 after the job prompt rewrite and the meaning-based pass.
Caveat: these are resumes written for testing, not a statistical accuracy study.
