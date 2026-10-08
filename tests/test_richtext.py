from sfchatter.richtext import markdown_to_segments, split_segments, stored_length


def B(kind):
    return {"type": "MarkupBegin", "markupType": kind}


def E(kind):
    return {"type": "MarkupEnd", "markupType": kind}


def T(text):
    return {"type": "Text", "text": text}


def test_heading_becomes_bold_paragraph_and_inline_bold_kept():
    assert markdown_to_segments("## Conclusion\n**Option A** wins") == [
        B("Paragraph"), B("Bold"), T("Conclusion"), E("Bold"), E("Paragraph"),
        B("Paragraph"), B("Bold"), T("Option A"), E("Bold"), T(" wins"), E("Paragraph"),
    ]


def test_lists_and_code_fence():
    md = "- First\n- Second\n\n1. Step A\n2. Step B\n\n```bash\nls -la\npwd\n```"
    assert markdown_to_segments(md) == [
        B("UnorderedList"), B("ListItem"), T("First"), E("ListItem"), B("ListItem"), T("Second"), E("ListItem"), E("UnorderedList"),
        B("OrderedList"), B("ListItem"), T("Step A"), E("ListItem"), B("ListItem"), T("Step B"), E("ListItem"), E("OrderedList"),
        B("Code"), T("ls -la\npwd"), E("Code"),
    ]


def test_table_rows_flatten_and_rule_dropped():
    md = "| Item | Value |\n|---|---|\n| A | 1 |"
    assert markdown_to_segments(md) == [
        B("Paragraph"), T("Item / Value"), E("Paragraph"),
        B("Paragraph"), T("A / 1"), E("Paragraph"),
    ]


def test_link_and_inline_code_become_plain_text():
    assert markdown_to_segments("[Reference](https://example.com/x) and `make test`") == [
        B("Paragraph"), T("Reference https://example.com/x"), T(" and "), T("make test"), E("Paragraph"),
    ]


def test_snake_case_and_multiplication_are_not_italic():
    assert markdown_to_segments("file_name_here and 2 * 3 * 4") == [
        B("Paragraph"), T("file_name_here and 2 * 3 * 4"), E("Paragraph"),
    ]


def test_markups_are_balanced_for_mixed_input():
    md = "# Heading\nBody *emphasis* and **bold**\n- a\n- **b**\n```\nx\n```\n---\nEnd"
    depth = 0
    for seg in markdown_to_segments(md):
        if seg["type"] == "MarkupBegin":
            depth += 1
        elif seg["type"] == "MarkupEnd":
            depth -= 1
        assert depth >= 0
    assert depth == 0


def test_stored_length_matches_html_salesforce_stored():
    # Posted to a real org on 2026-10-09; FeedComment.CommentBody came back as
    # <p>A&amp;B &lt;x&gt; &quot;q&quot;</p><p><b>bold</b><i>it</i><code>cd</code> https://example.com/1 </p>
    # <ul><li>li</li></ul><ol><li>ol</li></ol>@<display name>日本  (168 chars with a 23-char mention)
    segments = [
        B("Paragraph"), T('A&B <x> "q"'), E("Paragraph"),
        B("Paragraph"), B("Bold"), T("bold"), E("Bold"), B("Italic"), T("it"), E("Italic"),
        B("Code"), T("cd"), E("Code"), {"type": "Link", "url": "https://example.com/1"}, E("Paragraph"),
        B("UnorderedList"), B("ListItem"), T("li"), E("ListItem"), E("UnorderedList"),
        B("OrderedList"), B("ListItem"), T("ol"), E("ListItem"), E("OrderedList"),
        T("日本"),
    ]
    assert stored_length(segments) == 168 - 23
    assert stored_length([{"type": "Mention", "id": "005000000000ABCAAA"}]) == 256
    assert stored_length([{"type": "Mention", "text": "@" + "a" * 300}]) == 301


def test_split_keeps_every_chunk_within_stored_limit():
    # A reply whose plain text fit in one comment but whose HTML did not was
    # rejected with STRING_TOO_LONG; links, markup and escapes must count.
    md = "\n\n".join(
        f"**Part {i}** A&B <tag> `code {i}` [link](https://example.com/{i})\n- item A{i}\n- item B{i}"
        for i in range(1, 161))
    chunks = split_segments(markdown_to_segments(md), 3000)
    assert len(chunks) > 1
    for chunk in chunks:
        assert stored_length(chunk) <= 3000
        depth = 0
        for seg in chunk:
            depth += {"MarkupBegin": 1, "MarkupEnd": -1}.get(seg["type"], 0)
            assert depth >= 0
        assert depth == 0
    joined = "".join(s.get("text", "") + s.get("url", "") for c in chunks for s in c)
    assert "https://example.com/160" in joined and "item B160" in joined


def test_converter_discards_trailing_empty_list_items_and_code_whitespace():
    assert markdown_to_segments("answer\n-  \n- \t") == [
        B("Paragraph"), T("answer"), E("Paragraph"),
    ]
    assert markdown_to_segments("```\nanswer\n \t\n```") == [
        B("Code"), T("answer"), E("Code"),
    ]
