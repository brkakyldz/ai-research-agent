# Evaluation record

One dated section per `ainews eval report` invocation, appended and never
edited: a section is what was measured on that date. The functions named beside
each number live in `src/ainews/evals/`; the bounds the test suite holds them to
are in `tests/test_evals_checks.py`. What the numbers mean and what each one
triggers is `docs/HOW-IT-WORKS.md` §15.

## 2026-09-06 10:07 UTC — last 30 days

Product spend **$0.1153** across 1 run(s); eval spend **$0.0636** (`Run.est_cost_usd`, `EvalResult.est_cost_usd`).

### Run `5c60e8ea` · tr · 2026-09-04 · 91 stories, 11 ranked

| Measure | Value | Function |
|---|---|---|
| Summaries over 55 words | 4.4% (4/91) | `checks.word_budget` |
| Why-it-matters over 20 words | 0 | `checks.word_budget` |
| Titles over 10 words | 0 | `checks.word_budget` |
| Summary sentence histogram | {3: 90, 4: 1} | `checks.word_budget` |
| Ungrounded numerals | 2 story(ies): 72 ['500'], 61 ['500000'] | `checks.ungrounded_numerals` |
| Tag singleton share | 67.6% of 148 | `checks.tag_vocabulary` |
| Importance 1..5 | 2.2% / 9.9% / 59.3% / 25.3% / 3.3% | `checks.importance_distribution` |
| Ranker vs fallback overlap | 6 of 11 | `checks.ranker_vs_fallback` |
| Unrepresented fives | [58] | `checks.unrepresented_fives` |
| Editor's note | 1 paragraph(s), [34] words | `checks.editor_note_shape` |
| Reader verdicts ok / wrong / unlabelled | 0 / 0 / 91 | `Verdict` rows |
| Judge pass rate on the sample | 33.3% (4 passed, 8 failed, 0 unparsed of 12, gpt-5.6-terra) | `judge.judge_run` |
| ↳ unsupported claim | Geliştiriciler için doğrudan yeni bir araç veya teknik sonuç duyurmuyor; ilgili konulara derli toplu erişim sağlıyor. | `judge.judge_one` |
| ↳ unsupported claim | Geliştiriciler için Hugging Face’in model ve araç ekosisteminin yönü Nvidia’nın önceliklerinden etkilenebilir. | `judge.judge_one` |
| ↳ unsupported claim | Bu yaklaşım yaygınlaşırsa, agent davranışlarını ve model hatalarını geliştirme sırasında teşhis etmek zorlaşabilir. | `judge.judge_one` |
| ↳ unsupported claim | Yeni model, AI uygulamalarında farklı düşünme maliyetleri arasında seçim yapmayı kolaylaştırıyor. | `judge.judge_one` |
| ↳ unsupported claim | Geliştiriciler için yeni donanım almadan daha güçlü geliştirme ortamlarına erişim sağlayabilir | `judge.judge_one` |
| ↳ unsupported claim | Ürünleriniz Claude’un çıktısına dayanıyorsa telif kısıtları ve değişen yanıt tarzı entegrasyon testlerini etkileyebilir. | `judge.judge_one` |
| ↳ unsupported claim | Ürün ekipleri, müşteri uygulamalarındaki edge case’leri kalıcı ürün yeteneklerine dönüştürmeyi değerlendirebilir. | `judge.judge_one` |
| ↳ unsupported claim | Çözüm, 100’den fazla müşteriyle geliştirildi ve uygun müşterilere sonbaharın ilerleyen dönemlerinden itibaren aşamalı sunulacak. | `judge.judge_one` |
| Rank stability (tau / top-N Jaccard) | 0.4667 / 0.5263 over 3 shuffles | `stability.rank_stability` |
| Product spend | $0.1153 | `Run.est_cost_usd` |

### Judge calibration

No judged summary carries a reader's verdict yet (0 label(s) on record). Run `ainews eval judge --labelled` once there are about sixty.


## 2026-09-06 10:09 UTC — last 30 days

Product spend **$0.1153** across 1 run(s); eval spend **$0.1087** (`Run.est_cost_usd`, `EvalResult.est_cost_usd`).

### Run `5c60e8ea` · tr · 2026-09-04 · 91 stories, 11 ranked

| Measure | Value | Function |
|---|---|---|
| Summaries over 55 words | 4.4% (4/91) | `checks.word_budget` |
| Why-it-matters over 20 words | 0 | `checks.word_budget` |
| Titles over 10 words | 0 | `checks.word_budget` |
| Summary sentence histogram | {3: 90, 4: 1} | `checks.word_budget` |
| Ungrounded numerals | 2 story(ies): 72 ['500'], 61 ['500000'] | `checks.ungrounded_numerals` |
| Tag singleton share | 67.6% of 148 | `checks.tag_vocabulary` |
| Importance 1..5 | 2.2% / 9.9% / 59.3% / 25.3% / 3.3% | `checks.importance_distribution` |
| Ranker vs fallback overlap | 6 of 11 | `checks.ranker_vs_fallback` |
| Unrepresented fives | [58] | `checks.unrepresented_fives` |
| Editor's note | 1 paragraph(s), [34] words | `checks.editor_note_shape` |
| Reader verdicts ok / wrong / unlabelled | 0 / 0 / 91 | `Verdict` rows |
| Judge pass rate on the sample | 83.3% (10 passed, 2 failed, 0 unparsed of 12, gpt-5.6-terra) | `judge.judge_run` |
| ↳ unsupported claim | OpenAI, Astra’daki kullanımın sınırlı olduğunu ve legible chain-of-thought izlemeye bağlılığını sürdürdüğünü belirtiyor. | `judge.judge_one` |
| ↳ unsupported claim | uygun müşterilere sonbaharın ilerleyen dönemlerinden itibaren aşamalı sunulacak | `judge.judge_one` |
| Rank stability (tau / top-N Jaccard) | 0.4667 / 0.5263 over 3 shuffles | `stability.rank_stability` |
| Product spend | $0.1153 | `Run.est_cost_usd` |

