---
name: lark-slides
description: Create an editable native Slides presentation, or add or replace a slide.
---

Determine the audience, what they need to decide or do, and which sources support the
story. Plan a short deck with one claim per slide. Use short, specific titles and up to five
brief bullets per slide. Include a source title in a bullet when a claim needs attribution.
For an unfamiliar topic, ask for evidence or offer a public search confirmation.

Create the deck using lark_file_create with kind slides. Provide a JSON-encoded array of
objects with title and bullets. The tool converts the outline to an editable native deck.
To edit a specific slide, read the existing file to find its slide ID, then use lark_file_edit
with replace. To add a slide, use append. Pass one JSON object with title and bullets.
