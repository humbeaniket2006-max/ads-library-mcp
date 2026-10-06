from app.format import (
    NOTE,
    ad_row,
    dedupe,
    hoist_page,
    query_hash,
    render,
    sanitize_text,
    sign_cursor,
    strip_token,
    verify_cursor,
)

KEY = "k" * 40


def test_sanitize_strips_html_control_zero_width_bidi():
    raw = "Hi <script>alert(1)</script><b>there</b>​‮\x00 &lt;img src=x&gt; ⁦ok⁩\n\n  end"
    out = sanitize_text(raw, 300)
    assert out == "Hi there ok end"
    for bad in ("<", ">", "​", "‮", "\x00", "⁦"):
        assert bad not in out


def test_sanitize_truncates_and_collapses_whitespace():
    out = sanitize_text("word " * 200, 50)
    assert len(out) <= 50 and out.endswith("…") and "  " not in out


def test_ad_row_compact_omits_nulls_and_dates_are_days():
    row = ad_row({"id": "1", "page_id": "9", "page_name": "P", "ad_creative_bodies": ["<i>Hello</i>"],
                  "ad_delivery_start_time": "2026-03-01T08:00:00+0000", "publisher_platforms": ["FACEBOOK", "INSTAGRAM"],
                  "ad_snapshot_url": "https://x/?access_token=EAAabcdefgh"},
                 detail="compact", include_links=False, text_max=300)
    assert row == {"id": "1", "page": "P", "page_id": "9", "start": "2026-03-01", "platforms": ["fb", "ig"],
                   "text": "Hello"}


def test_ad_row_links_only_when_asked_and_token_removed():
    ad = {"id": "1", "ad_snapshot_url": "https://www.facebook.com/ads/archive/render_ad/?id=1&access_token=EAAabcdefgh"}
    row = ad_row(ad, detail="compact", include_links=True, text_max=10)
    assert row["url"] == "https://www.facebook.com/ads/archive/render_ad/?id=1"
    assert strip_token("https://a/?access_token=EAAx&b=1") == "https://a/?b=1"


def test_full_detail_adds_spend_reach_demographics():
    row = ad_row({"id": "1", "spend": {"lower_bound": "100", "upper_bound": "199"}, "currency": "EUR",
                  "eu_total_reach": 5000,
                  "demographic_distribution": [{"percentage": "0.4", "age": "25-34", "gender": "female"}]},
                 detail="full", include_links=False, text_max=10)
    assert row["spend"] == "100-199 EUR" and row["reach"] == 5000 and row["demo"] == ["25-34 f 40%"]


def test_dedupe_same_page_same_text_caps_three_ids():
    rows = [{"id": str(i), "page_id": "9", "text": "same"} for i in range(5)]
    rows.append({"id": "x", "page_id": "8", "text": "same"})
    rows.append({"id": "y", "page_id": "9", "text": "other"})
    out = dedupe(rows)
    assert len(out) == 3
    assert out[0] == {"page_id": "9", "text": "same", "n": 5, "ids": ["0", "1", "2"]}
    assert out[1]["id"] == "x" and "n" not in out[1]


def test_hoist_page_only_when_single_page():
    rows = [{"id": "1", "page": "P", "page_id": "9"}, {"id": "2", "page": "P", "page_id": "9"}]
    assert hoist_page(rows) == {"page": "P", "page_id": "9"} and "page" not in rows[0]
    mixed = [{"id": "1", "page_id": "9"}, {"id": "2", "page_id": "8"}]
    assert hoist_page(mixed) == {}


def test_truncation_drops_rows_and_flags():
    rows = [{"id": str(i), "text": "x" * 200} for i in range(50)]
    meta = {"n": 50}
    out = render(rows, meta, 2000)
    assert len(out) <= 2000 and '"truncated":true' in out and meta["n"] < 50
    small = render(rows[:2], {"n": 2}, 2000)
    assert "truncated" not in small and NOTE in small and " " not in small.replace("x" * 200, "").replace(NOTE, "")


def test_cursor_roundtrip_tamper_and_query_binding():
    qh = query_hash({"a": 1})
    cur = sign_cursor("AFTER", qh, KEY)
    assert verify_cursor(cur, qh, KEY) == "AFTER"
    assert verify_cursor(cur, query_hash({"a": 2}), KEY) is None
    assert verify_cursor(cur, qh, "z" * 40) is None
    payload, mac = cur.split(".")
    assert verify_cursor(payload + "." + mac[:-1] + ("A" if mac[-1] != "A" else "B"), qh, KEY) is None
    assert verify_cursor("garbage", qh, KEY) is None
    assert verify_cursor("", qh, KEY) is None
    assert "AFTER" not in cur  # opaque