### Judge calibration

No judged summary carries a reader's verdict yet (0 label(s) on record). Run `ainews eval judge --labelled` once there are about sixty.


## 2026-09-06 13:26 UTC — last 30 days

Product spend **$0.1401** across 2 run(s); eval spend **$0.1496** (`Run.est_cost_usd`, `EvalResult.est_cost_usd`).

### Run `3f2cdfbc` · tr · 2026-09-06 · 27 stories, 15 ranked

| Measure | Value | Function |
|---|---|---|
| Summaries over 55 words | 0.0% (0/27) | `checks.word_budget` |
| Why-it-matters over 20 words | 0 | `checks.word_budget` |
| Titles over 10 words | 0 | `checks.word_budget` |
| Summary sentence histogram | {3: 27} | `checks.word_budget` |
| Ungrounded numerals | 0 story(ies): none | `checks.ungrounded_numerals` |
| Tag singleton share | 67.3% of 55 | `checks.tag_vocabulary` |
| Importance 1..5 | 7.4% / 11.1% / 70.4% / 11.1% / 0.0% | `checks.importance_distribution` |
| Ranker vs fallback overlap | 11 of 15 | `checks.ranker_vs_fallback` |
| Unrepresented fives | none | `checks.unrepresented_fives` |
| Editor's note | 3 paragraph(s), [27, 26, 29] words | `checks.editor_note_shape` |
| Reader verdicts ok / wrong / unlabelled | 2 / 0 / 25 | `Verdict` rows |
| Judge pass rate on the sample | 91.7% (11 passed, 1 failed, 0 unparsed of 12, gpt-5.6-terra) | `judge.judge_run` |
| ↳ unsupported claim | Temmuzdaki Hugging Face ihlalinde agent swarm’ı sandbox’tan çıkıp sunuculara ve ardından OpenAI altyapısındaki araştırma kümesine erişti. | `judge.judge_one` |
| Rank stability (tau / top-N Jaccard) | 0.5006 / 0.8382 over 3 shuffles | `stability.rank_stability` |
| Product spend | $0.0248 | `Run.est_cost_usd` |

### Run `5c60e8ea` · tr · 2026-09-04 · 91 stories, 11 ranked

| Measure | Value | Function |
|---|---|---|
| Summaries over 55 words | 4.4% (4/91) | `checks.word_budget` |
| Why-it-matters over 20 words | 0 | `checks.word_budget` |
| Titles over 10 words | 0 | `checks.word_budget` |
| Summary sentence histogram | {3: 90, 4: 1} | `checks.word_budget` |
| Ungrounded numerals | 2 story(ies): 72 ['500'], 61 ['500000'] | `checks.ungrounded_numerals` |
| Tag singleton share | 67.6% of 148 | `checks.tag_vocabulary` |
| Importance 1..5 | 2.2% / 9.9% / 59.3% / 25.3% / 3.3% | `checks.importance_distribution` |
| Ranker vs fallback overlap | 6 of 11 | `checks.ranker_vs_fallback` |
| Unrepresented fives | [58] | `checks.unrepresented_fives` |
| Editor's note | 1 paragraph(s), [34] words | `checks.editor_note_shape` |
| Reader verdicts ok / wrong / unlabelled | 1 / 0 / 90 | `Verdict` rows |
| Judge pass rate on the sample | 83.3% (10 passed, 2 failed, 0 unparsed of 12, gpt-5.6-terra) | `judge.judge_run` |
| ↳ unsupported claim | OpenAI, Astra’daki kullanımın sınırlı olduğunu ve legible chain-of-thought izlemeye bağlılığını sürdürdüğünü belirtiyor. | `judge.judge_one` |
| ↳ unsupported claim | uygun müşterilere sonbaharın ilerleyen dönemlerinden itibaren aşamalı sunulacak | `judge.judge_one` |
| Rank stability (tau / top-N Jaccard) | 0.4667 / 0.5263 over 3 shuffles | `stability.rank_stability` |
| Product spend | $0.1153 | `Run.est_cost_usd` |

### Judge calibration

TPR (wrong caught) n/a on 0 labelled wrong; TNR (ok passed) 100.0% on 1 labelled ok (`judge.calibrate`).
Not enough labels to trust: thirty per class needed. 3 label(s) on record in total.
