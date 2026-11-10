import pytest

from masker import contains_raw_pii, mask_text, preprocess_email, tokens_of, unmask_text


@pytest.mark.parametrize("raw", ["ord/9988", "ORD - 9988", "ord9988", "ORD-9988", "Ord #9988", "ord_9988", "ORD / 9988"])
def test_messy_order_ids_normalize(raw):
    r = mask_text(f"Please help with {raw} today.")
    assert r.masked_text == "Please help with <ORDER_TOKEN_1> today."
    assert r.vault == {"<ORDER_TOKEN_1>": "ORD-9988"}


@pytest.mark.parametrize("raw", ["ab - 1234", "AB/1234", "ab1234", "AB-1234", "Ab - 1234", "ab_1234"])
def test_messy_part_numbers_normalize(raw):
    r = mask_text(f"Returning {raw} please.")
    assert r.masked_text == "Returning <PART_TOKEN_1> please."
    assert r.vault == {"<PART_TOKEN_1>": "AB-1234"}


def test_happy_path_sentence():
    r = mask_text("Hi, I want to return ab - 1234 from ord/9988. It arrived damaged.")
    assert r.masked_text == "Hi, I want to return <PART_TOKEN_1> from <ORDER_TOKEN_1>. It arrived damaged."
    assert r.vault == {"<PART_TOKEN_1>": "AB-1234", "<ORDER_TOKEN_1>": "ORD-9988"}
    assert not contains_raw_pii(r.masked_text)


def test_order_not_double_matched_as_part():
    r = mask_text("ORD-9988")
    assert tokens_of(r.vault, "PART") == []


def test_common_words_followed_by_numbers_are_not_parts():
    r = mask_text("I bought it in 2024 and paid on 1234 street, qty 2.")
    assert r.vault == {}


def test_labels_masked_before_orders():
    r = mask_text("My label UPS-RMA-ORD-9988 does not print.")
    assert r.masked_text == "My label <LABEL_TOKEN_1> does not print."
    assert r.vault == {"<LABEL_TOKEN_1>": "UPS-RMA-ORD-9988"}


def test_duplicate_values_share_one_token():
    r = mask_text("ord/9988 and again ORD-9988, part ab1234 and AB-1234")
    assert r.vault == {"<ORDER_TOKEN_1>": "ORD-9988", "<PART_TOKEN_1>": "AB-1234"}


def test_multiple_distinct_values_get_sequential_tokens():
    r = mask_text("parts AB-1234 and cd/5678")
    assert r.vault == {"<PART_TOKEN_1>": "AB-1234", "<PART_TOKEN_2>": "CD-5678"}


def test_multi_turn_vault_merge_preserves_previous_tokens():
    turn1 = mask_text("I need to return AB-1234 but lost my order number.")
    assert turn1.vault == {"<PART_TOKEN_1>": "AB-1234"}

    turn2 = mask_text("Found it: ord 9988. Same part ab-1234.", vault=turn1.vault)
    assert turn2.vault == {"<PART_TOKEN_1>": "AB-1234", "<ORDER_TOKEN_1>": "ORD-9988"}
    assert turn2.masked_text == "Found it: <ORDER_TOKEN_1>. Same part <PART_TOKEN_1>."
    assert turn2.new_tokens == {"<ORDER_TOKEN_1>": "ORD-9988"}
    # Original vault object is not mutated.
    assert turn1.vault == {"<PART_TOKEN_1>": "AB-1234"}


def test_multi_turn_new_part_gets_next_index():
    v = {"<PART_TOKEN_1>": "AB-1234", "<ORDER_TOKEN_1>": "ORD-9988"}
    r = mask_text("Also returning XY-4321", vault=v)
    assert r.vault["<PART_TOKEN_2>"] == "XY-4321"
    assert r.vault["<PART_TOKEN_1>"] == "AB-1234"


def test_unmask_roundtrip_and_unknown_tokens():
    v = {"<ORDER_TOKEN_1>": "ORD-9988", "<PART_TOKEN_1>": "AB-1234", "<LABEL_TOKEN_1>": "UPS-RMA-ORD-9988"}
    out = unmask_text("Label <LABEL_TOKEN_1> for <PART_TOKEN_1> on <ORDER_TOKEN_1>; <PART_TOKEN_9>", v)
    assert out == "Label UPS-RMA-ORD-9988 for AB-1234 on ORD-9988; <PART_TOKEN_9>"


def test_preprocess_strips_signature_dashes():
    text = "Return AB-1234 please.\n--\nJohn Smith\nAcme Corp ORD-0000 Inc"
    assert preprocess_email(text) == "Return AB-1234 please."


def test_preprocess_strips_best_regards():
    text = "Return AB-1234 please.\n\nBest regards,\nJohn\nPhone 555-1234"
    assert preprocess_email(text) == "Return AB-1234 please."


def test_preprocess_strips_reply_quotes():
    text = "Here is ORD-9988.\n\nOn Mon, Nov 3, 2026 at 9:00 AM SOP <sop@x.com> wrote:\n> What is your order id?\n> AB-9999"
    assert preprocess_email(text) == "Here is ORD-9988."
    text2 = "Here it is.\n> quoted CD-1111\nStill need help."
    assert preprocess_email(text2) == "Here it is.\nStill need help."


def test_preprocess_strips_html():
    text = "<html><body><p>Return <b>ab - 1234</b> from&nbsp;ord/9988</p><br><style>p{}</style></body></html>"
    r = mask_text(text)
    assert "<b>" not in r.clean_text
    assert r.masked_text == "Return <PART_TOKEN_1> from <ORDER_TOKEN_1>"


def test_meeting_link_is_masked_and_restored():
    link = "https://meet.axmattsupply.com/TS-1a2b3c4d"
    m = mask_text(f"Is {link} still the right link for AB-1234?")
    assert m.masked_text == "Is <LINK_TOKEN_1> still the right link for <PART_TOKEN_1>?"
    assert m.vault["<LINK_TOKEN_1>"] == link
    assert contains_raw_pii(link) and not contains_raw_pii(m.masked_text)
    assert unmask_text(m.masked_text, m.vault).startswith(f"Is {link}")
