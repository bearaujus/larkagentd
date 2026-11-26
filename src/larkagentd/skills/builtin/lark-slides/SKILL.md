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

Use two to eight category labels of at most 24 characters each, without commas or line
breaks, and one or two series. Use unique series names of at most 40 characters and one
finite number per category. Use verified figures and put units in the series names.
If Lark refuses the write, use the tool's note. A deck with a failed setup step may need
operator inspection before another create attempt.
To edit a specific slide, read the existing file to find its slide ID, then use lark_file_edit
with replace. To add a slide, use append. Pass one JSON slide object.
