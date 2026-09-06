You are checking a news summary against the article it was written from.

Below is the article text the summariser was shown, then the summary and a
"why it matters" line written from it. The summary may be in Turkish or
English; judge the claims, not the language.

One question: **is every factual claim in the summary and the why-it-matters
line supported by the article text?** A claim is supported if the text states
it or it follows directly from what the text states. A number the text does
not give, a name it does not mention, a date, a quote, a cause or a consequence
the text does not state - each of these is unsupported, even if it happens to
be true. A rounded or converted figure counts as a figure the text did not give.

Style, length, importance, tone and translation quality are not the question.
A summary that leaves things out is not failed for leaving them out.

Answer `passed: true` if every claim is supported. Otherwise `passed: false`,
and put the **first** unsupported claim in `unsupported_claim`, quoted in the
summary's own words. Leave `unsupported_claim` empty when it passes.

---
Source: {source}
Title: {title}

Article text:
{body}

---
Summary:
{summary}

Why it matters:
{why_it_matters}
