from quoth.chunker import OVERLAP, WINDOW, chunk_markdown, parse_frontmatter


def test_frontmatter_metadata_not_in_chunks():
    md = "---\ntags: [a, b]\ncreated: 2026-01-01\nupdated: \"2026-02-02\"\nstatus: draft\n---\n# T\n\nbody\n"
    meta, chunks = chunk_markdown(md)
    assert meta == {"tags": ["a", "b"], "created": "2026-01-01", "updated": "2026-02-02", "status": "draft"}
    assert all("status:" not in c.text for c in chunks)
    assert chunks[0].line_start == 7 and chunks[0].heading_path == "T"


def test_frontmatter_block_list():
    meta, start = parse_frontmatter(["---", "tags:", "  - практика", "  - осознанность", "---", "x"])
    assert meta == {"tags": ["практика", "осознанность"]} and start == 5


def test_frontmatter_keeps_every_top_level_key():
    meta, _ = parse_frontmatter(["---", "date created: 2026-01-01", "Дата: 2026-02-02",
                                 '"quoted key": v', "source: https://x.example/a:b", "---"])
    assert meta == {"date created": "2026-01-01", "Дата": "2026-02-02", "quoted key": "v",
                    "source": "https://x.example/a:b"}


def test_heading_paths_and_line_locators():
    md = "intro line\n\n# A\n\ntext a\n\n## B\n\ntext b\n\n### C\ntext c\n\n## D\ntext d\n# E\ntext e\n"
    _, chunks = chunk_markdown(md)
    got = [(c.heading_path, c.line_start, c.line_end) for c in chunks]
    assert got == [("", 1, 1), ("A", 3, 5), ("A > B", 7, 9), ("A > B > C", 11, 12),
                   ("A > D", 14, 15), ("E", 16, 17)]
    lines = md.split("\n")
    for c in chunks:
        assert c.text == "\n".join(lines[c.line_start - 1:c.line_end])


def test_headings_inside_code_fence_ignored():
    md = "# Real\n```\n# not a heading\n```\nafter\n"
    _, chunks = chunk_markdown(md)
    assert len(chunks) == 1 and chunks[0].heading_path == "Real"


def test_long_section_windows_with_overlap():
    body = "\n".join(f"строка {i:04d} " + "x" * 90 for i in range(60))
    md = "# Long\n" + body + "\n"
    _, chunks = chunk_markdown(md)
    assert len(chunks) > 3
    for c in chunks:
        assert len(c.text) <= WINDOW
        assert c.heading_path == "Long"
    for a, b in zip(chunks, chunks[1:]):
        assert b.line_start <= a.line_end  # overlapping line ranges
        overlap = "\n".join(sorted(set(a.text.split("\n")) & set(b.text.split("\n"))))
        assert 0 < len(overlap) <= OVERLAP + 120
    assert chunks[-1].line_end == 61


def test_single_huge_line_split():
    line = "很长的中文段落没有空格也没有句号" * 200      # no space, no sentence end: hard cuts
    _, chunks = chunk_markdown(line)
    assert len(chunks) >= 3 and all(len(c.text) <= WINDOW for c in chunks)
    assert all(c.line_start == c.line_end == 1 for c in chunks)
    _, chunks = chunk_markdown("这是第一句。" * 400)
    assert len(chunks) >= 3 and all(c.text.endswith("。") for c in chunks)


def _random_note(rng):
    words = ["garden", "tomato", "basil", "чай", "заметка", "plan", "x" * 40]
    lines = ["# Note"]
    for _ in range(rng.randint(1, 40)):
        kind = rng.random()
        if kind < 0.1:
            lines.append("## Part " + str(rng.randint(1, 9)))
        elif kind < 0.2:
            lines.append("")
        else:
            sentences = rng.choice([1, 2, 5, 15, 30])
            lines.append(" ".join(
                " ".join(rng.choice(words) for _ in range(rng.randint(3, 12))) + "."
                for _ in range(sentences)))
    return "\n".join(lines) + "\n"


