---
name: lark-replies
description: Write a warm group reply and format it for a Lark rich-text post.
---

Answer the question in the first sentence. Match the language and level of formality in the thread. A greeting can be a single friendly sentence. Use a verified display name when it helps distinguish speakers. Use “you” when the requester’s name is unavailable. The Gateway mentions the requester on the first answer message. Leave notification markup to it.

Choose formatting for the content:

- Use **bold** for a decision or a short label. Italics can mark a brief aside.
- Use inline code for a command, filename, field, or literal value.
- Put multiline code in a fenced block with a language hint. Leave a blank line after the closing fence.
- Use bullets for independent items and a numbered list for steps that must happen in order.
- Use a short blockquote for an excerpt. Link the source with descriptive Markdown link text when the tool supplies a verified URL.
- For comparisons, use a compact table only when the columns stay readable on mobile. Use the native preview for structured tool results when available.
- Keep the answer understandable when Lark falls back to plain text. Avoid decorative headings on a one-line reply.

Example for a failed job:

**The retry finished successfully.** You can check the latest run with:

```sh
make status
```

[Lark rich-post syntax](https://open.larksuite.com/document/uAjLw4CM/ukTMukTMukTM/im-v1/message/create_json)
