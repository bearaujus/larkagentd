"""How token counts read in a Lark message: the line under an answer and the /usage report.

The text is Lark markdown: a quote, bold figures and a small icon per part. Counts are shown
the way people read them (857, 1.2K, 20K, 305K, 1.2M); the exact numbers stay in the token log.
"""

INPUT, CACHED, OUTPUT = "\u2934", "\U0001f4be", "\u2935"


def total(counts):
    """Everything the model processed: input, cached input (read and written) and output.
    The same sum the monthly quota counts."""
    return (
        counts["input_tokens"]
        + counts["cache_read_tokens"]
        + counts["cache_write_tokens"]
        + counts["output_tokens"]
    )


def short(count):
    """A count as people read it: 857, 1.2K, 20K, 305K, 1.2M (rounded, never below the unit)."""
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        tenths = (count + 50) // 100  # tenths of a thousand
        if tenths < 100:
            return f"{tenths // 10}.{tenths % 10}K".replace(".0K", "K")
        thousands = (count + 500) // 1000
        if thousands < 1000:
            return f"{thousands}K"
        count = 1_000_000  # rounds up to a whole million
    tenths = (count + 50_000) // 100_000  # tenths of a million
    return f"{tenths // 10}.{tenths % 10}M".replace(".0M", "M")


def approx(count):
    """`short`, with a leading ~ when it is rounded."""
    return f"~{short(count)}" if count >= 1000 else str(count)


def parts(counts):
    cached = counts["cache_read_tokens"] + counts["cache_write_tokens"]
    return (
        f"{INPUT} {short(counts['input_tokens'])} **input** \u00b7 "
        f"{CACHED}  {short(cached)} **cached** \u00b7 "
        f"{OUTPUT}  {short(counts['output_tokens'])} **output**"
    )


def questions(count):
    return f"**{count}** {'question' if count == 1 else 'questions'}"


def footer(used, month_total=None, quota=0):
    """The quoted line under an answer: what this request used, and, when given, the month."""
    if not total(used):
        return ""
    line = f"> Tokens used: **{approx(total(used))}** ({parts(used)})"
    if month_total is not None:
        line += f" \u00b7 this month **{short(month_total)}**"
        if quota:
            line += f" of **{short(quota)}**"
    return line


def report(month, thread=None, quota=0):
    """The /usage reply: this month, and this conversation when there is one. One quote per line."""
    lines = [
        f"> This month: **{approx(total(month))}** tokens ({parts(month)}) "
        f"\u00b7 {questions(month['turns'])}"
    ]
    if quota:
        share = min(100, round(100 * total(month) / quota))
        lines.append(f"> Allowance: **{short(quota)}** tokens \u00b7 **{share}%** used")
    if thread is not None:
        lines.append(conversation(thread, "This conversation"))
    return "\n\n".join(lines)


def conversation(thread, label):
    if not thread["turns"]:
        return f"> {label}: nothing used yet."
    return (
        f"> {label}: **{approx(total(thread))}** tokens ({parts(thread)}) "
        f"\u00b7 {questions(thread['turns'])}"
    )


def thread_report(thread):
    """The /usage reply in a group: this thread only. A person's month is their own business."""
    return conversation(thread, "This thread (everyone in it)")


def governance_report(quota, used, missing, thread=None):
    label = "FlexTier" if quota["tier"] == "flex" else "FreeTier"
    limit = quota["limit"]
    allowance = "Unlimited" if limit is None else f"{short(limit)} tokens"
    lines = [
        f"> **{label}** | **{approx(used)}** tokens used this cycle",
        f"> Allowance: **{allowance}** | Resets **{quota['resets_on']} at 00:00 UTC**",
    ]
    if missing:
        lines.append(f"> Usage unavailable for {missing} model session(s).")
    if thread is not None:
        lines.append(conversation(thread, "This conversation"))
    return "\n\n".join(lines)