def test_windows_always_bring_new_lines_and_cover_every_line():
    import random
    rng = random.Random(11)
    for _ in range(300):
        md = _random_note(rng)
        lines = md.split("\n")
        _, chunks = chunk_markdown(md)
        covered = set()
        for c in chunks:
            assert len(c.text) <= WINDOW
            covered.update(range(c.line_start, c.line_end + 1))
            if c.line_start < c.line_end:
                assert c.text == "\n".join(lines[c.line_start - 1:c.line_end])
        for a, b in zip(chunks, chunks[1:]):
            same_long_line = a.line_start == a.line_end == b.line_start == b.line_end
            if a.heading_path == b.heading_path and not same_long_line:
                assert b.line_end > a.line_end
        content = {i + 1 for i, t in enumerate(lines) if t.strip() and not t.startswith("#")}
        assert content <= covered


def _words(n):
    return ("word " * n)[:n]


def test_short_lines_before_a_long_line_are_not_repeated_alone():
    md = "# T\n" + _words(500) + "\n" + _words(500) + "\n" + _words(100) + "\n" + _words(1150) + "\n"
    _, chunks = chunk_markdown(md)
    assert [(c.line_start, c.line_end) for c in chunks] == [(1, 4), (5, 5)]
    md = "# T\n" + _words(500) + "\n" + _words(500) + "\n" + _words(100) + "\n" + _words(3000) + "\n"
    _, chunks = chunk_markdown(md)
    assert [(c.line_start, c.line_end) for c in chunks] == [(1, 4), (5, 5), (5, 5), (5, 5)]


def test_long_paragraph_splits_at_sentences_with_bounded_overlap():
    para = " ".join(f"Sentence {i} is about the balcony garden and its tomatoes." for i in range(60))
    _, chunks = chunk_markdown("# Garden\n" + para + "\n")
    assert len(chunks) >= 3  # and the heading line is not a chunk of its own
    assert all(c.line_start == c.line_end == 2 and c.heading_path == "Garden" for c in chunks)
    starts = [para.index(c.text) for c in chunks]
    for c in chunks:
        assert len(c.text) <= WINDOW and c.text.startswith("Sentence ") and c.text.endswith(".")
    for (a, sa), sb in zip(zip(chunks, starts), starts[1:]):
        assert sa < sb < sa + len(a.text) and sa + len(a.text) - sb <= OVERLAP
    assert starts[0] == 0 and para.endswith(chunks[-1].text)


def test_words_are_not_cut_in_long_lines_without_sentences():
    line = " ".join(f"word{i:04d}" for i in range(400))
    _, chunks = chunk_markdown(line)
    assert len(chunks) >= 3
    for c in chunks:
        assert set(c.text.split()) <= set(line.split()) and len(c.text) <= WINDOW


def test_encoded_data_is_not_a_chunk_and_not_embedded():
    import base64
    import random
    from quoth.chunker import passage_text

    rng = random.Random(3)
    blob = base64.b64encode(bytes(rng.randrange(256) for _ in range(3000))).decode()
    drawing = "\n".join(blob[i:i + 256] for i in range(0, len(blob), 256))
    md = ("---\nexcalidraw-plugin: parsed\n---\n# Text Elements\nKitchen layout: sink by the window.\n\n"
          "%%\n# Drawing\n```compressed-json\n" + drawing + "\n```\n%%\n")
    _, chunks = chunk_markdown(md)
    assert [c.heading_path for c in chunks] == ["Text Elements"]   # the drawing is data
    words = "The tomatoes on the balcony grew well this year after the new soil mix. " * 6
    image = words + "![](data:image/png;base64," + blob[:300] + ") Taken in May."
    _, chunks = chunk_markdown("# Balcony\n" + image + "\n")
    assert len(chunks) == 1 and blob[:300] in chunks[0].text  # stored as written
    embedded = passage_text("Balcony.md", "Balcony", chunks[0].text)
    assert blob[:200] not in embedded and "Taken in May." in embedded and words in embedded
    _, chunks = chunk_markdown("# Photo\n![](data:image/png;base64," + blob + ")\n")
    assert chunks == []                                         # an image alone is data
    prose = "这是一个很长的中文段落，没有空格。" * 100            # CJK prose is not data
    _, chunks = chunk_markdown(prose)
    assert chunks and "".join(c.text for c in chunks).count("中文") >= 100
