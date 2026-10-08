from sfchatter.richtext import markdown_to_segments


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
