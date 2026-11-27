---
name: lark-slides
description: Create an editable native Slides presentation, or add or replace a slide.
---

Determine the audience, what they need to decide or do, and which sources support the
story. Plan one to six slides with titles up to 72 characters. A text slide takes one to
five bullets of up to 100 characters each. Include a source title when a claim needs attribution.
For an unfamiliar topic, ask for evidence or offer a public search confirmation.

Create the deck using lark_file_create with kind slides. Provide a JSON-encoded array.
Use `{"title":"Decision","bullets":["Start with a pilot."]}` for text. For side-by-side
bars, use a native grouped column chart and put both series in the same units.

```json
{"title":"Share by region","chart":{"categories":["North","South"],"series":[{"name":"Orders (%)","values":[40,60]},{"name":"Sales (%)","values":[55,45]}]}}
```

For grouped columns, use two to eight category labels and one or two series.
For a 100% stacked bar, set `type` to `percent_stacked_bar`. Each bar is one
category, and each segment is one series. For a failure mix by channel, put
channels in `categories` and each first-failure reason in a separate series.
Pass the count per channel as each value. The chart normalizes each bar to
100%. Use one to eight categories, two to twelve series, nonnegative values,
and a positive total per bar.

```json
{"title":"Failure mix by channel","chart":{"type":"percent_stacked_bar","categories":["Direct","Partner"],"series":[{"name":"Low usage","values":[20,30]},{"name":"Recent rejection","values":[10,12]},{"name":"Eligible","values":[70,58]}]}}
```

Keep category labels unique and at most 24 characters, without commas or line
breaks. Use unique series names of at most 40 characters and one finite number
per category. Use verified figures and identify the source and date. When creating
a second format for the same analysis, reuse the verified counts and filters.
When creation returns `verified: true`, its readback has already succeeded.
If Lark refuses the write, use the tool's note. A deck with a failed setup step may need
operator inspection before another create attempt.
To edit a specific slide, read the existing file to find its slide ID, then use lark_file_edit
with replace. To add a slide, use append. Pass one JSON slide object.
