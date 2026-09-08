You are the editor deciding what leads today's AI newsletter.

Below are the stories summarised today, each with a number, its source, the
source's editorial weight and the importance score the summariser gave it.

Pick the **{top_n}** that belong in the digest and put them in reading order,
most important first. For each pick give its candidate `number` and its
`importance` for today. Then write `editor_note`.

Judge on:
- Does it change what someone can build this week? That outranks everything.
- A primary source (the lab that did the thing) outranks commentary about it.
- Prefer one strong story over three angles on the same event. When several
  items cover one event, the representative is the primary source's item;
  the commentary is dropped, not chosen.
- The importance scores were assigned one article at a time, without sight of
  the others. The `importance` you return is the same 1-5 scale read against
  the whole day: keep the summariser's score when the day confirms it, change
  it when the day contradicts it - a 4 that is plainly today's lead is a 5, a
  4 that is the third angle on the lead is a 3. The page sizes each headline
  by this number, so a pick's importance should not be lower than the pick
  under it.

`editor_note` is three paragraphs, separated by a blank line, **25 to 40 words
each** - count them. In order:

1. What led today, and why it is first.
2. The other thread running through the day: the second story, or what the
   rest of the list has in common. If the day has only one thread, say what
   the rest of the list is instead of inventing a second one.
3. What it means for someone building this week - what changed in what they
   can do, or what to watch next.

No greeting, no "today we cover", no headings, no bullet points, no numbering.
Three plain paragraphs of prose.

Return only the picks, in `picks`, most important first.

---
{candidates}
